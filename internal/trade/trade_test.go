package trade

import (
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/alpacahq/alpaca-trade-api-go/v3/alpaca"
)

func TestGetBalance(t *testing.T) {
	fake, trade := newFake(t)
	fake.account = map[string]any{
		"currency": "USD", "cash": "9797.83", "equity": "9797.83", "buying_power": "39191.32",
		"non_marginable_buying_power": "9797.83", "options_buying_power": nil, "portfolio_value": "9797.83",
	}
	got, err := trade.GetBalance()
	if err != nil {
		t.Fatal(err)
	}
	want := Balance{Currency: "USD", Cash: 9797.83, Equity: 9797.83, BuyingPower: 39191.32,
		NonMarginableBuyingPower: 9797.83, OptionsBuyingPower: 0, PortfolioValue: 9797.83}
	if got != want {
		t.Errorf("GetBalance = %+v, want %+v", got, want)
	}
}

func TestGetBalanceAPIError(t *testing.T) {
	_, trade := newFake(t)
	trade.key = "wrong"
	_, err := trade.GetBalance()
	var apiErr *alpaca.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusUnauthorized ||
		apiErr.Body != `{"code":40110000,"message":"request is not authorized"}` {
		t.Errorf("err = %#v, want a 401 APIError with the response body", err)
	}
}

func TestGetOrders(t *testing.T) {
	fake, trade := newFake(t)
	for range 3 {
		fake.addOrder(nil)
	}
	orders, err := trade.GetOrders(2)
	if err != nil {
		t.Fatal(err)
	}
	if len(orders) != 2 || orders[0].ID != "order-3" || orders[1].ID != "order-2" {
		t.Errorf("GetOrders(2) = %d orders starting %q, want the newest 2", len(orders), orders[0].ID)
	}
	if orders, _ := trade.GetOrders(10_000); len(orders) != 3 {
		t.Errorf("GetOrders(10000) returned %d orders, want all 3", len(orders))
	}
}

func TestGetOrderNotFound(t *testing.T) {
	_, trade := newFake(t)
	_, err := trade.GetOrder("missing")
	var apiErr *alpaca.APIError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != http.StatusNotFound ||
		apiErr.Body != `{"code":40410000,"message":"order not found for missing"}` {
		t.Errorf("err = %#v, want a 404 APIError", err)
	}
}

func TestOpenLong(t *testing.T) {
	fake, trade := newFake(t)
	order, err := trade.OpenPosition(" f ", 5.009, "LONG", "tag-1")
	if err != nil {
		t.Fatal(err)
	}
	if order.ClientOrderID != "tag-1" || order.Notional.String() != "5" {
		t.Errorf("order = %s $%s, want tag-1 $5", order.ClientOrderID, order.Notional)
	}
	want := map[string]any{"symbol": "F", "notional": "5", "side": "buy", "type": "market", "time_in_force": "day", "qty": nil}
	checkPlaced(t, fake, want)
}

func TestOpenLongCrypto(t *testing.T) {
	fake, trade := newFake(t)
	order, err := trade.OpenPosition("btc", 10, "long", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(order.ClientOrderID) != 36 {
		t.Errorf("client_order_id = %q, want a generated UUID", order.ClientOrderID)
	}
	checkPlaced(t, fake, map[string]any{"symbol": "BTC/USD", "notional": "10", "time_in_force": "gtc"})
}

func TestOpenShort(t *testing.T) {
	fake, trade := newFake(t)
	fake.assets["F"] = map[string]any{"symbol": "F", "shortable": true, "fractionable": true}
	fake.quotes["F"] = quote(12.07, 12.08)
	if _, err := trade.OpenPosition("F", 30, "short", "tag-1"); err != nil {
		t.Fatal(err)
	}
	// Whole shares only, even though F is fractionable.
	checkPlaced(t, fake, map[string]any{"symbol": "F", "qty": "2", "side": "sell", "type": "market", "time_in_force": "day"})
}

func TestOpenPositionErrors(t *testing.T) {
	fake, trade := newFake(t)
	fake.assets["F"] = map[string]any{"symbol": "F", "shortable": true}
	fake.assets["GME"] = map[string]any{"symbol": "GME", "shortable": false}
	fake.assets["XYZ"] = map[string]any{"symbol": "XYZ", "shortable": true}
	fake.quotes["F"] = quote(12.07, 12.08)
	fake.quotes["XYZ"] = quote(0, 1)
	fake.quotes["F261016C00014000"] = quote(0, 0.03)
	fake.quotes["F261016C00015000"] = quote(0, 0)

	for _, c := range []struct {
		asset    string
		amount   float64
		position string
		want     string
	}{
		{"F", 5, "sideways", "position must be one of long, short, call, put, not 'sideways'"},
		{"F", 0, "long", "amount must be positive"},
		{"F", 0.004, "long", "amount must be at least $0.01"},
		{"F", 5, "call", "call needs an OCC option symbol such as SPY261014C00580000, not F"},
		{"F261016C00014000", 5, "put", "F261016C00014000 is a call, not a put"},
		{"F261016C00014000", 5, "long", "F261016C00014000 is an option contract; use --position call"},
		{"BTC/USD", 10, "short", "Crypto short selling is unsupported"},
		{"GME", 100, "short", "GME is not shortable"},
		{"XYZ", 100, "short", "No valid bid price available to short XYZ"},
		{"F", 5, "short", "$5.0 is too small to short one share of F at 12.07"},
		// Same message as the recorded Python output (testdata/python/07-open-default-id-error.json).
		{"F261016C00014000", 0.01, "call", "$0.01 is too small to buy one F261016C00014000 contract at 0.03 ($3.00)"},
		{"F261016C00015000", 5, "call", "No valid ask price available for F261016C00015000"},
		{"F261016C00016000", 5, "call", "No valid ask price available for F261016C00016000"}, // no quote at all
	} {
		_, err := trade.OpenPosition(c.asset, c.amount, c.position, "tag")
		if err == nil || err.Error() != c.want {
			t.Errorf("OpenPosition(%q, %v, %q) error = %v, want %q", c.asset, c.amount, c.position, err, c.want)
		}
	}
	if len(fake.placed) != 0 {
		t.Errorf("placed %d orders, want none", len(fake.placed))
	}
}

func TestBuyOption(t *testing.T) {
	fake, trade := newFake(t)
	fake.quotes["F261016C00014000"] = quote(0.01, 0.03)
	if _, err := trade.OpenPosition("f261016c00014000", 5, "Call", "tag-1"); err != nil {
		t.Fatal(err)
	}
	// floor(5 / 3.0000000000000004) = 1 contract, as in Python.
	checkPlaced(t, fake, map[string]any{
		"symbol": "F261016C00014000", "qty": "1", "side": "buy", "type": "limit", "time_in_force": "day", "limit_price": "0.03",
	})
}

func TestRoundToIncrement(t *testing.T) {
	for price, want := range map[float64]string{0.03: "0.03", 1.005: "1.01", 2.344: "2.34", 2.345: "2.35", 7: "7"} {
		if got := roundToIncrement(price, 0.01).String(); got != want {
			t.Errorf("roundToIncrement(%v) = %s, want %s", price, got, want)
		}
	}
}

func TestPyFloat(t *testing.T) {
	for value, want := range map[float64]string{30: "30.0", 12.07: "12.07", 0.01: "0.01", 1000: "1000.0"} {
		if got := pyFloat(value); got != want {
			t.Errorf("pyFloat(%v) = %q, want %q", value, got, want)
		}
	}
}

func TestCloseUnfilledOrderCancels(t *testing.T) {
	fake, trade := newFake(t)
	working := fake.addOrder(map[string]any{"client_order_id": "tag-1", "symbol": "F", "side": "buy", "notional": "5"})
	order, err := trade.CloseOrder("tag-1", "")
	if err != nil {
		t.Fatal(err)
	}
	if order.Status != "canceled" || order.ID != working["id"] {
		t.Errorf("CloseOrder returned %s %s, want the cancelled order", order.ID, order.Status)
	}
	if len(fake.cancelled) != 1 || len(fake.placed) != 0 {
		t.Errorf("cancelled %v and placed %d, want one cancel and no orders", fake.cancelled, len(fake.placed))
	}
}

func TestClosePendingCancelWaitsWithoutCancelling(t *testing.T) {
	fake, trade := newFake(t)
	order := fake.addOrder(map[string]any{"client_order_id": "tag-1", "symbol": "F", "side": "buy", "status": "pending_cancel"})
	go func() {
		time.Sleep(50 * time.Millisecond) // after CloseOrder's first lookup
		fake.mu.Lock()
		order["status"] = "canceled"
		fake.mu.Unlock()
	}()
	got, err := trade.CloseOrder("tag-1", "")
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != "canceled" || len(fake.cancelled) != 0 {
		t.Errorf("status %s after %d cancels, want canceled after none", got.Status, len(fake.cancelled))
	}
}

func TestCloseCancelStillPending(t *testing.T) {
	fake, trade := newFake(t)
	fake.onCancel = func(order map[string]any) { order["status"] = "pending_cancel" }
	fake.addOrder(map[string]any{"client_order_id": "tag-1", "symbol": "F", "side": "buy"})
	_, err := trade.CloseOrder("tag-1", "")
	if err == nil || err.Error() != "Cancel of tag-1 is still pending (pending_cancel); retry" {
		t.Errorf("err = %v", err)
	}
}

func TestClosePartlyFilledOrder(t *testing.T) {
	fake, trade := newFake(t)
	fake.onCancel = func(order map[string]any) { order["status"] = "canceled"; order["filled_qty"] = "1" }
	fake.addOrder(map[string]any{"client_order_id": "tag-1", "symbol": "F", "side": "buy", "qty": "3"})
	fake.positions = []map[string]any{{"symbol": "F", "asset_class": "us_equity", "side": "long", "qty": "1", "qty_available": "1"}}
	order, err := trade.CloseOrder("tag-1", "close-1")
	if err != nil {
		t.Fatal(err)
	}
	if order.ClientOrderID != "close-1" || order.Side != alpaca.Sell {
		t.Errorf("closing order = %s %s, want close-1 sell", order.ClientOrderID, order.Side)
	}
	checkPlaced(t, fake, map[string]any{"symbol": "F", "qty": "1", "side": "sell", "type": "market", "time_in_force": "day"})
}

func TestCloseFilledCryptoCapsAtAvailable(t *testing.T) {
	fake, trade := newFake(t)
	fake.addOrder(map[string]any{
		"client_order_id": "tag-1", "symbol": "BTC/USD", "side": "buy", "status": "filled",
		"filled_qty": "0.000120061", "asset_class": "crypto", "time_in_force": "gtc",
	})
	// Alpaca drops the slash from crypto positions, and fees come out of the coin.
	fake.positions = []map[string]any{{"symbol": "BTCUSD", "asset_class": "crypto", "side": "long", "qty": "0.00011976", "qty_available": "0.00011976"}}
	if _, err := trade.CloseOrder("tag-1", ""); err != nil {
		t.Fatal(err)
	}
	if len(fake.cancelled) != 0 {
		t.Error("cancelled a filled order")
	}
	checkPlaced(t, fake, map[string]any{"symbol": "BTC/USD", "qty": "0.00011976", "side": "sell", "time_in_force": "gtc"})
}

func TestCloseFilledShortBuysBack(t *testing.T) {
	fake, trade := newFake(t)
	fake.addOrder(map[string]any{"client_order_id": "tag-1", "symbol": "F", "side": "sell", "status": "filled", "filled_qty": "2"})
	fake.positions = []map[string]any{{"symbol": "F", "asset_class": "us_equity", "side": "short", "qty": "-2", "qty_available": "-2"}}
	if _, err := trade.CloseOrder("tag-1", ""); err != nil {
		t.Fatal(err)
	}
	checkPlaced(t, fake, map[string]any{"symbol": "F", "qty": "2", "side": "buy", "time_in_force": "day"})
}

func TestCloseErrors(t *testing.T) {
	for _, c := range []struct {
		name     string
		side     string
		position map[string]any
		want     string
	}{
		{"position gone", "buy", nil, "No open F position left to close for tag-1"},
		{"other side", "buy", map[string]any{"symbol": "F", "side": "short", "qty_available": "-2"},
			"F position is short, so tag-1 (buy) is already closed"},
		{"held by another order", "buy", map[string]any{"symbol": "F", "side": "long", "qty_available": "0"},
			"No available F to close for tag-1; another order may hold it"},
	} {
		t.Run(c.name, func(t *testing.T) {
			fake, trade := newFake(t)
			fake.addOrder(map[string]any{"client_order_id": "tag-1", "symbol": "F", "side": c.side, "status": "filled", "filled_qty": "2"})
			if c.position != nil {
				c.position["asset_class"] = "us_equity"
				fake.positions = []map[string]any{c.position}
			}
			_, err := trade.CloseOrder("tag-1", "")
			if err == nil || err.Error() != c.want {
				t.Errorf("err = %v, want %q", err, c.want)
			}
			if len(fake.placed) != 0 {
				t.Error("placed a closing order")
			}
		})
	}
}

// checkPlaced checks the one order placed against want's fields.
func checkPlaced(t *testing.T, fake *fakeAlpaca, want map[string]any) {
	t.Helper()
	if len(fake.placed) != 1 {
		t.Fatalf("placed %d orders, want 1", len(fake.placed))
	}
	for key, value := range want {
		if got := fake.placed[0][key]; got != value {
			t.Errorf("placed %s = %#v, want %#v", key, got, value)
		}
	}
	if id, _ := fake.placed[0]["client_order_id"].(string); strings.TrimSpace(id) == "" {
		t.Error("placed order has no client_order_id")
	}
}
