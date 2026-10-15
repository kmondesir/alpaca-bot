package trade

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeAlpaca serves the parts of the Alpaca trading and market data APIs
// that Trade uses, from in-memory state.
type fakeAlpaca struct {
	mu        sync.Mutex
	account   map[string]any
	orders    []map[string]any // newest last
	positions []map[string]any
	assets    map[string]map[string]any
	quotes    map[string]map[string]any // stock and option latest quotes by symbol
	// onCancel changes an order when it is cancelled; by default it becomes canceled.
	onCancel func(order map[string]any)
	// placed and cancelled record the requests Trade made.
	placed    []map[string]any
	cancelled []string
	nextID    int
}

func newFake(t *testing.T) (*fakeAlpaca, *Trade) {
	t.Helper()
	fake := &fakeAlpaca{
		assets: map[string]map[string]any{},
		quotes: map[string]map[string]any{},
		onCancel: func(order map[string]any) {
			order["status"] = "canceled"
		},
	}
	server := httptest.NewServer(http.HandlerFunc(fake.serve))
	t.Cleanup(server.Close)
	trade := New("key", "secret", server.URL, server.URL)
	trade.CancelTimeout = 200 * time.Millisecond
	trade.PollInterval = 10 * time.Millisecond
	return fake, trade
}

// addOrder stores an order and returns it; fields override the defaults.
func (f *fakeAlpaca) addOrder(fields map[string]any) map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.store(fields)
}

func (f *fakeAlpaca) store(fields map[string]any) map[string]any {
	f.nextID++
	order := map[string]any{
		"id":            fmt.Sprintf("order-%d", f.nextID),
		"created_at":    time.Date(2026, 10, 15, 14, 0, f.nextID, 0, time.UTC).Format(time.RFC3339Nano),
		"status":        "accepted",
		"type":          "market",
		"order_type":    "market",
		"time_in_force": "day",
		"filled_qty":    "0",
		"asset_class":   "us_equity",
	}
	for key, value := range fields {
		order[key] = value
	}
	f.orders = append(f.orders, order)
	return order
}

func (f *fakeAlpaca) findOrder(key, value string) map[string]any {
	for _, order := range f.orders {
		if order[key] == value {
			return order
		}
	}
	return nil
}

func (f *fakeAlpaca) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Header.Get("APCA-API-KEY-ID") != "key" || r.Header.Get("APCA-API-SECRET-KEY") != "secret" {
		writeError(w, http.StatusUnauthorized, 40110000, "request is not authorized")
		return
	}
	path, query := r.URL.Path, r.URL.Query()
	switch {
	case r.Method == http.MethodGet && path == "/v2/account":
		writeJSON(w, f.account)

	case r.Method == http.MethodGet && path == "/v2/orders":
		if query.Get("status") != "all" || query.Get("nested") != "false" {
			writeError(w, http.StatusBadRequest, 0, "unexpected query "+r.URL.RawQuery)
			return
		}
		limit := 50
		fmt.Sscan(query.Get("limit"), &limit)
		var newest []map[string]any
		for i := len(f.orders) - 1; i >= 0 && len(newest) < limit; i-- {
			newest = append(newest, f.orders[i])
		}
		writeJSON(w, newest)

	case r.Method == http.MethodGet && path == "/v2/orders:by_client_order_id":
		id := query.Get("client_order_id")
		if order := f.findOrder("client_order_id", id); order != nil {
			writeJSON(w, order)
		} else {
			writeError(w, http.StatusNotFound, 40410000, "order not found for "+id)
		}

	case strings.HasPrefix(path, "/v2/orders/"):
		id := strings.TrimPrefix(path, "/v2/orders/")
		order := f.findOrder("id", id)
		if order == nil {
			writeError(w, http.StatusNotFound, 40410000, "order not found")
			return
		}
		if r.Method == http.MethodDelete {
			f.cancelled = append(f.cancelled, id)
			f.onCancel(order)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		writeJSON(w, order)

	case r.Method == http.MethodPost && path == "/v2/orders":
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			writeError(w, http.StatusBadRequest, 0, err.Error())
			return
		}
		f.placed = append(f.placed, request)
		order := f.store(map[string]any{
			"client_order_id": request["client_order_id"],
			"symbol":          request["symbol"],
			"side":            request["side"],
			"type":            request["type"],
			"order_type":      request["type"],
			"time_in_force":   request["time_in_force"],
			"qty":             request["qty"],
			"notional":        request["notional"],
			"limit_price":     request["limit_price"],
		})
		writeJSON(w, order)

	case r.Method == http.MethodGet && path == "/v2/positions":
		writeJSON(w, f.positions)

	case r.Method == http.MethodGet && strings.HasPrefix(path, "/v2/assets/"):
		symbol := strings.TrimPrefix(path, "/v2/assets/")
		if asset, ok := f.assets[symbol]; ok {
			writeJSON(w, asset)
		} else {
			writeError(w, http.StatusNotFound, 40410000, "asset not found for "+symbol)
		}

	case r.Method == http.MethodGet && (path == "/v2/stocks/quotes/latest" || path == "/v1beta1/options/quotes/latest"):
		quotes := map[string]any{}
		for _, symbol := range strings.Split(query.Get("symbols"), ",") {
			if quote, ok := f.quotes[symbol]; ok {
				quotes[symbol] = quote
			}
		}
		writeJSON(w, map[string]any{"quotes": quotes})

	default:
		writeError(w, http.StatusNotFound, 0, "no fake for "+r.Method+" "+path)
	}
}

func writeJSON(w http.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status, code int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	fmt.Fprintf(w, `{"code":%d,"message":%q}`, code, message)
}

func quote(bid, ask float64) map[string]any {
	return map[string]any{"t": "2026-10-15T14:00:00Z", "bp": bid, "ap": ask}
}
