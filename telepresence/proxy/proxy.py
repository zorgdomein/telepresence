# Copyright 2020 Datawire. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
from subprocess import CalledProcessError
from typing import Callable, List, NamedTuple, Optional, Tuple, Type

from telepresence.cli import PortMapping
from telepresence.runner import Runner
from telepresence.utilities import get_alternate_nameserver

from .deployment import (
    existing_deployment, existing_deployment_openshift, get_image_name,
    supplant_deployment, swap_deployment_openshift
)
from .manifest import (
    Manifest, make_k8s_list, make_new_proxy_pod_manifest, make_svc_manifest
)
from .remote import (
    RemoteInfo, get_remote_info, make_remote_info_from_pod, wait_for_pod
)

ProxyIntent = NamedTuple(
    "ProxyIntent", [
        ("name", str),
        ("container", str),
        ("expose", PortMapping),
        ("custom_nameserver", Optional[str]),
        ("service_account", str),
    ]
)


def _dc_exists(runner: Runner, name: str) -> bool:
    """
    If we're using OpenShift Origin, we may be using a DeploymentConfig instead
    of a Deployment. Return True if a dc exists with the given name.
    """
    # Need to use oc to manage DeploymentConfigs. The cluster needs to be
    # running OpenShift as well. Check for both.
    kube = runner.kubectl
    if kube.command != "oc" or not kube.cluster_is_openshift:
        return False
    if ":" in name:
        name, container = name.split(":", 1)
    try:
        runner.check_call(runner.kubectl("get", "dc/{}".format(name)))
        return True
    except CalledProcessError as exc:
        runner.show(
            "Failed to find OpenShift deploymentconfig {}:".format(name)
        )
        runner.show("  {}".format(str(exc.stderr)))
        runner.show("Will try regular Kubernetes Deployment.")
    return False


def setup(runner: Runner,
          args: argparse.Namespace) -> Callable[[Runner], RemoteInfo]:
    """
    Determine how the user wants to set up the proxy in the cluster.
    """

    # OpenShift doesn't support running as root:
    if (
        args.expose.has_privileged_ports()
        and runner.kubectl.cluster_is_openshift
    ):
        raise runner.fail("OpenShift does not support ports <1024.")

    # Check the service account, if present
    if args.service_account:
        try:
            runner.check_call(
                runner.kubectl("get", "serviceaccount", args.service_account)
            )
        except CalledProcessError as exc:
            raise runner.fail(
                "Check service account {} failed:\n{}".format(
                    args.service_account, exc.stderr
                )
            )

    # Figure out which operation the user wants
    if args.deployment is not None:
        # This implies --deployment
        if _dc_exists(runner, args.deployment_arg):
            operationType = ExistingDC  # type: Type[ProxyOperation]
        else:
            operationType = ExistingDeploy

    if args.new_deployment is not None:
        # This implies --new-deployment
        operationType = New

    if args.swap_deployment is not None:
        # This implies --swap-deployment
        if _dc_exists(runner, args.deployment_arg):
            operationType = SwapDC
        else:
            operationType = SwapDeploy

    name, container = args.deployment_arg, ""
    if ":" in name:
        name, container = name.split(":", 1)

    # minikube/minishift break DNS because DNS gets captured, sent to minikube,
    # which sends it back to the DNS server set by host, resulting in a DNS
    # loop... We've fixed that for most cases by setting a distinct name server
    # for the proxy to use when making a new proxy pod, but that does not work
    # for --deployment.
    custom_nameserver = None
    if args.method == "vpn-tcp" and runner.kubectl.in_local_vm:
        if args.operation == "deployment":
            raise runner.fail(
                "vpn-tcp method doesn't work with minikube/minishift when"
                " using --deployment. Use --swap-deployment or"
                " --new-deployment instead."
            )
        try:
            custom_nameserver = get_alternate_nameserver()
        except Exception as exc:
            raise runner.fail(
                "Failed to find a fallback nameserver: {}".format(exc)
            )

    intent = ProxyIntent(
        name,
        container,
        args.expose,
        custom_nameserver,
        args.service_account or "",
    )
    operation = operationType(intent)

    operation.prepare(runner)

    return operation.act


LegacyOperation = Callable[[Runner, str, PortMapping, Optional[str], str],
                           Tuple[str, Optional[str]]]


class ProxyOperation:
    def __init__(self, intent: ProxyIntent) -> None:
        self.intent = intent
        self.remote_info = None  # type: Optional[RemoteInfo]

    def prepare(self, runner: Runner) -> None:
        pass

    def act(self, _: Runner) -> RemoteInfo:
        raise NotImplementedError()

    def _legacy(
        self,
        runner: Runner,
        legacy_op: LegacyOperation,
        deployment_type: str,
    ) -> RemoteInfo:
        deployment_arg = self.intent.name
        if self.intent.container:
            deployment_arg += ":" + self.intent.container

        tel_deployment, run_id = legacy_op(
            runner,
            deployment_arg,
            self.intent.expose,
            self.intent.custom_nameserver,
            self.intent.service_account,
        )

        remote_info = get_remote_info(
            runner,
            tel_deployment,
            deployment_type,
            run_id=run_id,
        )

        return remote_info


class New(ProxyOperation):
    def prepare(self, runner: Runner) -> None:
        self.manifests = []  # type: List[Manifest]

        # Construct a Pod manifest
        env = {}
        if self.intent.custom_nameserver:
            # If we're on local VM we need to use different nameserver to
            # prevent infinite loops caused by sshuttle:
            env["TELEPRESENCE_NAMESERVER"] = self.intent.custom_nameserver

        pod = make_new_proxy_pod_manifest(
            self.intent.name,
            runner.session_id,
            get_image_name(runner, self.intent.expose),
            self.intent.service_account,
            env,
        )
        self.manifests.append(pod)

        # Construct a Service manifest as needed
        if self.intent.expose.remote():
            svc = make_svc_manifest(
                self.intent.name,
                dict(telepresence=runner.session_id),
                dict(telepresence=runner.session_id),
                {p: p
                 for p in self.intent.expose.remote()},
            )
            self.manifests.append(svc)

        self.remote_info = make_remote_info_from_pod(pod)

    def act(self, runner: Runner) -> RemoteInfo:
        assert self.remote_info is not None

        runner.show(
            "Starting network proxy to cluster using "
            "new Pod {}".format(self.intent.name)
        )

        manifest_list = make_k8s_list(self.manifests)
        manifest_json = json.dumps(manifest_list)
        try:
            runner.check_call(
                runner.kubectl("create", "-f", "-"),
                input=manifest_json.encode("utf-8")
            )
        except CalledProcessError as exc:
            raise runner.fail(
                "Failed to create Pod/Service {}:\n{}".format(
                    self.intent.name, exc.stderr
                )
            )

        def clean_up():
            runner.show("Cleaning up Pod/Service {}".format(self.intent.name))
            runner.check_call(
                runner.kubectl(
                    "delete",
                    "--ignore-not-found",
                    "--wait=false",
                    "--selector=telepresence=" + runner.session_id,
                    "svc,pod",
                )
            )

        runner.add_cleanup("Delete new Pod/Service", clean_up)

        wait_for_pod(runner, self.remote_info)

        return self.remote_info


class ExistingDeploy(ProxyOperation):
    def act(self, runner: Runner) -> RemoteInfo:
        return self._legacy(runner, existing_deployment, "deployment")


class ExistingDC(ProxyOperation):
    def act(self, runner: Runner) -> RemoteInfo:
        return self._legacy(
            runner, existing_deployment_openshift, "deploymentconfig"
        )


class SwapDeploy(ProxyOperation):
    def act(self, runner: Runner) -> RemoteInfo:
        return self._legacy(runner, supplant_deployment, "deployment")


class SwapDC(ProxyOperation):
    def act(self, runner: Runner) -> RemoteInfo:
        return self._legacy(
            runner, swap_deployment_openshift, "deploymentconfig"
        )


"""
class Swap(ProxyOperation):
    def prepare(self, runner: Runner) -> None:
        # Grab original deployment's Pod Config
        deployment = get_deployment(runner, name)  # from .remote

        # Compute proxy Pod's manifest
        pod_spec = deployment["spec"]["template"]["spec"]
        # TODO: perform the usual swap changes
        # TODO: rip off from new_swapped_deployment(...)
        # FIXME: Implement this...

        # FIXME: Copy-pasta from New.prepare(...)
        # FIXME: factor out more of making a Tel pod?
        pod = make_new_proxy_pod_manifest(...)

        self.remote_info = make_remote_info_from_pod(pod)

    def act(self, runner: Runner) -> RemoteInfo:
        assert self.remote_info is not None

        # FIXME: Copy-pasta from New.act(...)
        # Apply the manifest
        # Set up for cleanup

        # FIXME: Factor this out?
        # This all seems repetitive

        wait_for_pod(runner, self.remote_info)

        return self.remote_info


class Existing(ProxyOperation):
    def prepare(self, runner: Runner) -> None:
        # Grab original Deployment's manifest
        deployment = get_deployment(runner, name)  # from .remote
        deployment_name = deployment["metadata"]["name"]  # type: str
        deployment_type = deployment["kind"]  # type: str

        # Find the Pod for this Deployment
        # FIXME: Implement this
        # TODO: This really does too much work; simplify it!
        # E.g., this waits for the pod, which we don't want to do so early...
        self.remote_info = get_remote_info(
            runner, deployment_name, deployment_type, runner.session_id
        )

    def act(self, runner: Runner) -> RemoteInfo:
        assert self.remote_info is not None

        # Nothing to do here, right?

        wait_for_pod(runner, self.remote_info)

        return self.remote_info
"""
