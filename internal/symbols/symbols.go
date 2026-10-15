// Package symbols normalizes stock, crypto pair and OCC option contract symbols.
package symbols

import (
	"regexp"
	"strings"
)

// optionSymbol matches an OCC option symbol, e.g. SPY261004C00575000:
// root, YYMMDD expiry, C/P, strike x 1000.
var optionSymbol = regexp.MustCompile(`^([A-Z]{1,6})(\d{6})([CP])(\d{8})$`)

// cryptoQuotes are the quote currencies CryptoPair recognizes, longest first.
var cryptoQuotes = []string{"USDT", "USDC", "USD", "BTC"}

// Normalize trims and upper-cases symbol; BTC and BTCUSD become BTC/USD.
func Normalize(symbol string) string {
	value := strings.ToUpper(strings.TrimSpace(symbol))
	if value == "BTC" || value == "BTCUSD" {
		return "BTC/USD"
	}
	return value
}

// IsCrypto reports whether symbol is a crypto pair such as BTC/USD.
func IsCrypto(symbol string) bool {
	return strings.Contains(Normalize(symbol), "/")
}

// IsOption reports whether symbol is an OCC option contract.
func IsOption(symbol string) bool {
	return optionSymbol.MatchString(Normalize(symbol))
}

// OptionType returns "call" or "put" for an OCC option symbol, and "" otherwise.
func OptionType(symbol string) string {
	match := optionSymbol.FindStringSubmatch(Normalize(symbol))
	if match == nil {
		return ""
	}
	if match[3] == "C" {
		return "call"
	}
	return "put"
}

// CryptoPair restores the slash Alpaca drops from crypto position symbols,
// e.g. ETHUSD -> ETH/USD.
func CryptoPair(symbol string) string {
	value := Normalize(symbol)
	if strings.Contains(value, "/") {
		return value
	}
	for _, quote := range cryptoQuotes {
		if strings.HasSuffix(value, quote) && len(value) > len(quote) {
			return value[:len(value)-len(quote)] + "/" + quote
		}
	}
	return value
}
