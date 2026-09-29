# alpaca — project context

Progress notes and decisions, kept up to date as work happens. See `README.md`
for setup and configuration.

## Status

Scaffolding, config, logging, the `Trade` wrapper and SQLite order tracking are
in place. Nothing has been run against a real Alpaca account yet; all checks so
far used mocked clients or no network. `start.py` checks `STATE` and the keys,
then syncs pending orders — no trading strategy yet. Initial commit 0d61199 on main (local only, no remote).

## Execution model

`start.py` does not poll or loop. A cron job runs it every 60 seconds; each run
does its work and exits. So nothing stays in memory between runs. State that
must carry over lives in the `orders` table (`src/db.py`). Don't add
sleep/poll loops.

Each run: `STATE` check → key check → `Trade.sync_orders()` → (strategy, TBD).

## Files

- `src/config.py` — loads `.env` and `src/config.json`, sets up logging on
  import, `uuid7()`, `new_client_order_id()`.
- `src/config.json` — `demo` / `prod` trading base URLs.
- `src/db.py` — `OrderDB`: SQLite `orders` table (`save` upsert, `get`, `by_status`).
- `src/trade.py` — `Trade(key, secret, url, db)` wrapping alpaca-py; records every action in `db`.
- `src/start.py` — entry point.
- `.env` — keys, `DEMO`, `STATE`, log settings, `PREFIX`, `DB_PATH` (gitignored).
- `data/alpaca.db` — default database location (gitignored).
- `.venv/` — local virtualenv with `requirements.txt` installed (alpaca-py 0.44.0, Python 3.13).

## Trade methods

| Method | Behavior |
|---|---|
| `sync_orders()` | Re-fetches every `NA`/`SUBMITTED`/`PARTIAL` row from Alpaca and updates status + costs. `NA` rows Alpaca returns 404 for become `SKIPPED`. |
| `get_balance()` | Dict of currency, cash, equity, buying power, portfolio value. |
| `place_order(symbol, side, qty=None, notional=None, limit_price=None, time_in_force="day", client_order_id=None, description=None)` | Market order, or limit if `limit_price`. Exactly one of `qty`/`notional`. Tag defaults to `<PREFIX>-<uuid7>`. Saves `NA` before submitting, then the Alpaca status. |
| `close_order(client_order_id)` | Skips if the row is `CLOSED`. Otherwise cancels unfilled remainder, closes the filled qty at market, and updates the same row: `CLOSED`, new `modified`, description naming qty + closing order id. Nothing filled → `CANCELLED` with a description. |
| `close_orders()` | Runs `close_order` on every row with this `PREFIX` that is `SUBMITTED`/`PARTIAL`/`FILLED`, or `CANCELLED`/`EXPIRED` with costs > 0. Other prefixes and non-DB positions untouched. One failure is logged and the rest continue. Returns the closing orders. |
| `cancel_order(client_order_id)` | Cancels one order; records current state (final state arrives on next sync). |
| `cancel_orders()` | Cancels all open orders, then `sync_orders()`. |
| `get_asset_data(symbol)` | Asset details + latest bid/ask (stock or crypto data client). |

## Decisions

- **Order tracking in SQLite** (user's design): columns `client_order_id` (PK),
  `hostname`, `status`, `created`, `modified`, `description`, `costs`.
  Statuses `NA, SUBMITTED, PARTIAL, FILLED, CANCELLED, CLOSED, EXPIRED, SKIPPED`
  enforced by a CHECK constraint. Timestamps are ISO 8601 UTC with `Z`.
  One row per order: closing orders are not given their own row (user decision).
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
  A custom expiry could now be built on the `orders` table.
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
