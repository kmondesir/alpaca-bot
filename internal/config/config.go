// Package config loads the Alpaca and log settings from .env.
package config

import (
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/joho/godotenv"
)

// Config holds the settings read from the environment and .env.
type Config struct {
	APIKey    string
	SecretKey string
	// BaseURL is the trading API base URL, e.g. https://paper-api.alpaca.markets, without /v2.
	BaseURL string
	Demo    bool
	Log     LogSettings
}

// LogSettings says where alpaca logs and when the log file rotates.
type LogSettings struct {
	// Directory is LOG_DIRECTORY (default "logs"); a relative path resolves
	// from the project root.
	Directory string
	// Name is LOG_NAME (default "alpaca.log").
	Name string
	// MaxSizeInMB is MAX_SIZE_IN_MB (default 10, decimals allowed): the size
	// at which the log file rotates. 0 turns rotation off.
	MaxSizeInMB float64
	// MaxBackup is MAX_BACKUP (default 5): how many rotated files to keep.
	// 0 turns rotation off.
	MaxBackup int
	// Level is LOG_LEVEL (default INFO): DEBUG, INFO, WARNING (or WARN),
	// ERROR or CRITICAL, in any case. Lines below it are not written.
	Level string
}

// Load reads root/.env into the environment and returns the settings.
// A variable already set in the environment wins over .env, and a missing
// .env is not an error.
func Load(root string) (Config, error) {
	err := godotenv.Load(filepath.Join(root, ".env"))
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return Config{}, err
	}
	maxSize, err := strconv.ParseFloat(nonEmpty("MAX_SIZE_IN_MB", "10"), 64)
	if err != nil || maxSize < 0 || math.IsInf(maxSize, 0) || math.IsNaN(maxSize) {
		return Config{}, fmt.Errorf("MAX_SIZE_IN_MB must be a number of megabytes, 0 or more, not '%s'", os.Getenv("MAX_SIZE_IN_MB"))
	}
	maxBackup, err := strconv.Atoi(nonEmpty("MAX_BACKUP", "5"))
	if err != nil || maxBackup < 0 {
		return Config{}, fmt.Errorf("MAX_BACKUP must be a whole number, 0 or more, not '%s'", os.Getenv("MAX_BACKUP"))
	}
	level := strings.ToUpper(nonEmpty("LOG_LEVEL", "INFO"))
	if _, ok := levels[level]; !ok {
		return Config{}, fmt.Errorf("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, not '%s'", os.Getenv("LOG_LEVEL"))
	}
	return Config{
		APIKey:    os.Getenv("ALPACA_API_KEY"),
		SecretKey: os.Getenv("ALPACA_SECRET_KEY"),
		BaseURL:   os.Getenv("ALPACA_BASE_URL"),
		Demo:      strings.ToLower(getenv("DEMO", "true")) == "true",
		Log: LogSettings{
			Directory:   nonEmpty("LOG_DIRECTORY", "logs"),
			Name:        nonEmpty("LOG_NAME", "alpaca.log"),
			MaxSizeInMB: maxSize,
			MaxBackup:   maxBackup,
			Level:       level,
		},
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

// nonEmpty returns the variable's trimmed value, or fallback when it is unset or blank.
func nonEmpty(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func getenv(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}
