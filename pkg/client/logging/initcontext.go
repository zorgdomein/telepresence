package logging

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/sirupsen/logrus"
	"golang.org/x/term"

	"github.com/datawire/dlib/dlog"
	"github.com/telepresenceio/telepresence/v2/pkg/filelocation"
)

// IsTerminal returns whether the given file descriptor is a terminal
var IsTerminal = term.IsTerminal

// loggerForTest exposes internals to initcontext_test.go
var loggerForTest *logrus.Logger

type formatter struct {
	inner logrus.Formatter
}

func (f formatter) Format(entry *logrus.Entry) ([]byte, error) {
	const callerPos = 130

	line, err := f.inner.Format(entry)
	if err != nil {
		return nil, err
	}
	sep := bytes.LastIndex(line, []byte(" func="))
	if sep < 0 || sep >= callerPos {
		return line, nil
	}
	var out bytes.Buffer
	fmt.Fprintf(&out, "%-*s %s", callerPos, line[:sep], line[sep+1:])
	return out.Bytes(), nil
}

// InitContext sets up standard Telepresence logging for a background process
func InitContext(ctx context.Context, name string) (context.Context, error) {
	logger := logrus.New()
	loggerForTest = logger
	logger.SetLevel(logrus.DebugLevel)

	if IsTerminal(int(os.Stdout.Fd())) {
		logger.SetFormatter(&logrus.TextFormatter{
			FullTimestamp:   true,
			TimestampFormat: "15:04:05.0000",
			SortingFunc:     dlog.DefaultFieldSort,
		})
	} else {
		logger.SetReportCaller(true)
		logger.SetFormatter(formatter{
			inner: &logrus.TextFormatter{
				FullTimestamp:   true,
				TimestampFormat: "2006-01-02 15:04:05.0000",
				SortingFunc:     dlog.DefaultFieldSort,
			},
		})
		dir, err := filelocation.AppUserLogDir(ctx)
		if err != nil {
			return ctx, err
		}
		rf, err := OpenRotatingFile(filepath.Join(dir, name+".log"), "20060102T150405", true, true, 0600, NewRotateOnce(), 5)
		if err != nil {
			return ctx, err
		}
		logger.SetOutput(rf)
	}
	return dlog.WithLogger(ctx, dlog.WrapLogrus(logger)), nil
}
