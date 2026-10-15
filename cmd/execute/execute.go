// Command execute places, looks up and closes Alpaca orders.
//
// It prints the result as JSON on stdout; orders are always a list. Logs go
// to the log file and stderr.
//
//	execute --balance
//	execute --orders
//	execute --orders ID1,ID2
//	execute --asset SPY --amount 500 --position long [--client_order_id TAG]
//	execute --close ID1,ID2
//
// Exit codes: 0 on success, 1 if anything failed, 2 for invalid arguments.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"

	"github.com/alpacahq/alpaca-trade-api-go/v3/alpaca"

	"github.com/kmondesir/alpaca-bot/internal/config"
	"github.com/kmondesir/alpaca-bot/internal/trade"
)

// recentOrders is how many orders --orders lists without ids.
const recentOrders = 100

// trader is what execute needs from trade.Trade.
type trader interface {
	GetBalance() (trade.Balance, error)
	GetOrders(limit int) ([]alpaca.Order, error)
	GetOrder(clientOrderID string) (*alpaca.Order, error)
	OpenPosition(asset string, amount float64, position, clientOrderID string) (*alpaca.Order, error)
	CloseOrder(orderID, clientOrderID string) (*alpaca.Order, error)
}

func main() {
	defer config.LogPanic()
	root, err := config.RootDir()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	newTrader := func(c config.Config) trader { return trade.New(c.APIKey, c.SecretKey, c.BaseURL, "") }
	os.Exit(run(os.Args[1:], root, os.Stdout, os.Stderr, newTrader))
}

// run executes one command and returns the exit code.
func run(args []string, root string, stdout, stderr io.Writer, newTrader func(config.Config) trader) int {
	opts, err := parseArgs(args)
	if errors.Is(err, errHelp) {
		fmt.Fprintln(stdout, help)
		return 0
	}
	if err != nil {
		fmt.Fprintf(stderr, "%s\nexecute: error: %s\n", usage, err)
		return 2
	}

	settings, err := config.Load(root)
	if err != nil {
		fmt.Fprintf(stderr, "Invalid settings in .env or the environment: %s\n", err)
		return 1
	}
	// --logdirectory and --logname win over LOG_DIRECTORY and LOG_NAME.
	logSettings := settings.Log
	if opts.logDirectory != "" {
		logSettings.Directory = opts.logDirectory
	}
	if opts.logName != "" {
		logSettings.Name = opts.logName
	}
	_, closeLog, err := config.SetupLogging(root, logSettings, stderr)
	if err != nil {
		fmt.Fprintf(stderr, "Could not open the log file: %s\n", err)
		return 1
	}
	defer closeLog()

	if missing := settings.Missing(); len(missing) > 0 {
		slog.Error(fmt.Sprintf("Missing required .env variables: %s; exiting", strings.Join(missing, ", ")))
		return 1
	}
	slog.Info(fmt.Sprintf("Starting execute (demo=%s, url=%s)", pyBool(settings.Demo), settings.BaseURL))

	result, ok, err := execute(opts, newTrader(settings))
	if err != nil {
		// Not an answer from Alpaca (e.g. the connection failed), so an order
		// may or may not have been placed: abort without a result, as the
		// Python version did, rather than report the id as failed.
		slog.Log(context.Background(), config.LevelCritical, fmt.Sprintf("Uncaught error; run aborted: %v", err))
		return 1
	}
	if err := printJSON(stdout, result); err != nil {
		slog.Error(fmt.Sprintf("Could not write the result: %v", err))
		return 1
	}
	if !ok {
		return 1
	}
	return 0
}

// execute runs the requested action and returns its result, whether it all
// succeeded, and an error only for failures that aren't the order's fault.
func execute(opts options, t trader) (any, bool, error) {
	if opts.balance {
		balance, err := t.GetBalance()
		return balance, err == nil, err
	}
	if opts.ordersSet {
		if len(opts.orders) == 0 {
			orders, err := t.GetOrders(recentOrders)
			if err != nil {
				return nil, false, err
			}
			results := []any{}
			for _, order := range orders {
				results = append(results, toOrderJSON(order))
			}
			return results, true, nil
		}
		return forEachID(opts.orders, t.GetOrder)
	}
	if opts.close != nil {
		return forEachID(opts.close, func(id string) (*alpaca.Order, error) {
			return t.CloseOrder(id, opts.clientOrderID)
		})
	}

	clientOrderID := opts.clientOrderID
	if clientOrderID == "" {
		clientOrderID = config.NewClientOrderID()
	}
	order, err := t.OpenPosition(opts.asset, opts.amount, opts.position, clientOrderID)
	if err != nil {
		if !reportable(err) {
			return nil, false, err
		}
		slog.Error(fmt.Sprintf("Could not open %s %s: %s", opts.position, opts.asset, logMessage(err)))
		return []any{errorEntry{clientOrderID, errorMessage(err)}}, false, nil
	}
	return []any{toOrderJSON(*order)}, true, nil
}

// forEachID runs action on each id; a failure becomes an {"id", "error"}
// entry so the rest still run.
func forEachID(ids []string, action func(string) (*alpaca.Order, error)) (any, bool, error) {
	results, ok := []any{}, true
	for _, id := range ids {
		order, err := action(id)
		if err != nil {
			if !reportable(err) {
				return nil, false, err
			}
			slog.Error(fmt.Sprintf("%s failed: %s", id, logMessage(err)))
			results = append(results, errorEntry{id, errorMessage(err)})
			ok = false
			continue
		}
		results = append(results, toOrderJSON(*order))
	}
	return results, ok, nil
}

// reportable reports whether err is Alpaca's or Trade's answer about the
// order, which the result lists as that order's error.
func reportable(err error) bool {
	var apiErr *alpaca.APIError
	var tradeErr trade.Error
	return errors.As(err, &apiErr) || errors.As(err, &tradeErr)
}

// logMessage is the error as the Python logs showed it: an API error's body
// without the status code.
func logMessage(err error) string {
	var apiErr *alpaca.APIError
	if errors.As(err, &apiErr) {
		return apiBody(apiErr)
	}
	return err.Error()
}

func pyBool(b bool) string {
	if b {
		return "True"
	}
	return "False"
}
