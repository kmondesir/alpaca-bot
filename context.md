# alpaca — project context

Progress notes and decisions, kept up to date as work happens. See `README.md`
for setup and configuration.

## Status

Scaffolding, config, logging, the `Trade` wrapper, SQLite order tracking, and a
MACD/Parabolic SAR signal strategy are in place. Nothing has been run against a
real Alpaca account; checks use mocked clients or no network. `start.py` checks
`STATE` and credentials, syncs orders, then routes strategy signals through
`risk.py`. Initial commit 0d61199 on main (local only, no remote).

## Execution model

`start.py` runs once per cron tick and exits. Entry and protective order state
lives in `parent` and `child` tables; deduplicated signal claims live in the
risk-owned `strategy_signals` table. Do not add polling loops.

Each run: `STATE` check → key check → `Trade.sync_orders()` → signal generation
→ risk state/market gates → order, close, and protection handling.

## Files

- `src/config.py` — loads `.env`, sets up logging, and provides UUID helpers.
- `src/config.json` — demo and production trading base URLs.
- `src/db.py` — `OrderDB` for entry parents, protective children, and parent close details.
- `src/risk.py` — tracks losses, atomically claims signals, gates trades, and handles reversals/protection.
- `src/strategy.py` — generates MACD crossover signals confirmed by Parabolic SAR; it does not execute trades.
- `src/trade.py` — Alpaca trading/data wrapper; records entries on parents and protective exits on children.
- `src/start.py` — checks `STATE` and credentials, syncs orders, then passes signals to risk.
- `.env` — credentials, `STATE`, `STRATEGY_SYMBOLS`, and risk/log settings; gitignored.
- `data/alpaca.db` — default SQLite database location; gitignored.
- `.venv/` — project virtual environment.

## Trade methods

| Method                   | Behavior                                                                                                          |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `sync_orders()`          | Refreshes pending parent entries, protective children, and parent close orders.                                   |
| `get_balance()`          | Returns account cash, equity, buying power, and portfolio value.                                                  |
| `place_order(...)`       | Creates a parent entry by default; supplying a parent and protective role creates a child. Records before submit. |
| `open_position(...)`     | Sizes a long or short entry using `WAGER`.                                                                        |
| `protect_position(...)`  | Creates linked take-profit and trailing-stop children for a parent.                                               |
| `close_order(parent_id)` | Stores the close order ID, status, quantity, and costs on the parent.                                             |
| `close_orders()`         | Closes eligible parent positions tagged with this `PREFIX`.                                                       |
| `cancel_order(order_id)` | Cancels one Alpaca order and updates its parent or child record.                                                  |
| `get_asset_data(symbol)` | Returns asset details and the latest bid/ask quote.                                                               |

## Decisions

- **Parent/child order tracking in SQLite** (user's design): each `parent` row
  represents an entry and overall position; each protective take-profit or
  trailing-stop order is a linked `child` row. Manual/reversal close details
  live on the parent. Both protective children remain for history after one
  fills and the sibling is canceled.
- **Fresh schema:** `OrderDB` drops the unused flat `orders` table on initialization and creates `parent` and `child`; no migration is performed.
- **Status mapping** from Alpaca: filled→FILLED, expired→EXPIRED,
  canceled/rejected/replaced→CANCELLED, anything else→PARTIAL if
  filled_qty > 0 else SUBMITTED. `CLOSED` is only set by our close methods.
- **`costs`** = filled_qty × filled_avg_price (no fees; Alpaca stock trades are
  commission-free, crypto fees aren't included).
- **Record before submit**: `place_order` saves `NA` first so a submit that
  times out after reaching Alpaca is still found by the next sync. `SKIPPED`
  means Alpaca never received it.
- **`DONE_FOR_DAY` is not terminal** — removed from `_DONE_STATUSES` so
  `close_order` cancels those (they resume next trading day).
- **`client_order_id` = `<PREFIX>-<uuid7>`**; `PREFIX` ≤ 91 chars so the tag fits
  Alpaca's 128-char limit. uuid7 is time-ordered, so tags sort by creation.
  Alpaca cannot filter lists by tag, so the DB is the index.
- **No custom expiry** in Alpaca's time-in-force (`day`, `gtc`, `opg`, `cls`,
  `ioc`, `fok`; `gtc` auto-cancels after 90 days; crypto allows only `gtc`/`ioc`).
  A custom expiry could be added to the parent lifecycle if needed.
- **`DEMO`** (was `ALPACA_PAPER`) selects `config.BASE_URL` from `config.json`;
  defaults to `true`. URLs omit `/v2` because the SDK appends it.
- **`STATE`** is a kill switch; defaults to `false` so nothing runs unless set.
- **Missing keys**: `start.py` logs an error and exits (the SDK raises otherwise).
- **Logging**: rotating file + console, UTC timestamps, one uuid7 per record
  (set in a LogRecordFactory so both handlers share it). Custom `uuid7()` in
  `config.py` because Python 3.13 lacks `uuid.uuid7`.
- **Settings live in `.env`**, not `config.json` (moved at user's request).

## Known gaps / open questions

- `close_order` race: a fill landing between cancel and re-fetch is missed; the
  row is marked `CLOSED` with the smaller qty, leaving that extra fill as an
  untracked position.
- **Overlapping runs (deferred by user, keep in mind):** if a run takes longer than
  60s, cron starts another one while it's still going. Both would act on the same orders (e.g. both
  could pass the `CLOSED` check and double-close); `RotatingFileHandler` isn't
  multi-process safe. A lock file at startup would prevent this. Revisit when
  adding strategy logic or anything slow.
- No `.env.example` yet; `.gitignore` excludes `.env.*`, so one would need an exception.
- Not built yet (offered): expiry tracking.
- No tests directory yet (lifecycle was checked with a mocked client + temp DB).

## Log

- 2026-09-28 — Scaffolded project; added `Trade` with balance/order/close/asset methods.
- 2026-09-29 — Added `cancel_order(s)`; switched close/cancel to `client_order_id`;
  added `client_order_id` to `place_order`; rotating log in `config.py`;
  renamed `ALPACA_PAPER` → `DEMO`; added `STATE`; moved log settings to `.env`;
  `DEMO` picks demo/prod URL from `config.json`; README updated.
  Noted execution model: cron every 60s, no polling.
- 2026-09-29 — Added SQLite order tracking (`db.py`), `PREFIX` + `DB_PATH`,
  `sync_orders()` at start of each run, `description` on `place_order`,
  idempotent `close_order`; key check in `start.py`; README + context updated.
- 2026-09-29 — `close_orders()` scoped to `PREFIX` (per-row `close_order`, no
  more account-wide `close_all_positions`); closes update the original row's
  status/modified/description instead of adding a row (user decision).
