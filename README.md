# alpaca

Order execution for Alpaca. `src/execute.py` opens positions sized in USD,
looks orders up, and closes them by client order id. It makes no trading decisions:
strategies, signals, stops and risk limits belong to the caller.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (it is gitignored, so it is not in the
repo) and fill in the variables below.

## Configuration

All settings are read from `.env` by `src/config.py`. Any variable left out
falls back to its default.

```env
# Broker config
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
DEMO=true

# Log file management
LOG_DIRECTORY=C:\logs\alpaca
LOG_NAME=alpaca.log
MAX_SIZE_IN_MB=10
MAX_BACKUP=5
LOG_LEVEL=INFO

```

| Variable            | Default      | Description                                                                                                             |
| ------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `ALPACA_API_KEY`    | _(empty)_    | Alpaca API key.                                                                                                         |
| `ALPACA_SECRET_KEY` | _(empty)_    | Alpaca API secret.                                                                                                      |
| `ALPACA_BASE_URL`   | _(empty)_    | Trading API base URL, such as `https://paper-api.alpaca.markets` or `https://api.alpaca.markets`; do not include `/v2`. |
| `DEMO`              | `true`       | Demo-mode label logged at startup. Set `ALPACA_BASE_URL` to the matching paper or live endpoint.                        |
| `STATE`             | `false`      | Kill switch. Orders are only placed or closed when `true`; `--balance` and `--orders` always work. Normally set by the caller (see below), not in this `.env`. |
| `LOG_DIRECTORY`     | `logs`       | Folder for log files, relative to the project root. Created if missing. `--logdirectory` overrides it.                  |
| `LOG_NAME`          | `alpaca.log` | Log file name. `--logname` overrides it.                                                                                |
| `MAX_SIZE_IN_MB`    | `10`         | Size at which the log file rotates. Decimals allowed.                                                                   |
| `MAX_BACKUP`        | `5`          | Number of rotated files to keep (`alpaca.log.1` … `alpaca.log.5`).                                                      |
| `LOG_LEVEL`         | `INFO`       | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`.                                                                      |

Use matching API keys and `ALPACA_BASE_URL` for the same account (paper or
live). The Alpaca SDK appends `/v2`, so provide only the base URL.

`STATE` is left out of this `.env` because the caller owns the kill switch:
alpaca-strategies keeps `STATE` in its own `.env` and passes it to every
`execute.py` run in the environment. A variable already set in the
environment wins over `.env`. Run on its own, `execute.py` falls back to
`STATE=false` and places no orders; set `STATE=true` in the environment (or
add it to this `.env`) to place or close orders by hand.

## Usage

Each run performs one action, prints its result as JSON on stdout, and exits.
Orders are always printed as a list of dictionaries, in Alpaca's order format
(`id`, `client_order_id`, `symbol`, `side`, `qty`, `filled_qty`, `status`, …).

```bash
python src/execute.py --balance
python src/execute.py --orders
python src/execute.py --orders 154f558b-8bff-485b-a886-d1e8cab10dbe,my-tag-1
python src/execute.py --asset SPY --amount 500 --position long
python src/execute.py --asset TSLA --amount 1000 --position short --client_order_id my-tag-1
python src/execute.py --asset BTC/USD --amount 100 --position long
python src/execute.py --asset SPY261014C00580000 --amount 400 --position call
python src/execute.py --close my-tag-1
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
| `--logdirectory`    | Log folder for this run, relative to the project root (an absolute path also works).                               |
| `--logname`         | Log file name for this run.                                                                                         |

`--balance`, `--orders`, `--close` and `--asset` are mutually exclusive.
Orders are identified only by their client order id, never by Alpaca's own
order id. Pass `--client_order_id` to choose it; otherwise a random UUID is
used, and it is returned in the order's `client_order_id` field. Lists of ids
can be wrapped in braces (`{id1,id2}`).

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
arguments. With several ids, a failure on one does not stop the others; it
appears in the list as `{"id": "<id>", "error": "<message>"}`. A failed
`--asset` returns `[{"id": "<client_order_id>", "error": "<message>"}]`.
With `STATE=false`, `--asset` and `--close` exit with `1` and place nothing.

## Logging

Each run logs to the log file and to stderr, so stdout carries only the JSON
result. Each line has the format:

```
<UTC timestamp> <computer name> <uuid7> <module> <line number> [<message>]
```

For example:

```
2026-10-14T02:34:48.298Z DESKTOP-01 01a13843-2a6a-7504-b2ff-8f7d67d54cba execute 190 [Starting execute (demo=True, url=https://paper-api.alpaca.markets)]
```

Uncaught exceptions are written to the log file too, for callers that discard
stderr.
