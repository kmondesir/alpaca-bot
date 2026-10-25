package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"runtime"
	"strings"
	"time"
	"unicode/utf16"

	"github.com/alpacahq/alpaca-trade-api-go/v3/alpaca"
	"github.com/shopspring/decimal"
)

// orderJSON is an order as the Python version printed it (alpaca-py's Order
// model), with the same keys in the same order. expires_at is left out: the
// Go SDK doesn't read it, and no caller uses it.
type orderJSON struct {
	ID             string      `json:"id"`
	ClientOrderID  string      `json:"client_order_id"`
	CreatedAt      *string     `json:"created_at"`
	UpdatedAt      *string     `json:"updated_at"`
	SubmittedAt    *string     `json:"submitted_at"`
	FilledAt       *string     `json:"filled_at"`
	ExpiredAt      *string     `json:"expired_at"`
	CanceledAt     *string     `json:"canceled_at"`
	FailedAt       *string     `json:"failed_at"`
	ReplacedAt     *string     `json:"replaced_at"`
	ReplacedBy     *string     `json:"replaced_by"`
	Replaces       *string     `json:"replaces"`
	AssetID        *string     `json:"asset_id"`
	Symbol         string      `json:"symbol"`
	AssetClass     *string     `json:"asset_class"`
	Notional       *string     `json:"notional"`
	Qty            *string     `json:"qty"`
	FilledQty      *string     `json:"filled_qty"`
	FilledAvgPrice *string     `json:"filled_avg_price"`
	OrderClass     *string     `json:"order_class"`
	OrderType      *string     `json:"order_type"`
	Type           *string     `json:"type"`
	Side           *string     `json:"side"`
	TimeInForce    *string     `json:"time_in_force"`
	LimitPrice     *string     `json:"limit_price"`
	StopPrice      *string     `json:"stop_price"`
	Status         string      `json:"status"`
	ExtendedHours  bool        `json:"extended_hours"`
	Legs           []orderJSON `json:"legs"`
	TrailPercent   *string     `json:"trail_percent"`
	TrailPrice     *string     `json:"trail_price"`
	HWM            *string     `json:"hwm"`
	PositionIntent *string     `json:"position_intent"`
	RatioQty       *string     `json:"ratio_qty"`
}

func toOrderJSON(order alpaca.Order) orderJSON {
	// The API sends an empty order_class for simple orders; alpaca-py printed "simple".
	orderClass := string(order.OrderClass)
	if orderClass == "" {
		orderClass = string(alpaca.Simple)
	}
	var legs []orderJSON
	for _, leg := range order.Legs {
		legs = append(legs, toOrderJSON(leg))
	}
	return orderJSON{
		ID:             order.ID,
		ClientOrderID:  order.ClientOrderID,
		CreatedAt:      timeString(&order.CreatedAt),
		UpdatedAt:      timeString(&order.UpdatedAt),
		SubmittedAt:    timeString(&order.SubmittedAt),
		FilledAt:       timeString(order.FilledAt),
		ExpiredAt:      timeString(order.ExpiredAt),
		CanceledAt:     timeString(order.CanceledAt),
		FailedAt:       timeString(order.FailedAt),
		ReplacedAt:     timeString(order.ReplacedAt),
		ReplacedBy:     order.ReplacedBy,
		Replaces:       order.Replaces,
		AssetID:        text(order.AssetID),
		Symbol:         order.Symbol,
		AssetClass:     text(string(order.AssetClass)),
		Notional:       decimalString(order.Notional),
		Qty:            decimalString(order.Qty),
		FilledQty:      decimalString(&order.FilledQty),
		FilledAvgPrice: decimalString(order.FilledAvgPrice),
		OrderClass:     text(orderClass),
		OrderType:      text(string(order.Type)),
		Type:           text(string(order.Type)),
		Side:           text(string(order.Side)),
		TimeInForce:    text(string(order.TimeInForce)),
		LimitPrice:     decimalString(order.LimitPrice),
		StopPrice:      decimalString(order.StopPrice),
		Status:         order.Status,
		ExtendedHours:  order.ExtendedHours,
		Legs:           legs,
		TrailPercent:   decimalString(order.TrailPercent),
		TrailPrice:     decimalString(order.TrailPrice),
		HWM:            decimalString(order.HWM),
		PositionIntent: text(string(order.PositionIntent)),
		RatioQty:       decimalString(order.RatioQty),
	}
}

// timeString formats t as pydantic did: UTC, microseconds only when non-zero,
// and null for a missing time.
func timeString(t *time.Time) *string {
	if t == nil || t.IsZero() {
		return nil
	}
	utc := t.UTC()
	s := utc.Format("2006-01-02T15:04:05")
	if micro := utc.Nanosecond() / 1000; micro != 0 {
		s += fmt.Sprintf(".%06d", micro)
	}
	s += "Z"
	return &s
}

func decimalString(value *decimal.Decimal) *string {
	if value == nil {
		return nil
	}
	s := value.String()
	return &s
}

// text returns nil for an empty string, which the API sent as null.
func text(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// errorEntry is how a failed id appears in a list of orders.
type errorEntry struct {
	ID    string `json:"id"`
	Error string `json:"error"`
}

// errorMessage formats an error as the Python version did: an Alpaca API
// error as "<status>: <response body>".
func errorMessage(err error) string {
	var apiErr *alpaca.APIError
	if errors.As(err, &apiErr) {
		return fmt.Sprintf("%d: %s", apiErr.StatusCode, apiBody(apiErr))
	}
	return err.Error()
}

// apiBody is the error's response body, or its message when there is none.
func apiBody(apiErr *alpaca.APIError) string {
	if apiErr.Body != "" {
		return apiErr.Body
	}
	return apiErr.Message
}

// printJSON writes value as Python's print(json.dumps(value, indent=2)) did:
// two-space indent, non-ASCII escaped, HTML characters left alone, and line
// endings in the platform's text mode.
func printJSON(w io.Writer, value any) error {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		return err
	}
	out := asciiOnly(buffer.String())
	if runtime.GOOS == "windows" {
		out = strings.ReplaceAll(out, "\n", "\r\n")
	}
	_, err := io.WriteString(w, out)
	return err
}

// asciiOnly escapes non-ASCII characters as \uXXXX, as json.dumps does by default.
func asciiOnly(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r < 0x80:
			b.WriteRune(r)
		case r > 0xFFFF:
			high, low := utf16.EncodeRune(r)
			fmt.Fprintf(&b, `\u%04x\u%04x`, high, low)
		default:
			fmt.Fprintf(&b, `\u%04x`, r)
		}
	}
	return b.String()
}
