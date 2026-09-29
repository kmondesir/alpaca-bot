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
```

| Variable | Default | Description |
|---|---|---|
| `ALPACA_API_KEY` | *(empty)* | Alpaca API key. |
| `ALPACA_SECRET_KEY` | *(empty)* | Alpaca API secret. |
| `DEMO` | `true` | `true` uses the `demo` (paper-trading) URL from `src/config.json`; `false` uses the `prod` (live) URL. The chosen URL is `config.BASE_URL`. |
| `STATE` | `false` | Kill switch. The script only runs when `true`; otherwise it logs a message and exits. |
| `LOG_DIRECTORY` | `logs` | Folder for log files, relative to the project root. Created if missing. |
| `LOG_NAME` | `alpaca.log` | Log file name. |
| `MAX_SIZE_IN_MB` | `10` | Size at which the log file rotates. Decimals allowed. |
| `MAX_BACKUP` | `5` | Number of rotated files to keep (`alpaca.log.1` … `alpaca.log.5`). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `PREFIX` | `alpaca` | Prefix for generated order tags: `client_order_id` is `<PREFIX>-<uuid7>`. Max 91 characters. |
| `DB_PATH` | `data/alpaca.db` | SQLite order-tracking database, relative to the project root. Created if missing. |

### Demo vs. live

- **Keys are per account.** Alpaca issues separate API keys for paper and live
  accounts. When you change `DEMO`, put the matching `ALPACA_API_KEY` and
  `ALPACA_SECRET_KEY` in `.env`, or every request will be rejected.
- **No `/v2` in the URLs.** The `demo` and `prod` URLs in `src/config.json` are
  base URLs only. The Alpaca SDK appends `/v2` itself, so adding it produces
  `/v2/v2/...` and requests fail.
- **Market data is shared.** `DEMO` only switches the trading API. Quotes (used
  by `Trade.get_asset_data`) come from Alpaca's single market data endpoint,
  which works with either set of keys.

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

| Column | Description |
|---|---|
| `client_order_id` | Order tag, `<PREFIX>-<uuid7>` (primary key). |
| `hostname` | Computer that placed the order. |
| `status` | See below. |
| `created` | When the row was created (UTC, ISO 8601). |
| `modified` | When the row last changed (UTC, ISO 8601). |
| `description` | Free text passed to `place_order(description=...)`; replaced with a summary of the close when the order is closed. |
| `costs` | Filled quantity × average fill price. |

| Status | Meaning |
|---|---|
| `NA` | Recorded locally, not yet confirmed at Alpaca. |
| `SUBMITTED` | Accepted by Alpaca, nothing filled yet. |
| `PARTIAL` | Partly filled, still working. |
| `FILLED` | Completely filled. |
| `CANCELLED` | Cancelled or rejected (`costs` > 0 if it partly filled first). |
| `CLOSED` | Filled quantity has been closed out. `close_order` skips these, so an order is never closed twice. |

Closing an order does not add a row: the closing market order is recorded on
the original row (status `CLOSED`, new `modified` time, and a `description`
naming the quantity closed and the closing order's id).

`close_orders()` only acts on rows whose `client_order_id` starts with this
`.env`'s `PREFIX` and that are still working or hold a fill. Orders from other
prefixes, and positions not in the database, are left alone.
| `EXPIRED` | Expired at Alpaca (`costs` > 0 if it partly filled first). |
| `SKIPPED` | Never reached Alpaca (e.g. the submit failed). |

## Run

Set `STATE=true` in `.env`, then:

```bash
python src/start.py
```

The script runs once and exits; it does not poll. In production it is run
every 60 seconds by a cron job, for example:

```cron
* * * * * cd /path/to/alpaca && .venv/bin/python src/start.py
```
