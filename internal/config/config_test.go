package config

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
)

var names = []string{"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL", "DEMO",
	"LOG_DIRECTORY", "LOG_NAME", "MAX_SIZE_IN_MB", "MAX_BACKUP", "LOG_LEVEL"}

// unsetAll clears the settings for one test and restores them afterwards.
func unsetAll(t *testing.T) {
	t.Helper()
	for _, name := range names {
		if value, ok := os.LookupEnv(name); ok {
			t.Cleanup(func() { os.Setenv(name, value) })
		} else {
			t.Cleanup(func() { os.Unsetenv(name) })
		}
		os.Unsetenv(name)
	}
}

func writeEnv(t *testing.T, content string) string {
	t.Helper()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, ".env"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return root
}

func TestLoadReadsEnvFile(t *testing.T) {
	unsetAll(t)
	root := writeEnv(t, "ALPACA_SECRET_KEY=secret\n\n# Broker config\n"+
		"ALPACA_BASE_URL=https://paper-api.alpaca.markets\nALPACA_API_KEY=key\nDEMO=false\n")
	got, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	want := Config{APIKey: "key", SecretKey: "secret", BaseURL: "https://paper-api.alpaca.markets", Demo: false, Log: defaults}
	if got != want {
		t.Errorf("Load = %+v, want %+v", got, want)
	}
	if missing := got.Missing(); missing != nil {
		t.Errorf("Missing = %v, want none", missing)
	}
}

func TestEnvironmentWinsOverEnvFile(t *testing.T) {
	unsetAll(t)
	t.Setenv("ALPACA_API_KEY", "from-environment")
	t.Setenv("ALPACA_SECRET_KEY", "") // set but empty still wins, as with python-dotenv
	root := writeEnv(t, "ALPACA_API_KEY=from-file\nALPACA_SECRET_KEY=from-file\n")
	got, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	if got.APIKey != "from-environment" || got.SecretKey != "" {
		t.Errorf("Load = %+v, want the environment's values", got)
	}
}

func TestLoadWithoutEnvFile(t *testing.T) {
	unsetAll(t)
	got, err := Load(t.TempDir())
	if err != nil {
		t.Fatalf("missing .env should not be an error: %v", err)
	}
	if !got.Demo {
		t.Error("DEMO should default to true")
	}
	want := []string{"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL"}
	if missing := got.Missing(); !reflect.DeepEqual(missing, want) {
		t.Errorf("Missing = %v, want %v", missing, want)
	}
}

func TestDemo(t *testing.T) {
	for value, want := range map[string]bool{"true": true, "TRUE": true, "True": true, "false": false, "": false, "yes": false} {
		unsetAll(t)
		t.Setenv("DEMO", value)
		got, err := Load(t.TempDir())
		if err != nil {
			t.Fatal(err)
		}
		if got.Demo != want {
			t.Errorf("DEMO=%q: Demo = %v, want %v", value, got.Demo, want)
		}
	}
}

func TestMissingIgnoresWhitespace(t *testing.T) {
	c := Config{APIKey: " ", SecretKey: "secret", BaseURL: "\t"}
	want := []string{"ALPACA_API_KEY", "ALPACA_BASE_URL"}
	if missing := c.Missing(); !reflect.DeepEqual(missing, want) {
		t.Errorf("Missing = %v, want %v", missing, want)
	}
}

func TestNewClientOrderID(t *testing.T) {
	first, second := NewClientOrderID(), NewClientOrderID()
	id, err := uuid.Parse(first)
	if err != nil || id.Version() != 4 {
		t.Errorf("NewClientOrderID = %q, want a version 4 UUID", first)
	}
	if first == second {
		t.Error("NewClientOrderID returned the same id twice")
	}
}

func TestRootDirUnderGoTestIsWorkingDirectory(t *testing.T) {
	root, err := RootDir()
	if err != nil {
		t.Fatal(err)
	}
	wd, _ := os.Getwd()
	if root != wd {
		t.Errorf("RootDir = %q, want the working directory %q", root, wd)
	}
}

func TestLoadLogSettings(t *testing.T) {
	unsetAll(t)
	t.Setenv("LOG_NAME", "from-environment.log") // the environment still wins
	root := writeEnv(t, `LOG_DIRECTORY=C:/logs/alpaca
LOG_NAME=from-file.log
MAX_SIZE_IN_MB=2.5
MAX_BACKUP=0
LOG_LEVEL=debug
`)
	got, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	want := LogSettings{Directory: "C:/logs/alpaca", Name: "from-environment.log", MaxSizeInMB: 2.5, MaxBackup: 0, Level: "DEBUG"}
	if got.Log != want {
		t.Errorf("Log = %+v, want %+v", got.Log, want)
	}
}

func TestBlankLogSettingsUseDefaults(t *testing.T) {
	unsetAll(t)
	root := writeEnv(t, "LOG_DIRECTORY=\nLOG_NAME= \nMAX_SIZE_IN_MB=\nMAX_BACKUP=\nLOG_LEVEL=\n")
	got, err := Load(root)
	if err != nil {
		t.Fatal(err)
	}
	if got.Log != defaults {
		t.Errorf("Log = %+v, want the defaults %+v", got.Log, defaults)
	}
}

func TestInvalidLogSettings(t *testing.T) {
	for name, value := range map[string]string{
		"MAX_SIZE_IN_MB": "ten", "MAX_BACKUP": "2.5", "LOG_LEVEL": "LOUD",
	} {
		unsetAll(t)
		t.Setenv(name, value)
		_, err := Load(t.TempDir())
		if err == nil || !strings.Contains(err.Error(), name) || !strings.Contains(err.Error(), "'"+value+"'") {
			t.Errorf("%s=%s: err = %v, want an error naming the variable and value", name, value, err)
		}
	}
	for _, negative := range []string{"MAX_SIZE_IN_MB", "MAX_BACKUP"} {
		unsetAll(t)
		t.Setenv(negative, "-1")
		if _, err := Load(t.TempDir()); err == nil {
			t.Errorf("%s=-1 was accepted", negative)
		}
	}
}
