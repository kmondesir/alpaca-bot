package config

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// LevelCritical matches Python's CRITICAL, one step above ERROR.
const LevelCritical = slog.LevelError + 4

// StackKey is the attribute whose value is written on the lines after the
// record, as Python writes a traceback.
const StackKey = "stack"

var levels = map[string]slog.Level{
	"DEBUG":    slog.LevelDebug,
	"INFO":     slog.LevelInfo,
	"WARNING":  slog.LevelWarn,
	"WARN":     slog.LevelWarn,
	"ERROR":    slog.LevelError,
	"CRITICAL": LevelCritical,
}

// levelName returns the level as Python names it (WARNING, not WARN).
func levelName(level slog.Level) string {
	switch level {
	case slog.LevelDebug:
		return "DEBUG"
	case slog.LevelInfo:
		return "INFO"
	case slog.LevelWarn:
		return "WARNING"
	case slog.LevelError:
		return "ERROR"
	case LevelCritical:
		return "CRITICAL"
	}
	return level.String()
}

// hostFQDN returns the computer's fully qualified name, found as Python's
// socket.getfqdn finds it: the first name with a dot among the names its
// addresses resolve to, or the plain host name when none has one.
var hostFQDN = sync.OnceValue(func() string {
	hostname, err := os.Hostname()
	if err != nil {
		return "?"
	}
	if strings.Contains(hostname, ".") {
		return hostname
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	addrs, _ := net.DefaultResolver.LookupHost(ctx, hostname)
	for _, addr := range addrs {
		names, _ := net.DefaultResolver.LookupAddr(ctx, addr)
		for _, name := range names {
			if name = strings.TrimSuffix(name, "."); strings.Contains(name, ".") {
				return name
			}
		}
	}
	return hostname
})

// newline matches what Python's text-mode log files and stderr write.
var newline = func() string {
	if runtime.GOOS == "windows" {
		return "\r\n"
	}
	return "\n"
}()

// handler writes each record as
//
//	<UTC timestamp> <level> <host FQDN> <run uuid7> <module> <line number> [<message>]
//
// where module is the calling file's name without .go, as in the Python logs,
// and the uuid7 is the same on every line of one run.
type handler struct {
	mu      *sync.Mutex
	writers []io.Writer
	level   slog.Level
	host    string
	runID   string
	attrs   []slog.Attr
}

func newHandler(level slog.Level, writers ...io.Writer) *handler {
	return &handler{mu: &sync.Mutex{}, writers: writers, level: level, host: hostFQDN(), runID: newUUID7()}
}

func (h *handler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= h.level
}

func (h *handler) Handle(_ context.Context, record slog.Record) error {
	module, line := "?", 0
	if record.PC != 0 {
		frame, _ := runtime.CallersFrames([]uintptr{record.PC}).Next()
		module = strings.TrimSuffix(filepath.Base(frame.File), ".go")
		line = frame.Line
	}

	message := record.Message
	stack := ""
	addAttr := func(attr slog.Attr) bool {
		if attr.Key == StackKey {
			stack = attr.Value.String()
		} else {
			message += fmt.Sprintf(" %s=%v", attr.Key, attr.Value)
		}
		return true
	}
	for _, attr := range h.attrs {
		addAttr(attr)
	}
	record.Attrs(addAttr)

	var b strings.Builder
	fmt.Fprintf(&b, "%s %s %s %s %s %d [%s]",
		record.Time.UTC().Format("2006-01-02T15:04:05.000Z"), levelName(record.Level), h.host, h.runID, module, line, message)
	if stack != "" {
		b.WriteString(newline)
		b.WriteString(strings.ReplaceAll(strings.TrimRight(stack, "\n"), "\n", newline))
	}
	b.WriteString(newline)

	h.mu.Lock()
	defer h.mu.Unlock()
	var firstErr error
	for _, w := range h.writers {
		if _, err := io.WriteString(w, b.String()); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (h *handler) WithAttrs(attrs []slog.Attr) slog.Handler {
	clone := *h
	clone.attrs = append(append([]slog.Attr{}, h.attrs...), attrs...)
	return &clone
}

// WithGroup is not used; groups are flattened into the message.
func (h *handler) WithGroup(string) slog.Handler {
	return h
}

func newUUID7() string {
	id, err := uuid.NewV7()
	if err != nil {
		return uuid.NewString()
	}
	return id.String()
}

// SetupLogging logs to a rotating file and to console (stderr) through slog's
// default logger, and returns the log file path and a function that closes
// the file.
//
// A relative settings.Directory resolves from root. console is stderr in
// practice, which keeps stdout free for the command's JSON result.
func SetupLogging(root string, settings LogSettings, console io.Writer) (string, func() error, error) {
	directory := settings.Directory
	if !filepath.IsAbs(directory) {
		directory = filepath.Join(root, directory)
	}
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return "", nil, err
	}
	path := filepath.Join(directory, settings.Name)
	file, err := openRotatingFile(path, int64(settings.MaxSizeInMB*1024*1024), settings.MaxBackup)
	if err != nil {
		return "", nil, err
	}
	slog.SetDefault(slog.New(newHandler(levels[settings.Level], file, console)))
	return path, file.Close, nil
}

// LogPanic logs a panic with its stack and exits with code 1, so a crash
// leaves a trace for callers that discard stderr. Defer it first in main:
//
//	defer config.LogPanic()
//
// Without it Go exits with code 2, which callers read as invalid arguments.
func LogPanic() {
	value := recover()
	if value == nil {
		return
	}
	slog.Log(context.Background(), LevelCritical, fmt.Sprintf("Uncaught panic; run aborted: %v", value),
		StackKey, string(debug.Stack()))
	os.Exit(1)
}
