// Package trade is a thin wrapper around the Alpaca trading and market data APIs.
//
// Execution only: it opens positions sized in USD, looks orders up, and closes
// them by client_order_id. Deciding what to trade is left to the caller.
package trade

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/alpacahq/alpaca-trade-api-go/v3/alpaca"
	"github.com/alpacahq/alpaca-trade-api-go/v3/marketdata"
	"github.com/shopspring/decimal"

	"github.com/kmondesir/alpaca-bot/internal/config"
	"github.com/kmondesir/alpaca-bot/internal/symbols"
)

// Positions are the values OpenPosition accepts.
var Positions = []string{"long", "short", "call", "put"}

// MaxOrders is the most orders Alpaca returns per request.
const MaxOrders = 500

// doneStatuses are the states in which an order can no longer fill, so there
// is nothing to cancel. (done_for_day is not here: those orders resume the
// next trading day.)
var doneStatuses = []string{"filled", "canceled", "expired", "rejected", "replaced"}

// Error is a reason Trade refused or could not finish a request, as opposed
// to a failed connection. Callers report it, like an *alpaca.APIError, as the
// order's error.
type Error string

func (e Error) Error() string { return string(e) }

func errorf(format string, args ...any) error {
	return Error(fmt.Sprintf(format, args...))
}

func isDone(order *alpaca.Order) bool {
	return slices.Contains(doneStatuses, order.Status)
}

// Trade places, looks up and closes orders for one Alpaca account.
type Trade struct {
	client  *alpaca.Client
	data    *marketdata.Client
	key     string
	secret  string
	baseURL string
	http    *http.Client

	// CancelTimeout is how long CloseOrder waits for a cancel to settle, and
	// PollInterval how often it checks.
	CancelTimeout time.Duration
	PollInterval  time.Duration
}

// New returns a Trade for the account. baseURL is the trading API base URL
// (config.BaseURL), without /v2. dataURL is the market data URL; "" means
// Alpaca's.
func New(key, secret, baseURL, dataURL string) *Trade {
	return &Trade{
		client:        alpaca.NewClient(alpaca.ClientOpts{APIKey: key, APISecret: secret, BaseURL: baseURL}),
		data:          marketdata.NewClient(marketdata.ClientOpts{APIKey: key, APISecret: secret, BaseURL: dataURL}),
		key:           key,
		secret:        secret,
		baseURL:       strings.TrimRight(baseURL, "/"),
		http:          &http.Client{Timeout: 30 * time.Second},
		CancelTimeout: 10 * time.Second,
		PollInterval:  500 * time.Millisecond,
	}
}

// Balance is the account's cash, equity and buying power.
type Balance struct {
	Currency                 string  `json:"currency"`
	Cash                     float64 `json:"cash"`
	Equity                   float64 `json:"equity"`
	BuyingPower              float64 `json:"buying_power"`
	NonMarginableBuyingPower float64 `json:"non_marginable_buying_power"`
	OptionsBuyingPower       float64 `json:"options_buying_power"`
	PortfolioValue           float64 `json:"portfolio_value"`
}

// GetBalance returns the account's cash, equity and buying power.
//
// It reads /v2/account directly because the SDK's Account has no
// options_buying_power.
func (t *Trade) GetBalance() (Balance, error) {
	request, err := http.NewRequest(http.MethodGet, t.baseURL+"/v2/account", nil)
	if err != nil {
		return Balance{}, err
	}
	request.Header.Set("APCA-API-KEY-ID", t.key)
	request.Header.Set("APCA-API-SECRET-KEY", t.secret)
	response, err := t.http.Do(request)
	if err != nil {
		return Balance{}, err
	}
	defer response.Body.Close()
	if response.StatusCode >= http.StatusMultipleChoices {
		return Balance{}, alpaca.APIErrorFromResponse(response)
	}
	var account struct {
		Currency                 string           `json:"currency"`
		Cash                     decimal.Decimal  `json:"cash"`
		Equity                   decimal.Decimal  `json:"equity"`
		BuyingPower              decimal.Decimal  `json:"buying_power"`
		NonMarginableBuyingPower *decimal.Decimal `json:"non_marginable_buying_power"`
		OptionsBuyingPower       *decimal.Decimal `json:"options_buying_power"`
		PortfolioValue           decimal.Decimal  `json:"portfolio_value"`
	}
	if err := json.NewDecoder(response.Body).Decode(&account); err != nil {
		return Balance{}, err
	}
	return Balance{
		Currency:                 account.Currency,
		Cash:                     account.Cash.InexactFloat64(),
		Equity:                   account.Equity.InexactFloat64(),
		BuyingPower:              account.BuyingPower.InexactFloat64(),
		NonMarginableBuyingPower: orZero(account.NonMarginableBuyingPower),
		OptionsBuyingPower:       orZero(account.OptionsBuyingPower),
		PortfolioValue:           account.PortfolioValue.InexactFloat64(),
	}, nil
}

func orZero(value *decimal.Decimal) float64 {
	if value == nil {
		return 0
	}
	return value.InexactFloat64()
}

// GetOrders returns the most recent orders in any status, newest first.
func (t *Trade) GetOrders(limit int) ([]alpaca.Order, error) {
	return t.client.GetOrders(alpaca.GetOrdersRequest{Status: "all", Limit: min(limit, MaxOrders), Nested: false})
}

// GetOrder looks an order up by its client_order_id.
func (t *Trade) GetOrder(clientOrderID string) (*alpaca.Order, error) {
	return t.client.GetOrderByClientOrderID(clientOrderID)
}

// OpenPosition opens a position worth about amount USD and returns the submitted order.
//
// position is "long" or "short" for a stock or crypto pair, and "call" or
// "put" for an OCC option contract symbol (e.g. SPY261014C00580000), which is
// bought to open. clientOrderID defaults to a random UUID.
func (t *Trade) OpenPosition(asset string, amount float64, position, clientOrderID string) (*alpaca.Order, error) {
	symbol := symbols.Normalize(asset)
	position = strings.ToLower(position)
	if !slices.Contains(Positions, position) {
		return nil, errorf("position must be one of %s, not '%s'", strings.Join(Positions, ", "), position)
	}
	if amount <= 0 {
		return nil, Error("amount must be positive")
	}
	if clientOrderID == "" {
		clientOrderID = config.NewClientOrderID()
	}

	if position == "call" || position == "put" {
		if !symbols.IsOption(symbol) {
			return nil, errorf("%s needs an OCC option symbol such as SPY261014C00580000, not %s", position, symbol)
		}
		if kind := symbols.OptionType(symbol); kind != position {
			return nil, errorf("%s is a %s, not a %s", symbol, kind, position)
		}
		return t.buyOption(symbol, amount, clientOrderID)
	}
	if symbols.IsOption(symbol) {
		return nil, errorf("%s is an option contract; use --position %s", symbol, symbols.OptionType(symbol))
	}
	if position == "short" {
		return t.openShort(symbol, amount, clientOrderID)
	}

	notional := decimal.NewFromFloat(amount).RoundDown(2)
	if !notional.IsPositive() {
		return nil, Error("amount must be at least $0.01")
	}
	return t.submit(alpaca.PlaceOrderRequest{
		Symbol:        symbol,
		Notional:      &notional,
		Side:          alpaca.Buy,
		Type:          alpaca.Market,
		TimeInForce:   timeInForce(symbol),
		ClientOrderID: clientOrderID,
	})
}

// openShort sells short the whole shares amount buys at the bid; Alpaca
// rejects notional and fractional shorts.
func (t *Trade) openShort(symbol string, amount float64, clientOrderID string) (*alpaca.Order, error) {
	if symbols.IsCrypto(symbol) {
		return nil, Error("Crypto short selling is unsupported")
	}
	asset, err := t.client.GetAsset(symbol)
	if err != nil {
		return nil, err
	}
	if !asset.Shortable {
		return nil, errorf("%s is not shortable", symbol)
	}
	quote, err := t.data.GetLatestQuote(symbol, marketdata.GetLatestQuoteRequest{})
	if err != nil {
		return nil, err
	}
	bid := 0.0
	if quote != nil {
		bid = quote.BidPrice
	}
	if bid <= 0 {
		return nil, errorf("No valid bid price available to short %s", symbol)
	}
	// Float math, as in the Python version, so both size orders the same.
	qty := math.Floor(amount / bid)
	if qty <= 0 {
		return nil, errorf("$%s is too small to short one share of %s at %s", pyFloat(amount), symbol, pyFloat(bid))
	}
	shares := decimal.NewFromFloat(qty)
	return t.submit(alpaca.PlaceOrderRequest{
		Symbol:        symbol,
		Qty:           &shares,
		Side:          alpaca.Sell,
		Type:          alpaca.Market,
		TimeInForce:   alpaca.Day,
		ClientOrderID: clientOrderID,
	})
}

// buyOption buys the whole contracts amount covers with a day limit order at the ask.
func (t *Trade) buyOption(symbol string, amount float64, clientOrderID string) (*alpaca.Order, error) {
	quote, err := t.data.GetLatestOptionQuote(symbol, marketdata.GetLatestOptionQuoteRequest{})
	if err != nil {
		return nil, err
	}
	ask := 0.0
	if quote != nil {
		ask = quote.AskPrice
	}
	if ask <= 0 {
		return nil, errorf("No valid ask price available for %s", symbol)
	}
	qty := math.Floor(amount / (ask * 100))
	if qty < 1 {
		return nil, errorf("$%s is too small to buy one %s contract at %s ($%.2f)",
			pyFloat(amount), symbol, pyFloat(ask), ask*100)
	}
	contracts := decimal.NewFromFloat(qty)
	limit := roundToIncrement(ask, 0.01)
	return t.submit(alpaca.PlaceOrderRequest{
		Symbol:        symbol,
		Qty:           &contracts,
		Side:          alpaca.Buy,
		Type:          alpaca.Limit,
		TimeInForce:   alpaca.Day,
		LimitPrice:    &limit,
		ClientOrderID: clientOrderID,
	})
}

func (t *Trade) submit(request alpaca.PlaceOrderRequest) (*alpaca.Order, error) {
	order, err := t.client.PlaceOrder(request)
	if err != nil {
		return nil, err
	}
	size := "$" + decimalString(order.Notional)
	if order.Qty != nil {
		size = order.Qty.String()
	}
	slog.Info(fmt.Sprintf("Placed %s %s %s %s (%s, %s)", order.Type, order.Side, size, order.Symbol, order.ID, order.ClientOrderID))
	return order, nil
}

// CloseOrder closes the order whose client_order_id is orderID and returns the result.
//
// A working order is cancelled first. If nothing had filled, the cancelled
// order is returned. Otherwise the filled quantity is closed with an opposite
// market order, which is returned; clientOrderID tags it and defaults to a
// random UUID.
func (t *Trade) CloseOrder(orderID, clientOrderID string) (*alpaca.Order, error) {
	order, err := t.GetOrder(orderID)
	if err != nil {
		return nil, err
	}
	if !isDone(order) {
		if order.Status != "pending_cancel" {
			if err := t.client.CancelOrder(order.ID); err != nil {
				return nil, err
			}
		}
		if order, err = t.waitUntilDone(order.ID); err != nil {
			return nil, err
		}
		if !isDone(order) {
			return nil, errorf("Cancel of %s is still pending (%s); retry", orderID, order.Status)
		}
		slog.Info(fmt.Sprintf("Cancelled %s (%s)", order.ID, order.Status))
	}

	filled := order.FilledQty
	if filled.IsZero() {
		return order, nil
	}

	position, err := t.findPosition(order.Symbol)
	if err != nil {
		return nil, err
	}
	if position == nil {
		return nil, errorf("No open %s position left to close for %s", order.Symbol, orderID)
	}
	heldSide := strings.ToLower(position.Side)
	expectedSide, closingSide := "long", alpaca.Sell
	if order.Side != alpaca.Buy {
		expectedSide, closingSide = "short", alpaca.Buy
	}
	if heldSide != expectedSide {
		return nil, errorf("%s position is %s, so %s (%s) is already closed", order.Symbol, heldSide, orderID, order.Side)
	}
	// Crypto fees come out of the purchased coin, so less than filled may be held.
	qty := decimal.Min(filled, position.QtyAvailable.Abs())
	if !qty.IsPositive() {
		return nil, errorf("No available %s to close for %s; another order may hold it", order.Symbol, orderID)
	}
	if clientOrderID == "" {
		clientOrderID = config.NewClientOrderID()
	}

	closing, err := t.submit(alpaca.PlaceOrderRequest{
		Symbol:        order.Symbol,
		Qty:           &qty,
		Side:          closingSide,
		Type:          alpaca.Market,
		TimeInForce:   timeInForce(order.Symbol),
		ClientOrderID: clientOrderID,
	})
	if err != nil {
		return nil, err
	}
	slog.Info(fmt.Sprintf("Closing %s of %s for %s", qty, order.Symbol, orderID))
	return closing, nil
}

// waitUntilDone polls until the order reaches a final status or CancelTimeout passes.
func (t *Trade) waitUntilDone(id string) (*alpaca.Order, error) {
	deadline := time.Now().Add(t.CancelTimeout)
	order, err := t.client.GetOrder(id)
	for err == nil && !isDone(order) && time.Now().Before(deadline) {
		time.Sleep(t.PollInterval)
		order, err = t.client.GetOrder(id)
	}
	return order, err
}

// findPosition returns the open position for symbol, matching crypto pairs to
// Alpaca's unslashed form, or nil if there is none.
func (t *Trade) findPosition(symbol string) (*alpaca.Position, error) {
	target := symbols.Normalize(symbol)
	positions, err := t.client.GetPositions()
	if err != nil {
		return nil, err
	}
	for i, position := range positions {
		held := symbols.Normalize(position.Symbol)
		if position.AssetClass == alpaca.Crypto {
			held = symbols.CryptoPair(position.Symbol)
		}
		if held == target {
			return &positions[i], nil
		}
	}
	return nil, nil
}

func timeInForce(symbol string) alpaca.TimeInForce {
	if symbols.IsCrypto(symbol) {
		return alpaca.GTC
	}
	return alpaca.Day
}

// roundToIncrement rounds price half up to a multiple of increment.
func roundToIncrement(price, increment float64) decimal.Decimal {
	step := decimal.NewFromFloat(increment)
	return decimal.NewFromFloat(price).Div(step).Round(0).Mul(step)
}

func decimalString(value *decimal.Decimal) string {
	if value == nil {
		return "None"
	}
	return value.String()
}

// pyFloat formats f the way Python's str(float) does for ordinary values:
// 30 becomes "30.0", 12.07 stays "12.07".
func pyFloat(f float64) string {
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.ContainsAny(s, ".eEn") {
		s += ".0"
	}
	return s
}
