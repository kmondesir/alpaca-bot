# alpaca

Order execution for Alpaca. `alpaca` is a small Go program that opens
positions sized in USD, looks orders up, and closes them by client order id.
It makes no trading decisions: strategies, signals, stops, risk limits and the
kill switch belong to the caller.

## Setup

Install [Go](https://go.dev/dl/) 1.26 or later, then build the binary in the
project root:

```bash
go build -o alpaca.exe ./cmd/alpaca
```

On Linux and macOS, build it as `alpaca` instead. The binary is gitignored,
so **rebuild it after every `git pull`**. A caller that runs it (such as
alpaca-strategies) otherwise keeps running the old build, or finds none.

Create a `.env` file in the project root, next to `alpaca.exe`. It is
gitignored, so it is not in the repo. Fill in the variables below.

## Configuration

`internal/config` loads `.env` from the folder holding the binary, using
[godotenv](https://github.com/joho/godotenv), and then reads the variables
below. A variable already set in the environment wins over `.env`, so a
caller can pass them in directly. Under `go run` and `go test`, `.env` is read
from the working directory instead.

```env
ALPACA_SECRET_KEY=

# Broker config
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_API_KEY=
DEMO=true

# Log file management
LOG_DIRECTORY=logs
LOG_NAME=alpaca.log
MAX_SIZE_IN_MB=10
MAX_BACKUP=5
LOG_LEVEL=INFO
```

| Variable            | Default   | Description                                                                                                             |
| ------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------- |
| `ALPACA_API_KEY`    | _(empty)_ | Alpaca API key. Required.                                                                                               |
| `ALPACA_SECRET_KEY` | _(empty)_ | Alpaca API secret. Required.                                                                                            |
| `ALPACA_BASE_URL`   | _(empty)_ | Trading API base URL, such as `https://paper-api.alpaca.markets` or `https://api.alpaca.markets`; do not include `/v2`. Required. |
| `DEMO`              | `true`    | Demo-mode label logged at startup. Set `ALPACA_BASE_URL` to the matching paper or live endpoint.                        |
| `LOG_DIRECTORY`     | `logs`    | Folder for log files, relative to the project root (an absolute path also works). Created if missing. |
| `LOG_NAME`          | `alpaca.log` | Log file name.                                                                                                       |
| `MAX_SIZE_IN_MB`    | `10`      | Size at which the log file rotates. Decimals allowed; `0` turns rotation off.                                           |
| `MAX_BACKUP`        | `5`       | Number of rotated files to keep (`alpaca.log.1` … `alpaca.log.5`); `0` turns rotation off.                              |
| `LOG_LEVEL`         | `INFO`    | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, in any case.                                                         |

A log variable left out or left blank uses its default. An invalid number or
level stops the run with exit code `1` and an error on stderr, before
anything is sent to Alpaca.

Logging is set only here, not on the command line. A variable in the
environment still wins over `.env`, so a caller must not pass its own log
settings to `alpaca.exe` unless it means to. alpaca-strategies uses the same
variable names for its own log, so its executor removes them from the
environment it passes, and this `.env` decides where `alpaca` logs. To keep
`alpaca`'s log next to the strategy's, set for example:

```env
LOG_DIRECTORY=C:/logs/alpaca
LOG_NAME=alpaca.log
```

Don't point `LOG_NAME` at a file another program also writes: two processes
can't share a rotating log.

Use matching API keys and `ALPACA_BASE_URL` for the same account (paper or
live). The Alpaca SDK appends `/v2`, so provide only the base URL.

This project only executes orders: every `--asset` or `--close` run places or
closes orders. Deciding whether to trade, including any kill switch, is the
caller's job.

## Usage

Each run performs one action, prints its result as JSON on stdout, and exits.
Orders are always printed as a list of dictionaries, in Alpaca's order format
(`id`, `client_order_id`, `symbol`, `side`, `qty`, `filled_qty`, `status`, …).

```bash
./alpaca.exe --balance
./alpaca.exe --orders
./alpaca.exe --orders 154f558b-8bff-485b-a886-d1e8cab10dbe,my-tag-1
./alpaca.exe --asset SPY --amount 500 --position long
./alpaca.exe --asset TSLA --amount 1000 --position short --client_order_id my-tag-1
./alpaca.exe --asset BTC/USD --amount 100 --position long
./alpaca.exe --asset SPY261014C00580000 --amount 400 --position call
./alpaca.exe --close my-tag-1
```

| Argument            | Description                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `--balance`         | Account cash, equity, buying power, options buying power and portfolio value, as one dictionary.                   |
| `--orders [IDS]`    | Without ids, the last 100 orders in any status, newest first. With comma-separated client order ids, only those orders. |
| `--close IDS`       | Close orders by client order id (see below).                                                                        |
| `--asset`           | Symbol to open a position in: a stock (`SPY`), a crypto pair (`BTC/USD`; `BTC` also works), or an OCC option contract. |
| `--amount`          | Position size in USD. Required with `--asset`.                                                                      |
| `--position`        | `long`, `short`, `call` or `put` (any case). Required with `--asset`.                                               |
| `--client_order_id` | Client order id for the order placed by `--asset` or `--close` (one id only). Defaults to a random UUID. Max 128 characters. |
| `-h`, `--help`      | Show the help text.                                                                                                 |

`--balance`, `--orders`, `--close` and `--asset` are mutually exclusive.
Options take their value after a space or an `=` (`--amount=500`); shortened
names such as `--bal` are not accepted. Orders are identified only by their
client order id, never by Alpaca's own order id. Pass `--client_order_id` to
choose it; otherwise a random UUID is used, and it is returned in the order's
`client_order_id` field. Lists of ids can be wrapped in braces (`{id1,id2}`).

### Opening positions

| Position | Asset                | Order                                                                                                        |
| -------- | -------------------- | ------------------------------------------------------------------------------------------------------------ |
| `long`   | Stock or crypto pair | Market buy for `--amount` dollars (rounded down to cents). Stocks are day orders, crypto GTC.               |
| `short`  | Stock                | Market sell of the whole shares `--amount` covers at the bid (Alpaca rejects fractional shorts). Crypto can't be shorted. |
| `call`   | OCC call contract    | Day limit buy at the ask for the whole contracts `--amount` covers: `floor(amount ÷ (ask × 100))`.           |
| `put`    | OCC put contract     | As for `call`, with a put contract.                                                                           |

Contract selection is the caller's job: pass the exact OCC symbol, for example
`SPY261014C00580000` (root, `YYMMDD` expiry, `C`/`P`, strike × 1000). The
position must match the contract type. No take-profit or stop orders are
attached; the caller manages exits.

### Closing orders

`--close` takes one or more client order ids and, for each one:

1. If the order is still working, cancels it and waits up to 10 seconds for
   the cancel to settle.
2. If nothing had filled, returns the cancelled order.
3. Otherwise closes the filled quantity with an opposite market order (sell
   for a buy, buy back for a short) and returns that closing order. The
   quantity is capped at what the position still has available, since crypto
   fees come out of the purchased coin.

A close fails for that id if the position is already gone or is on the other
side, or if another order holds the shares.

### Errors and exit codes

The exit code is `0` on success, `1` if anything failed, and `2` for invalid
arguments (the usage and the error go to stderr).

With several ids, a failure on one does not stop the others; it appears in the
list as `{"id": "<id>", "error": "<message>"}`. A failed `--asset` returns
`[{"id": "<client_order_id>", "error": "<message>"}]`. An error from Alpaca
reads `"<HTTP status>: <response body>"`. An error entry always means the
order was not placed or closed.

When the outcome is unknown, for example when the connection to Alpaca fails
mid-request, the run prints no JSON and exits with `1`, because the order may
or may not have reached Alpaca. A crash also exits with `1`.

## Logging

Each run logs to the log file and to stderr, so stdout carries only the JSON
result. The file is `LOG_NAME` in `LOG_DIRECTORY`; by default
`logs/alpaca.log` under the project root. Lines below `LOG_LEVEL` are left out. The file rotates when it reaches
`MAX_SIZE_IN_MB`, keeping `MAX_BACKUP` old files (by default 10 MB and
`alpaca.log.1` … `alpaca.log.5`).

Each line has the format:

```
<UTC timestamp> <computer name> <uuid7> <module> <line number> [<message>]
```

where `<module>` is the Go source file the line was logged from, without
`.go`. For example:

```
2026-10-15T02:39:18.820Z DESKTOP-01 01a13d6d-a724-7079-8058-478dcaa94a7d alpaca 81 [Starting alpaca (demo=True, url=https://paper-api.alpaca.markets)]
```

A crash is written to the log file with its stack trace, for callers that
discard stderr.

## Development

| Path               | Contents                                                                 |
| ------------------ | ------------------------------------------------------------------------ |
| `cmd/alpaca`       | The command: argument parsing, JSON output, exit codes.                  |
| `internal/config`  | `.env` settings, project root, log format and rotation.                  |
| `internal/trade`   | Orders through the [Alpaca Go SDK](https://github.com/alpacahq/alpaca-trade-api-go). |
| `internal/symbols` | Stock, crypto pair and OCC option symbol helpers.                        |
| `tools`            | Python scripts that record and compare outputs on the paper account (below). |
| `testdata/python`  | Outputs of the earlier Python version: the reference for the JSON contract. |
| `testdata/go`      | The latest recording of this build, as compared with `testdata/python`.  |

```bash
go vet ./...
go test ./...
```

The tests run the SDK against a fake Alpaca server and touch no account.

The JSON output, exit codes and log format were carried over unchanged from
the earlier Python version, which callers were written against.
`testdata/python` holds its outputs for 28 scenarios on the paper account. To
check that a change keeps that contract, replay the scenarios against a new
build and compare. This places and closes small paper orders, and needs a
Python with `python-dotenv`; this project has no virtual environment of its
own, but alpaca-strategies' `.venv` works:

```bash
python tools/record_outputs.py --env .env --out testdata/go --cmd "./alpaca.exe" --option <cheap OCC call>
python tools/compare_outputs.py testdata/python testdata/go
```

`--option` takes an OCC call contract whose ask is at most $0.05, so `$5` buys
one contract; the script cancels it again. Stock and option orders only fill
while the market is open; out of hours they queue and are cancelled, and only
the crypto scenarios fill.

### Changing the command line or the output

alpaca-strategies runs `alpaca.exe` on every scheduled run and parses its
JSON, so it has to change with any argument, field or exit code. Make the
caller accept both the old and the new form first, or release the two
changes in an order that never pairs a caller with a binary that rejects its
arguments. Rebuild `alpaca.exe` in the live checkout last.

The binary was called `execute.exe` before it was renamed to `alpaca.exe`. A
build doesn't remove the old `execute.exe`, so a caller that still runs it
keeps the old build until it is pointed at `alpaca.exe` (in alpaca-strategies,
`ALPACA_BOT_EXECUTE` or its default in `src/config.py`).
