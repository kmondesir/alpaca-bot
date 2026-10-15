package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/url"
	"reflect"
	"strings"
	"testing"

	"github.com/alpacahq/alpaca-trade-api-go/v3/alpaca"

	"github.com/kmondesir/alpaca-bot/internal/config"
	"github.com/kmondesir/alpaca-bot/internal/trade"
)

// TestOrderJSONMatchesPython decodes every order the Python version printed,
// prints it with toOrderJSON, and requires the same keys in the same order
// with the same values, apart from expires_at.
func TestOrderJSONMatchesPython(t *testing.T) {
	checked := 0
	for name, r := range loadRecordings(t) {
		var items []json.RawMessage
		if json.Unmarshal(r.Stdout, &items) != nil {
			continue
		}
		for _, raw := range items {
			if !bytes.Contains(raw, []byte(`"symbol"`)) {
				continue
			}
			checked++
			var order alpaca.Order
			if err := json.Unmarshal(raw, &order); err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			got, err := json.Marshal(toOrderJSON(order))
			if err != nil {
				t.Fatal(err)
			}

			wantKeys := without(objectKeys(t, raw), "expires_at")
			if gotKeys := objectKeys(t, got); !reflect.DeepEqual(gotKeys, wantKeys) {
				t.Errorf("%s: keys\n got %v\nwant %v", name, gotKeys, wantKeys)
			}
			var gotValues, wantValues map[string]any
			json.Unmarshal(got, &gotValues)
			json.Unmarshal(raw, &wantValues)
			delete(wantValues, "expires_at")
			for key, want := range wantValues {
				if !reflect.DeepEqual(gotValues[key], want) {
					t.Errorf("%s: %s = %#v, want %#v", name, key, gotValues[key], want)
				}
			}
		}
	}
	if checked < 20 {
		t.Errorf("checked %d recorded orders, want at least 20", checked)
	}
}

func TestEmptyOrderClassIsSimple(t *testing.T) {
	if got := toOrderJSON(alpaca.Order{}).OrderClass; got == nil || *got != "simple" {
		t.Errorf("order_class = %v, want simple", got)
	}
}

func objectKeys(t *testing.T, raw []byte) []string {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.Token() // {
	var keys []string
	for decoder.More() {
		key, _ := decoder.Token()
		keys = append(keys, key.(string))
		var skip json.RawMessage
		if err := decoder.Decode(&skip); err != nil {
			t.Fatal(err)
		}
	}
	return keys
}

func without(keys []string, drop string) []string {
	var kept []string
	for _, key := range keys {
		if key != drop {
			kept = append(kept, key)
		}
	}
	return kept
}

func TestErrorMessageMatchesPython(t *testing.T) {
	body := `{"code":40410000,"message":"order not found for p0-1792029226-missing"}`
	err := &alpaca.APIError{StatusCode: 404, Code: 40410000, Message: "order not found for p0-1792029226-missing", Body: body}
	// As recorded in testdata/python/09-orders-missing-id.json.
	if got, want := errorMessage(err), "404: "+body; got != want {
		t.Errorf("errorMessage = %q, want %q", got, want)
	}
	if got := errorMessage(trade.Error("Crypto short selling is unsupported")); got != "Crypto short selling is unsupported" {
		t.Errorf("errorMessage = %q", got)
	}
}

func TestPrintJSONMatchesPythonLayout(t *testing.T) {
	var out bytes.Buffer
	value := []any{errorEntry{"a<b>&", "café \U0001F600"}, map[string]any{"cash": 9797.83, "n": nil, "l": []any{}}}
	if err := printJSON(&out, value); err != nil {
		t.Fatal(err)
	}
	// print(json.dumps(value, indent=2)) in Python, with a dict's keys in sorted order.
	want := "[\n  {\n    \"id\": \"a<b>&\",\n    \"error\": \"caf\\u00e9 \\ud83d\\ude00\"\n  },\n" +
		"  {\n    \"cash\": 9797.83,\n    \"l\": [],\n    \"n\": null\n  }\n]\n"
	if got := strings.ReplaceAll(out.String(), "\r\n", "\n"); got != want {
		t.Errorf("printJSON wrote\n%s\nwant\n%s", got, want)
	}
}

// stub is a trader with canned answers.
type stub struct {
	orders map[string]*alpaca.Order
	err    error // returned by every call when set
	opened []string
}

func (s *stub) GetBalance() (trade.Balance, error) {
	return trade.Balance{Currency: "USD", Cash: 10}, s.err
}

func (s *stub) GetOrders(int) ([]alpaca.Order, error) {
	return nil, s.err
}

func (s *stub) GetOrder(id string) (*alpaca.Order, error) {
	if s.err != nil {
		return nil, s.err
	}
	if order, ok := s.orders[id]; ok {
		return order, nil
	}
	return nil, &alpaca.APIError{StatusCode: 404, Body: `{"code":40410000,"message":"order not found for ` + id + `"}`}
}

func (s *stub) OpenPosition(asset string, _ float64, position, id string) (*alpaca.Order, error) {
	s.opened = append(s.opened, id)
	if s.err != nil {
		return nil, s.err
	}
	return &alpaca.Order{ID: "order-1", ClientOrderID: id, Symbol: asset, Status: "accepted"}, nil
}

func (s *stub) CloseOrder(id, _ string) (*alpaca.Order, error) {
	return s.GetOrder(id)
}

// runWith runs execute with the stub and returns the exit code, stdout and stderr.
func runWith(t *testing.T, s *stub, args ...string) (int, string, string) {
	t.Helper()
	t.Setenv("ALPACA_API_KEY", "key")
	t.Setenv("ALPACA_SECRET_KEY", "secret")
	t.Setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
	var stdout, stderr bytes.Buffer
	args = append(args, "--logdirectory", t.TempDir())
	code := run(args, t.TempDir(), &stdout, &stderr, func(config.Config) trader { return s })
	return code, strings.ReplaceAll(stdout.String(), "\r\n", "\n"), stderr.String()
}

func TestRunBalance(t *testing.T) {
	code, stdout, stderr := runWith(t, &stub{}, "--balance")
	if code != 0 || !strings.Contains(stdout, `"cash": 10,`) || !strings.Contains(stderr, "[Starting execute (demo=True, url=https://paper-api.alpaca.markets)]") {
		t.Errorf("exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
}

func TestRunRecentOrdersEmptyIsList(t *testing.T) {
	if code, stdout, _ := runWith(t, &stub{}, "--orders"); code != 0 || stdout != "[]\n" {
		t.Errorf("exit %d, stdout %q; want 0 and []", code, stdout)
	}
}

func TestRunOrdersReportsEachFailure(t *testing.T) {
	s := &stub{orders: map[string]*alpaca.Order{"a": {ID: "1", ClientOrderID: "a", Status: "filled"}}}
	code, stdout, _ := runWith(t, s, "--orders", "a,missing")
	var got []map[string]any
	if err := json.Unmarshal([]byte(stdout), &got); err != nil {
		t.Fatal(err)
	}
	if code != 1 || len(got) != 2 || got[0]["client_order_id"] != "a" ||
		got[1]["id"] != "missing" || got[1]["error"] != `404: {"code":40410000,"message":"order not found for missing"}` {
		t.Errorf("exit %d, result %v", code, got)
	}
}

func TestRunOpenFailureUsesClientOrderID(t *testing.T) {
	s := &stub{err: trade.Error("Crypto short selling is unsupported")}
	code, stdout, _ := runWith(t, s, "--asset", "BTC/USD", "--amount", "10", "--position", "short")
	want := "[\n  {\n    \"id\": \"" + s.opened[0] + "\",\n    \"error\": \"Crypto short selling is unsupported\"\n  }\n]\n"
	if code != 1 || stdout != want || len(s.opened[0]) != 36 {
		t.Errorf("exit %d, stdout %q; want 1 and %q", code, stdout, want)
	}
}

func TestRunConnectionErrorAbortsWithoutResult(t *testing.T) {
	s := &stub{err: &url.Error{Op: "Post", URL: "https://paper-api.alpaca.markets/v2/orders", Err: errors.New("connection reset")}}
	code, stdout, stderr := runWith(t, s, "--asset", "F", "--amount", "5", "--position", "long", "--client_order_id", "tag")
	if code != 1 || stdout != "" || !strings.Contains(stderr, "Uncaught error; run aborted") {
		t.Errorf("exit %d, stdout %q, stderr %q; want 1, no result, and a logged abort", code, stdout, stderr)
	}
}

func TestRunMissingSettings(t *testing.T) {
	t.Setenv("ALPACA_BASE_URL", "")
	var stdout, stderr bytes.Buffer
	t.Setenv("ALPACA_API_KEY", "key")
	t.Setenv("ALPACA_SECRET_KEY", "secret")
	code := run([]string{"--balance", "--logdirectory", t.TempDir()}, t.TempDir(), &stdout, &stderr,
		func(config.Config) trader { t.Fatal("created a trader without settings"); return nil })
	if code != 1 || stdout.Len() != 0 || !strings.Contains(stderr.String(), "[Missing required .env variables: ALPACA_BASE_URL; exiting]") {
		t.Errorf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestRunInvalidArguments(t *testing.T) {
	code, stdout, stderr := runWith(t, &stub{}, "--amount", "5")
	if code != 2 || stdout != "" || !strings.HasSuffix(stderr, "execute: error: one of the arguments --balance --orders --close --asset is required\n") {
		t.Errorf("exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
}

func TestRunHelp(t *testing.T) {
	code, stdout, _ := runWith(t, &stub{}, "--help")
	if code != 0 || !strings.Contains(stdout, "show this help message and exit") {
		t.Errorf("exit %d, stdout %q", code, stdout)
	}
}
