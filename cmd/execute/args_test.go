package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// recording is one scenario recorded from the Python version by tools/record_outputs.py.
type recording struct {
	Args         []string        `json:"args"`
	EnvOverrides []string        `json:"env_overrides"`
	ExitCode     int             `json:"exit_code"`
	Stdout       json.RawMessage `json:"stdout"`
	StderrTail   []string        `json:"stderr_tail"`
}

func loadRecordings(t *testing.T) map[string]recording {
	t.Helper()
	paths, err := filepath.Glob("../../testdata/python/*.json")
	if err != nil || len(paths) == 0 {
		t.Fatalf("no recordings found: %v", err)
	}
	recordings := map[string]recording{}
	for _, path := range paths {
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var r recording
		if err := json.Unmarshal(content, &r); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		recordings[strings.TrimSuffix(filepath.Base(path), ".json")] = r
	}
	return recordings
}

// TestArgumentErrorsMatchPython replays every recorded argument error: Go
// must reject the same arguments with the same message.
func TestArgumentErrorsMatchPython(t *testing.T) {
	checked := 0
	for name, r := range loadRecordings(t) {
		if r.ExitCode != 2 {
			continue
		}
		checked++
		_, err := parseArgs(r.Args)
		want := strings.TrimPrefix(r.StderrTail[len(r.StderrTail)-1], "execute.py: error: ")
		if err == nil || err.Error() != want {
			t.Errorf("%s: parseArgs(%q) error = %v, want %q", name, r.Args, err, want)
		}
	}
	if checked < 9 {
		t.Errorf("checked %d argument errors, want at least 9", checked)
	}
}

// TestRecordedArgumentsParse checks every recorded command that Python
// accepted also parses in Go.
func TestRecordedArgumentsParse(t *testing.T) {
	for name, r := range loadRecordings(t) {
		if r.ExitCode == 2 {
			continue
		}
		if _, err := parseArgs(r.Args); err != nil {
			t.Errorf("%s: parseArgs(%q) = %v, want no error", name, r.Args, err)
		}
	}
}

func TestParseArgs(t *testing.T) {
	for _, c := range []struct {
		args []string
		want options
	}{
		{[]string{"--balance"}, options{balance: true}},
		{[]string{"--orders"}, options{ordersSet: true}},
		{[]string{"--orders", "--logname", "x.log"}, options{ordersSet: true, logName: "x.log"}},
		{[]string{"--orders", "{b, a ,b}"}, options{ordersSet: true, orders: []string{"b", "a"}}},
		{[]string{"--orders={a}"}, options{ordersSet: true, orders: []string{"a"}}},
		{[]string{"--close", "a", "--client_order_id", " tag "}, options{close: []string{"a"}, clientOrderID: "tag"}},
		{[]string{"--asset", "SPY", "--amount=500", "--position", "Long", "--logdirectory", `C:\logs`},
			options{asset: "SPY", assetSet: true, amount: 500, amountSet: true, position: "long", logDirectory: `C:\logs`}},
		{[]string{"--balance", "--balance"}, options{balance: true}},
	} {
		got, err := parseArgs(c.args)
		if err != nil || !reflect.DeepEqual(got, c.want) {
			t.Errorf("parseArgs(%q) = %+v, %v; want %+v", c.args, got, err, c.want)
		}
	}
}

func TestParseArgsErrors(t *testing.T) {
	for _, c := range []struct {
		args []string
		want string
	}{
		{[]string{"--asset", "SPY", "--amount", "abc", "--position", "long"}, "argument --amount: not a USD amount: 'abc'"},
		{[]string{"--asset", "SPY", "--amount", "nan", "--position", "long"}, "argument --amount: not a USD amount: 'nan'"},
		{[]string{"--asset", "SPY", "--amount"}, "argument --amount: expected one argument"},
		{[]string{"--close", "--balance"}, "argument --close: expected one argument"},
		{[]string{"--balance", "extra"}, "unrecognized arguments: extra"},
		{[]string{"--balance", "--bal"}, "unrecognized arguments: --bal"},
		{[]string{"--balance=yes"}, "argument --balance: ignored explicit argument 'yes'"},
		{[]string{"--balance", "--position", "long"}, "--position is only used with --asset"},
		{[]string{"--balance", "--client_order_id", "x"}, "--client_order_id is only used with --asset or --close"},
		{[]string{"--asset", "SPY"}, "--asset requires --amount and --position"},
		{[]string{"--close", "a", "--asset", "SPY"}, "argument --asset: not allowed with argument --close"},
		{[]string{"--orders", "{}"}, "argument --orders: expected at least one client_order_id"},
	} {
		_, err := parseArgs(c.args)
		if err == nil || err.Error() != c.want {
			t.Errorf("parseArgs(%q) error = %v, want %q", c.args, err, c.want)
		}
	}
	if _, err := parseArgs([]string{"--balance", "-h"}); err != errHelp {
		t.Errorf("-h returned %v, want errHelp", err)
	}
}
