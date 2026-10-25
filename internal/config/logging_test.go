package config

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// linePattern is the Python log format:
// <UTC timestamp> <computer name> <uuid7> <module> <line number> [<message>]
var linePattern = regexp.MustCompile(
	`^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z) (\S+) ([0-9a-f-]{36}) (\S+) (\d+) \[(.*)\]$`)

func TestLineFormat(t *testing.T) {
	var buffer bytes.Buffer
	logger := slog.New(newHandler(slog.LevelInfo, &buffer))
	_, _, line, _ := runtime.Caller(0)
	logger.Info(fmt.Sprintf("Starting alpaca (demo=%v)", true)) // must stay on the line after runtime.Caller
	logger.Info("second", "id", "abc")

	lines := strings.Split(strings.TrimSuffix(buffer.String(), newline), newline)
	if len(lines) != 2 {
		t.Fatalf("got %d lines, want 2: %q", len(lines), buffer.String())
	}
	first := linePattern.FindStringSubmatch(lines[0])
	if first == nil {
		t.Fatalf("line %q does not match the log format", lines[0])
	}
	hostname, _ := os.Hostname()
	if first[2] != hostname {
		t.Errorf("computer name = %q, want %q", first[2], hostname)
	}
	if id, err := uuid.Parse(first[3]); err != nil || id.Version() != 7 {
		t.Errorf("id %q is not a uuid7", first[3])
	}
	if first[4] != "logging_test" || first[5] != fmt.Sprint(line+1) {
		t.Errorf("source = %s %s, want logging_test %d", first[4], first[5], line+1)
	}
	if first[6] != "Starting alpaca (demo=true)" {
		t.Errorf("message = %q", first[6])
	}
	second := linePattern.FindStringSubmatch(lines[1])
	if second == nil || second[6] != "second id=abc" {
		t.Errorf("second line = %q, want message %q", lines[1], "second id=abc")
	}
	if second != nil && second[3] == first[3] {
		t.Error("two lines share a uuid7")
	}
}

func TestLevelFilter(t *testing.T) {
	var buffer bytes.Buffer
	logger := slog.New(newHandler(levels["INFO"], &buffer))
	logger.Debug("hidden")
	logger.Warn("shown")
	if strings.Contains(buffer.String(), "hidden") || !strings.Contains(buffer.String(), "[shown]") {
		t.Errorf("INFO level wrote %q", buffer.String())
	}
}

func TestStackFollowsLine(t *testing.T) {
	var buffer bytes.Buffer
	slog.New(newHandler(slog.LevelInfo, &buffer)).Error("crashed", StackKey, "frame one\nframe two\n")
	lines := strings.Split(buffer.String(), newline)
	if len(lines) != 4 || !strings.HasSuffix(lines[0], "[crashed]") || lines[1] != "frame one" || lines[2] != "frame two" {
		t.Errorf("got %q", buffer.String())
	}
}

func TestRotation(t *testing.T) {
	path := filepath.Join(t.TempDir(), "alpaca.log")
	file, err := openRotatingFile(path, 25, 2)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	for i := 1; i <= 5; i++ {
		fmt.Fprintf(file, "record %d....\n", i) // 14 bytes, so each file holds one record
	}
	for suffix, want := range map[string]string{"": "record 5", ".1": "record 4", ".2": "record 3"} {
		content, err := os.ReadFile(path + suffix)
		if err != nil || !strings.HasPrefix(string(content), want) {
			t.Errorf("%s holds %q (%v), want %q", filepath.Base(path+suffix), content, err, want)
		}
	}
	if _, err := os.Stat(path + ".3"); !errors.Is(err, fs.ErrNotExist) {
		t.Error("kept more than 2 backups")
	}
}

func TestRotationCountsExistingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "alpaca.log")
	if err := os.WriteFile(path, []byte("earlier run\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	file, err := openRotatingFile(path, 20, 5)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	fmt.Fprint(file, "this run\n")
	if content, _ := os.ReadFile(path + ".1"); string(content) != "earlier run\n" {
		t.Errorf("alpaca.log.1 = %q, want the earlier run", content)
	}
}

// defaults are the log settings Load returns with nothing set.
var defaults = LogSettings{Directory: "logs", Name: "alpaca.log", MaxSizeInMB: 10, MaxBackup: 5, Level: "INFO"}

func TestSetupLoggingDirectories(t *testing.T) {
	root := t.TempDir()
	defer slog.SetDefault(slog.Default())

	path, closeLog, err := SetupLogging(root, defaults, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	closeLog()
	if want := filepath.Join(root, "logs", "alpaca.log"); path != want {
		t.Errorf("default path = %q, want %q", path, want)
	}

	absolute := filepath.Join(t.TempDir(), "elsewhere")
	settings := defaults
	settings.Directory, settings.Name = absolute, "alpaca.log"
	path, closeLog, err = SetupLogging(root, settings, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	slog.Info("written")
	closeLog()
	content, err := os.ReadFile(filepath.Join(absolute, "alpaca.log"))
	if err != nil || !strings.Contains(string(content), "[written]") {
		t.Errorf("absolute directory: read %q, %v", content, err)
	}
}

func TestSetupLoggingUsesSizeBackupsAndLevel(t *testing.T) {
	root := t.TempDir()
	defer slog.SetDefault(slog.Default())
	// 0.0001 MB is 104 bytes, so each record below starts a new file.
	settings := LogSettings{Directory: "logs", Name: "small.log", MaxSizeInMB: 0.0001, MaxBackup: 2, Level: "WARNING"}
	path, closeLog, err := SetupLogging(root, settings, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	slog.Info("below the level")
	for i := 1; i <= 4; i++ {
		slog.Warn(fmt.Sprintf("record %d %s", i, strings.Repeat("x", 40)))
	}
	closeLog()

	current, _ := os.ReadFile(path)
	if !strings.Contains(string(current), "[record 4 ") {
		t.Errorf("small.log = %q, want only record 4", current)
	}
	if _, err := os.Stat(path + ".2"); err != nil {
		t.Errorf("small.log.2 missing: %v", err)
	}
	if _, err := os.Stat(path + ".3"); !errors.Is(err, fs.ErrNotExist) {
		t.Error("kept more than MAX_BACKUP=2 rotated files")
	}
	for _, suffix := range []string{"", ".1", ".2"} {
		content, _ := os.ReadFile(path + suffix)
		if strings.Contains(string(content), "below the level") {
			t.Errorf("small.log%s has an INFO line under LOG_LEVEL=WARNING", suffix)
		}
	}
}

// TestLogPanic runs a child test process that panics and checks it exits 1
// and leaves the panic in the log file.
func TestLogPanic(t *testing.T) {
	if directory := os.Getenv("LOG_PANIC_DIR"); directory != "" {
		defer LogPanic()
		if _, _, err := SetupLogging(directory, defaults, os.Stderr); err != nil {
			t.Fatal(err)
		}
		panic("boom")
	}

	directory := t.TempDir()
	command := exec.Command(os.Args[0], "-test.run=^TestLogPanic$")
	command.Env = append(os.Environ(), "LOG_PANIC_DIR="+directory)
	err := command.Run()
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) || exitErr.ExitCode() != 1 {
		t.Fatalf("child exited with %v, want exit code 1", err)
	}
	content, _ := os.ReadFile(filepath.Join(directory, "logs", "alpaca.log"))
	if !strings.Contains(string(content), "[Uncaught panic; run aborted: boom]") ||
		!strings.Contains(string(content), "TestLogPanic") {
		t.Errorf("log does not record the panic and its stack:\n%s", content)
	}
}
