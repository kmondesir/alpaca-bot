package config

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"sync"
)

// rotatingFile appends to a log file and rolls it over like Python's
// RotatingFileHandler: when a write would take the file to maxBytes, name.N
// is dropped, name.1 … name.N-1 shift up by one, and name becomes name.1.
type rotatingFile struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	backups  int
	file     *os.File
	size     int64
}

func openRotatingFile(path string, maxBytes int64, backups int) (*rotatingFile, error) {
	r := &rotatingFile{path: path, maxBytes: maxBytes, backups: backups}
	if err := r.open(); err != nil {
		return nil, err
	}
	return r, nil
}

func (r *rotatingFile) open() error {
	file, err := os.OpenFile(r.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	info, err := file.Stat()
	if err != nil {
		file.Close()
		return err
	}
	r.file, r.size = file, info.Size()
	return nil
}

// Write writes one whole record, rolling over first if it would reach maxBytes.
func (r *rotatingFile) Write(p []byte) (int, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.maxBytes > 0 && r.backups > 0 && r.size > 0 && r.size+int64(len(p)) >= r.maxBytes {
		if err := r.rollover(); err != nil {
			return 0, err
		}
	}
	n, err := r.file.Write(p)
	r.size += int64(n)
	return n, err
}

func (r *rotatingFile) rollover() error {
	// Windows can't rename an open file.
	if err := r.file.Close(); err != nil {
		return err
	}
	for i := r.backups - 1; i >= 1; i-- {
		if err := renameIfExists(fmt.Sprintf("%s.%d", r.path, i), fmt.Sprintf("%s.%d", r.path, i+1)); err != nil {
			return err
		}
	}
	if err := renameIfExists(r.path, r.path+".1"); err != nil {
		return err
	}
	return r.open()
}

// renameIfExists moves from to to, replacing to; a missing from is skipped.
func renameIfExists(from, to string) error {
	if _, err := os.Stat(from); errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err := os.Remove(to); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return os.Rename(from, to)
}

func (r *rotatingFile) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.file.Close()
}
