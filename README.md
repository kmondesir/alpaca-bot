# alpaca

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
ALPACA_BASE_URL=
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
DEMO=true
STATE=false

LOG_DIRECTORY=logs
LOG_NAME=alpaca.log
MAX_SIZE_IN_MB=10
MAX_BACKUP=5
LOG_LEVEL=INFO

PREFIX=alpaca
DB_PATH=data/alpaca.db

WAGER=0.04
TAKE_PROFIT=0.5
TRAILING_STOP_LOSS=0.05
MAX_CONSECUTIVE_LOSS=5
MAX_OPEN_POSITIONS=3
LOSS_DIRECTORY=status
```

| Variable               | Default          | Description                                                                                                                     |
| ---------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `ALPACA_API_KEY`       | _(empty)_        | Alpaca API key.                                                                                                                 |
| `ALPACA_SECRET_KEY`    | _(empty)_        | Alpaca API secret.                                                                                                              |
| `ALPACA_BASE_URL`      | _(empty)_        | Trading API base URL, such as `https://paper-api.alpaca.markets` or `https://api.alpaca.markets`; do not include `/v2`.         |
| `DEMO`                 | `true`           | Demo-mode label logged at startup. Set `ALPACA_BASE_URL` to the matching paper or live endpoint.                                |
| `STATE`                | `false`          | Kill switch. The script only runs when `true`; otherwise it logs a message and exits.                                           |
| `LOG_DIRECTORY`        | `logs`           | Folder for log files, relative to the project root. Created if missing.                                                         |
| `LOG_NAME`             | `alpaca.log`     | Log file name.                                                                                                                  |
| `MAX_SIZE_IN_MB`       | `10`             | Size at which the log file rotates. Decimals allowed.                                                                           |
| `MAX_BACKUP`           | `5`              | Number of rotated files to keep (`alpaca.log.1` … `alpaca.log.5`).                                                              |
| `LOG_LEVEL`            | `INFO`           | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`.                                                                              |
| `PREFIX`               | `alpaca`         | Prefix for generated order tags: `client_order_id` is `<PREFIX>-<uuid7>`. Max 91 characters.                                    |
| `DB_PATH`              | `data/alpaca.db` | SQLite order-tracking database, relative to the project root. Created if missing.                                               |
| `WAGER`                | `0.04`           | Fraction of account buying power used for a new position. For example, `0.04` sizes a position at 4% of buying power.           |
| `TAKE_PROFIT`          | `0.5`            | Fraction above the purchase price for the take-profit exit. For example, `0.5` targets 50% above entry; `0` disables it.        |
| `TRAILING_STOP_LOSS`   | `0.05`           | Fraction the trailing stop follows below the running high price. For example, `0.05` trails by 5%; `0` disables it.             |
| `MAX_CONSECUTIVE_LOSS` | `5`              | Consecutive losing trades allowed before the bot sets `STATE=false` in `.env`; `0` disables this kill switch.                   |
| `MAX_OPEN_POSITIONS`   | `3`              | Maximum number of distinct open positions. Pending orders reserve a slot; additional orders for an existing symbol are allowed. |
| `LOSS_DIRECTORY`       | `status`         | Directory, relative to the project root, for `losses.json`, which stores loss history and the consecutive-loss count.           |

### Risk management

- `WAGER` is applied to buying power each time a new position is opened, so
  position size changes as the account balance changes.
- New symbols are rejected when `MAX_OPEN_POSITIONS` is reached. Existing
  positions and pending buy orders count toward the limit; buys that add to an
  already-open symbol do not consume another slot.
- A filled take-profit or trailing-stop exit is recorded as a trade outcome.
  Once the loss streak reaches `MAX_CONSECUTIVE_LOSS`, the bot writes
  `STATE=false` to `.env`; the next run exits without trading. Set the limit
  to `0` to disable this automatic kill switch while keeping loss history.
- The loss tracker is created at `LOSS_DIRECTORY/losses.json` when needed. It
  persists across runs. To reset it after reviewing the losses, manually set
  `STATE=true` and delete the tracker file.
- Use matching API keys and `ALPACA_BASE_URL` for the same account (paper or
  live). The Alpaca SDK appends `/v2`, so provide only the base URL.

## Logging

Importing `config` sets up logging to both the log file and the console, so
other modules only need:

```python
import logging

logger = logging.getLogger(__name__)
```

Each line has the format:

```
<UTC timestamp> <computer name> <uuid7> <module> <line number> [<message>]
```

For example:

```
2026-09-29T09:09:23.152Z DESKTOP-01 01a0ec6d-0650-7559-a5f2-50cca19d19a4 start 11 [Starting alpaca (demo=True)]
```

## Order tracking

Order history is stored in `parent` and `child` tables in the SQLite database
at `DB_PATH`. A parent row represents an initial entry and tracks the overall
position lifecycle, entry order status, costs, and any manual/reversal close
order ID, status, costs, side, quantity, and type. Each take-profit or trailing
stop order is a child row linked to its parent; both children remain for
history when one fills and the other is canceled.

Parent overall status is `NA`, `SUBMITTED`, `PARTIAL`, `OPEN`, `CLOSING`,
`CLOSED`, `CANCELLED`, `EXPIRED`, or `SKIPPED`. Entry and child order statuses
reflect Alpaca (`NA`, `SUBMITTED`, `PARTIAL`, `FILLED`, `CANCELLED`, `EXPIRED`,
or `SKIPPED`). `Trade.sync_orders()` refreshes pending entries, protective
children, and parent close orders on each run. `close_orders()` only closes
parent positions whose IDs start with the configured `PREFIX`.

Duplicate strategy signals are claimed atomically in the risk ledger, also
stored in the same database. This database has not been used for live orders;
the obsolete `orders` table is dropped during initialization; no order data is
migrated.

## Run

### CLI commands

Pass a command after `src/start.py` to inspect account state or perform a
risk-gated action:

```bash
python src/start.py get_balance
python src/start.py asset AAPL
python src/start.py get_positions
python src/start.py get_position AAPL
python src/start.py get_market_status
python src/start.py open_position AAPL --side buy
python src/start.py close_position AAPL
```

For the scheduled strategy, pass one or more comma-separated assets without a
subcommand. Stocks use Alpaca's IEX feed; crypto pairs use crypto bars. `BTC` is
accepted as an alias for `BTC/USD`:

```bash
python src/start.py --assets SPY
python src/start.py --assets SPY,AAPL,BTC/USD
```

`open_position` submits a market order sized by risk using `WAGER` from `.env`.
`close_position` closes a tracked position. Both require `STATE=true` and an
open market; the read-only commands do not require `STATE`. With no subcommand,
the scheduled strategy requires the `--assets` argument.

### Strategy

Each asset in `--assets` is evaluated in turn, and every asset that produces a
signal is sent on to `risk.py`. The strategy checks closed 15-minute
MACD (12/26/9) bars for bullish crosses below zero or bearish crosses above
zero, confirmed by Parabolic SAR on both 1-minute and 5-minute bars. Crypto
bars come from Coinbase's public API (no key needed); stock bars come from
Alpaca's IEX feed. It only
returns signals; `start.py` sends them to `risk.py`, which requires `STATE=true`,
prevents duplicate entries, applies `WAGER`, and handles protective exits and
tracked reversals. Crypto runs 24/7, is long-only, and uses GTC orders. The
configured `TRAILING_STOP_LOSS` fraction becomes a stop-limit for crypto, since
Alpaca has no crypto trailing-stop order; each run raises it to that fraction
below the current bid once it can move by a tenth of the trail distance, and
never lowers it. Because the bot moves the stop once per run, it trails the
price at the run interval, not tick by tick. A fast drop can trigger a
stop-limit without filling it, so when a run finds the bid at or below a stop
that has not filled, it cancels the stop and sells the remainder at market.
Crypto position sizing uses non-marginable buying power.

Set `STATE=true` in `.env` and provide one or more assets, then:

```bash
python src/start.py --assets SPY,AAPL
```

The script runs once and exits; it does not poll. In production it is run
every 60 seconds by a cron job, for example:

```cron
* * * * * cd /path/to/alpaca && .venv/bin/python src/start.py --assets SPY,AAPL
```
