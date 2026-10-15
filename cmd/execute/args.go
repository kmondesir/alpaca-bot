package main

import (
	"errors"
	"fmt"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/kmondesir/alpaca-bot/internal/trade"
)

// maxClientOrderID is the most characters Alpaca allows in a client_order_id.
const maxClientOrderID = 128

const usage = `usage: execute [-h] (--balance | --orders [ID1,ID2] | --close ID1,ID2 |
               --asset ASSET) [--amount AMOUNT]
               [--position {long,short,call,put}]
               [--client_order_id CLIENT_ORDER_ID]`

const help = usage + `

Execute and look up Alpaca orders; results print as JSON.

options:
  -h, --help            show this help message and exit
  --balance             show the account balance
  --orders [ID1,ID2]    show the last 100 orders, or only the orders with the
                        given client_order_ids
  --close ID1,ID2       close orders by client_order_id: cancel if working,
                        then sell/buy back any fill
  --asset ASSET         symbol to open a position in: stock, crypto pair
                        (BTC/USD), or OCC option
  --amount AMOUNT       position size in USD (with --asset)
  --position {long,short,call,put}
                        long or short for stocks and crypto, call or put for
                        an option contract (with --asset)
  --client_order_id CLIENT_ORDER_ID
                        client_order_id for the order placed (default: a
                        random UUID); with --close, only one id is allowed

Logging is set in .env: LOG_DIRECTORY, LOG_NAME, MAX_SIZE_IN_MB, MAX_BACKUP
and LOG_LEVEL.`

// options holds the parsed command line. Exactly one of balance, orders,
// close and asset is set.
type options struct {
	balance       bool
	orders        []string // empty for the most recent orders
	ordersSet     bool
	close         []string
	asset         string
	assetSet      bool
	amount        float64
	amountSet     bool
	position      string
	clientOrderID string
	help          bool
}

// errHelp is returned when -h or --help asks for the help text.
var errHelp = errors.New("help requested")

// negativeNumber matches what argparse accepts as an option's value even
// though it starts with a dash.
var negativeNumber = regexp.MustCompile(`^-\d+$|^-\d*\.\d+$`)

var actions = []string{"--balance", "--orders", "--close", "--asset"}

// parseArgs parses the command line the way the former Python execute.py did,
// with the same error messages. Prefix abbreviations such as --bal are not
// accepted.
func parseArgs(args []string) (options, error) {
	var opts options
	var unrecognized []string
	action := ""

	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "-h" || arg == "--help" {
			return opts, errHelp
		}
		if !strings.HasPrefix(arg, "-") || negativeNumber.MatchString(arg) || arg == "-" {
			unrecognized = append(unrecognized, arg)
			continue
		}
		name, inline, hasInline := strings.Cut(arg, "=")
		if !slices.Contains([]string{"--balance", "--orders", "--close", "--asset", "--amount",
			"--position", "--client_order_id"}, name) {
			unrecognized = append(unrecognized, arg)
			continue
		}

		if slices.Contains(actions, name) {
			if action != "" && action != name {
				return opts, fmt.Errorf("argument %s: not allowed with argument %s", name, action)
			}
			action = name
		}

		// value returns the option's value: inline, or the next argument.
		value := func() (string, error) {
			if hasInline {
				return inline, nil
			}
			if i+1 < len(args) && isValue(args[i+1]) {
				i++
				return args[i], nil
			}
			return "", fmt.Errorf("argument %s: expected one argument", name)
		}

		switch name {
		case "--balance":
			if hasInline {
				return opts, fmt.Errorf("argument --balance: ignored explicit argument '%s'", inline)
			}
			opts.balance = true
		case "--orders":
			opts.ordersSet, opts.orders = true, nil
			if hasInline || (i+1 < len(args) && isValue(args[i+1])) {
				raw, _ := value()
				ids, err := parseIDs(raw)
				if err != nil {
					return opts, fmt.Errorf("argument --orders: %w", err)
				}
				opts.orders = ids
			}
		case "--close":
			raw, err := value()
			if err != nil {
				return opts, err
			}
			if opts.close, err = parseIDs(raw); err != nil {
				return opts, fmt.Errorf("argument --close: %w", err)
			}
		case "--asset":
			raw, err := value()
			if err != nil {
				return opts, err
			}
			opts.asset, opts.assetSet = raw, true
		case "--amount":
			raw, err := value()
			if err != nil {
				return opts, err
			}
			if opts.amount, err = parseAmount(raw); err != nil {
				return opts, fmt.Errorf("argument --amount: %w", err)
			}
			opts.amountSet = true
		case "--position":
			raw, err := value()
			if err != nil {
				return opts, err
			}
			position := strings.ToLower(raw)
			if !slices.Contains(trade.Positions, position) {
				return opts, fmt.Errorf("argument --position: invalid choice: '%s' (choose from %s)",
					position, strings.Join(trade.Positions, ", "))
			}
			opts.position = position
		case "--client_order_id":
			raw, err := value()
			if err != nil {
				return opts, err
			}
			if opts.clientOrderID, err = parseClientOrderID(raw); err != nil {
				return opts, fmt.Errorf("argument --client_order_id: %w", err)
			}
		}
	}

	if action == "" {
		return opts, errors.New("one of the arguments --balance --orders --close --asset is required")
	}
	if len(unrecognized) > 0 {
		return opts, fmt.Errorf("unrecognized arguments: %s", strings.Join(unrecognized, " "))
	}
	return opts, validate(opts)
}

// isValue reports whether arg can be an option's value rather than an option.
func isValue(arg string) bool {
	return !strings.HasPrefix(arg, "-") || arg == "-" || negativeNumber.MatchString(arg)
}

// validate checks the combinations argparse can't express.
func validate(opts options) error {
	if opts.assetSet {
		var missing []string
		if !opts.amountSet {
			missing = append(missing, "--amount")
		}
		if opts.position == "" {
			missing = append(missing, "--position")
		}
		if len(missing) > 0 {
			return fmt.Errorf("--asset requires %s", strings.Join(missing, " and "))
		}
	} else {
		if opts.amountSet {
			return errors.New("--amount is only used with --asset")
		}
		if opts.position != "" {
			return errors.New("--position is only used with --asset")
		}
	}
	if opts.clientOrderID != "" {
		if !opts.assetSet && opts.close == nil {
			return errors.New("--client_order_id is only used with --asset or --close")
		}
		if len(opts.close) > 1 {
			return errors.New("--client_order_id can only tag the close of a single order")
		}
	}
	return nil
}

// parseIDs splits a comma-separated list of client order ids, optionally
// wrapped in braces, dropping blanks and repeats.
func parseIDs(value string) ([]string, error) {
	var ids []string
	for _, item := range strings.Split(strings.Trim(value, "{}"), ",") {
		item = strings.TrimSpace(item)
		if item != "" && !slices.Contains(ids, item) {
			ids = append(ids, item)
		}
	}
	if len(ids) == 0 {
		return nil, errors.New("expected at least one client_order_id")
	}
	return ids, nil
}

func parseAmount(value string) (float64, error) {
	amount, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
	if err != nil || math.IsNaN(amount) || math.IsInf(amount, 0) {
		return 0, fmt.Errorf("not a USD amount: '%s'", value)
	}
	if amount <= 0 {
		return 0, errors.New("amount must be positive")
	}
	return amount, nil
}

func parseClientOrderID(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" || utf8.RuneCountInString(value) > maxClientOrderID {
		return "", fmt.Errorf("client_order_id must be 1 to %d characters", maxClientOrderID)
	}
	return value, nil
}
