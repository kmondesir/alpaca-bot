// Package config loads the Alpaca settings from .env and holds the fixed log settings.
package config

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/google/uuid"
	"github.com/joho/godotenv"
)

// Log settings. A relative LogDirectory resolves from the project root;
// --logdirectory and --logname override the first two for a single run.
const (
	LogDirectory = "logs"
	LogName      = "alpaca.log"
	MaxSizeInMB  = 10
	MaxBackup    = 5
	LogLevel     = "INFO"
)

// Config holds the four settings read from the environment and .env.
type Config struct {
	APIKey    string
	SecretKey string
	// BaseURL is the trading API base URL, e.g. https://paper-api.alpaca.markets, without /v2.
	BaseURL string
	Demo    bool
}

// Load reads root/.env into the environment and returns the settings.
// A variable already set in the environment wins over .env, and a missing
// .env is not an error.
func Load(root string) (Config, error) {
	err := godotenv.Load(filepath.Join(root, ".env"))
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return Config{}, err
	}
	return Config{
		APIKey:    os.Getenv("ALPACA_API_KEY"),
		SecretKey: os.Getenv("ALPACA_SECRET_KEY"),
		BaseURL:   os.Getenv("ALPACA_BASE_URL"),
		Demo:      strings.ToLower(getenv("DEMO", "true")) == "true",
	}, nil
}

// Missing returns the names of the required variables that are empty.
func (c Config) Missing() []string {
	var missing []string
	for _, required := range []struct{ name, value string }{
		{"ALPACA_API_KEY", c.APIKey},
		{"ALPACA_SECRET_KEY", c.SecretKey},
		{"ALPACA_BASE_URL", c.BaseURL},
	} {
		if strings.TrimSpace(required.value) == "" {
			missing = append(missing, required.name)
		}
	}
	return missing
}

// RootDir returns the project root: the folder holding the executable.
// Under go run or go test the executable is built in a temp folder, so the
// working directory is used instead.
func RootDir() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", err
	}
	dir := filepath.Dir(executable)
	inTemp := strings.HasPrefix(strings.ToLower(dir), strings.ToLower(filepath.Clean(os.TempDir())))
	if inTemp || strings.Contains(dir, "go-build") {
		return os.Getwd()
	}
	return dir, nil
}

// NewClientOrderID returns the random UUID used as the client_order_id when the caller doesn't provide one.
func NewClientOrderID() string {
	return uuid.NewString()
}

func getenv(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}
