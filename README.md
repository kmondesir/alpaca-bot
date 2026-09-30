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
STRATEGY_SYMBOLS=

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
| `STRATEGY_SYMBOLS`     | _(empty)_        | Comma-separated stock symbols for MACD/Parabolic SAR signals, e.g. `SPY,AAPL`. Empty disables strategy entries.                 |
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

Because the script runs fresh every minute, order state is kept in a SQLite
table (`orders`, in `DB_PATH`) so each run knows what earlier runs did.
`Trade` writes to it on every place, cancel and close, and `start.py` calls
`Trade.sync_orders()` at the start of each run to pick up fills,
cancellations and expiries from Alpaca.

| Column            | Description                                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------------------------ |
| `client_order_id` | Order tag, `<PREFIX>-<uuid7>` (primary key).                                                                       |
| `hostname`        | Computer that placed the order.                                                                                    |
| `status`          | See below.                                                                                                         |
| `created`         | When the row was created (UTC, ISO 8601).                                                                          |
| `modified`        | When the row last changed (UTC, ISO 8601).                                                                         |
| `description`     | Free text passed to `place_order(description=...)`; replaced with a summary of the close when the order is closed. |
| `costs`           | Filled quantity × average fill price.                                                                              |
| `linked_order_id` | Client order ID of a linked protective exit (take-profit/trailing-stop).                                           |
| `basis`           | Entry cost basis used to calculate P/L for a tracked exit.                                                         |
| `side`            | Order side (`buy` or `sell`).                                                                                      |
| `quantity`        | Order quantity in shares or units; null for notional-only requests if Alpaca has no quantity.                      |
| `order_type`      | Order type (`market`, `limit`, or `trailing_stop`).                                                                |

| Status      | Meaning                                                                                            |
| ----------- | -------------------------------------------------------------------------------------------------- |
| `NA`        | Recorded locally, not yet confirmed at Alpaca.                                                     |
| `SUBMITTED` | Accepted by Alpaca, nothing filled yet.                                                            |
| `PARTIAL`   | Partly filled, still working.                                                                      |
| `FILLED`    | Completely filled.                                                                                 |
| `CANCELLED` | Cancelled or rejected (`costs` > 0 if it partly filled first).                                     |
| `CLOSED`    | Filled quantity has been closed out. `close_order` skips these, so an order is never closed twice. |

Closing an order marks its original row `CLOSED` and records the closing market
order in a separate row so its fill and P/L can be tracked.

`close_orders()` only acts on rows whose `client_order_id` starts with this
`.env`'s `PREFIX` and that are still working or hold a fill. Orders from other
prefixes, and positions not in the database, are left alone.
| `EXPIRED` | Expired at Alpaca (`costs` > 0 if it partly filled first). |
| `SKIPPED` | Never reached Alpaca (e.g. the submit failed). |

## Run

### Strategy

Set `STRATEGY_SYMBOLS` to a comma-separated list of stocks (for example,
`SPY,AAPL`) to enable signal generation. The strategy checks closed 15-minute
MACD (12/26/9) bars for bullish crosses below zero or bearish crosses above
zero, confirmed by Parabolic SAR on both 1-minute and 5-minute bars. It only
returns signals; `start.py` sends them to `risk.py`, which requires `STATE=true`
and an open Alpaca market, prevents duplicate entries, applies `WAGER`, and
handles protective exits and tracked reversals. An empty symbol list creates
no signals.

Set `STATE=true` in `.env`, then:

```bash
python src/start.py
```

The script runs once and exits; it does not poll. In production it is run
every 60 seconds by a cron job, for example:

```cron
* * * * * cd /path/to/alpaca && .venv/bin/python src/start.py
```
