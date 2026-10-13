"""Backtest 0dte_gamma_macd on historical Alpaca data with the bot's risk settings.

    python backtests/zero_dte_gamma_macd.py --env C:/projects/alpaca-bot/.env --year 2025

Replays one bot run per minute. Each run uses the strategy's own signal code
on the last 100 closed 15-minute IEX bars, ranks crosses by IEX average daily
volume, and checks today's expiring contracts for positive GEX. Then the
risk.py rules apply: TRADING_START/TRADING_STOP, MAX_OPEN_POSITIONS, no second
option of the same type on an underlying, an opposite one closing the held
option, WAGER sizing, OPTION_FLATTEN_TIME, and the MAX_CONSECUTIVE_LOSS kill
switch. Exits follow the live strategy: the 1-minute MACD reversal, with no
take-profit or stop. --exit-macd-minutes picks another timeframe, and 0
replaces the reversal with TAKE_PROFIT and the STOP_LOSS trailing stop.

Historical option quotes aren't available, so prices are approximations:
- Bid and ask are the last minute-bar trade price -/+ SLIPPAGE.
- Gamma and delta are Black-Scholes values from the implied volatility of each
  contract's last trade. Open interest is Alpaca's figure for the day before expiry.
- The MAX_SPREAD check is replaced by requiring a trade in the last
  LIQUID_MINUTES minutes.
- An option still open at the close settles at intrinsic value.
"""

import argparse
import json
import math
import os
import pickle
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default=str(ROOT / ".env"), help=".env with API keys and risk settings")
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--assets", default="MSFT,AAPL,GOOG,AMZN,META,AVGO,TSLA")
    parser.add_argument("--capital", type=float, default=100_000.0, help="starting buying power")
    parser.add_argument("--slippage", type=float, default=0.02, help="fraction of the trade price paid over/under it")
    parser.add_argument("--cache", default=str(Path(tempfile.gettempdir()) / "alpaca-backtest-cache"))
    parser.add_argument(
        "--exit-macd-minutes",
        type=int,
        default=1,
        help="close an option once MACD on this timeframe crosses back beyond zero against it, e.g. 1, 5 or 15 "
        "(0: use TAKE_PROFIT and the STOP_LOSS trailing stop instead)",
    )
    parser.add_argument("--out", default=None, help="write trades and summary JSON here")
    return parser.parse_args()


ARGS = _parse_args()
# Load the bot's settings before config does, and keep backtest logging out of the live log.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ARGS.env)
os.environ["LOG_DIRECTORY"] = str(Path(ARGS.cache) / "logs")
os.environ["LOG_LEVEL"] = "WARNING"
sys.path.insert(0, str(ROOT / "src"))

from alpaca.data.enums import DataFeed  # noqa: E402
from alpaca.data.historical.option import OptionHistoricalDataClient  # noqa: E402
from alpaca.data.historical.stock import StockHistoricalDataClient  # noqa: E402
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest  # noqa: E402
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit  # noqa: E402
from alpaca.trading.client import TradingClient  # noqa: E402
from alpaca.trading.enums import AssetStatus  # noqa: E402
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest  # noqa: E402

import config  # noqa: E402
from market_data import OptionQuote  # noqa: E402
from market_data.option_quote import black_scholes_delta, black_scholes_gamma  # noqa: E402
from strategies.base import BAR_LIMIT  # noqa: E402
from strategies.zero_dte_gamma_macd import ADV_DAYS  # noqa: E402
from strategies.zero_dte_macd15_atm import CROSS_MINUTES, FRESH_MINUTES, MIN_BARS, zero_line_crosses  # noqa: E402
from strategies.zero_dte_macd_divergence import MARKET_TZ, MAX_DELTA, MIN_DELTA, TARGET_DELTA, chain_gex  # noqa: E402

SESSION_OPEN = time(9, 30)
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
# Strikes farther than this from the open add almost no 0DTE gamma, so their bars aren't fetched.
STRIKE_RANGE = 0.15
LIQUID_MINUTES = 5

CACHE = Path(ARGS.cache)
CACHE.mkdir(parents=True, exist_ok=True)
stock_client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
option_client = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
trading_client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=True)


def cached(name: str, fetch):
    path = CACHE / f"{name}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    value = fetch()
    path.write_bytes(pickle.dumps(value))
    return value


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _stock_bars(symbol: str, timeframe: TimeFrame, start: datetime, end: datetime) -> list[Bar]:
    response = stock_client.get_stock_bars(
        StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, end=end, feed=DataFeed.IEX)
    )
    return [Bar(b.timestamp, b.open, b.high, b.low, b.close, b.volume) for b in response.data.get(symbol, [])]


def year_bars(symbol: str, timeframe: TimeFrame, label: str) -> list[Bar]:
    start = datetime(ARGS.year - 1, 10, 1, tzinfo=timezone.utc)
    end = datetime(ARGS.year + 1, 1, 1, tzinfo=timezone.utc)
    return cached(f"{symbol}-{label}-{ARGS.year}", lambda: _stock_bars(symbol, timeframe, start, end))


def reversal_bars(symbol: str, day: date, minutes: int) -> list[Bar]:
    """`minutes` bars from a few days before `day` through its close, enough for a 100-bar MACD at the open."""
    start = datetime.combine(day, time(0), MARKET_TZ) - timedelta(days=5)
    end = datetime.combine(day, time(20), MARKET_TZ)
    return cached(
        f"{symbol}-{minutes}m-{day}",
        lambda: _stock_bars(symbol, TimeFrame(minutes, TimeFrameUnit.Minute), start, end),
    )


def minute_bars(symbol: str, day: date) -> dict[datetime, Bar]:
    start = datetime.combine(day, SESSION_OPEN, MARKET_TZ)
    bars = cached(f"{symbol}-1m-{day}", lambda: _stock_bars(symbol, TimeFrame.Minute, start, start + timedelta(hours=7)))
    return {bar.timestamp: bar for bar in bars}


@dataclass(frozen=True)
class Contract:
    symbol: str
    side: str
    strike: float
    open_interest: float


def expiring_contracts(symbol: str) -> dict[date, list[Contract]]:
    def fetch():
        contracts, token = [], None
        while True:
            response = trading_client.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[symbol],
                    status=AssetStatus.INACTIVE,
                    expiration_date_gte=date(ARGS.year, 1, 1),
                    expiration_date_lte=date(ARGS.year, 12, 31),
                    limit=10000,
                    page_token=token,
                )
            )
            contracts += response.option_contracts or []
            token = response.next_page_token
            if not token:
                break
        by_day = defaultdict(list)
        for c in contracts:
            if c.root_symbol != symbol:
                continue  # adjusted contracts after corporate actions
            by_day[c.expiration_date].append(
                Contract(c.symbol, c.type.value, float(c.strike_price), float(c.open_interest or 0))
            )
        return dict(by_day)

    return cached(f"{symbol}-contracts-{ARGS.year}", fetch)


def option_minute_bars(underlying: str, day: date, contracts: list[Contract], spot: float) -> dict[str, list]:
    """Minute bars on `day` for contracts within STRIKE_RANGE of `spot`: symbol -> [(timestamp, close)]."""

    def fetch():
        symbols = [c.symbol for c in contracts if abs(c.strike / spot - 1) <= STRIKE_RANGE]
        start = datetime.combine(day, SESSION_OPEN, MARKET_TZ)
        result = {}
        for i in range(0, len(symbols), 100):
            response = option_client.get_option_bars(
                OptionBarsRequest(
                    symbol_or_symbols=symbols[i : i + 100],
                    timeframe=TimeFrame.Minute,
                    start=start,
                    end=start + timedelta(hours=7),
                )
            )
            for key, bars in response.data.items():
                result[key] = [(bar.timestamp, float(bar.close)) for bar in bars]
        return result

    return cached(f"{underlying}-options-{day}", fetch)


def bs_price(side: str, spot: float, strike: float, vol: float, years: float) -> float:
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
    call = spot * n(d1) - strike * n(d2)
    return call if side == "call" else call - spot + strike


def implied_vol(side: str, price: float, spot: float, strike: float, years: float):
    intrinsic = max(spot - strike, 0) if side == "call" else max(strike - spot, 0)
    if price <= intrinsic + 0.005:
        return None
    low, high = 0.01, 10.0
    if bs_price(side, spot, strike, high, years) < price:
        return None
    for _ in range(60):
        mid = (low + high) / 2
        if bs_price(side, spot, strike, mid, years) > price:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def years_left(now: datetime, day: date) -> float:
    expires = datetime.combine(day, time(16), MARKET_TZ)
    return max((expires - now).total_seconds(), 60) / SECONDS_PER_YEAR


class Day:
    """One underlying's data for one session, with lookups as of a run time."""

    def __init__(self, symbol: str, day: date, contracts: list[Contract], open_spot: float):
        self.symbol, self.day = symbol, day
        self.contracts = {c.symbol: c for c in contracts}
        self.spots = minute_bars(symbol, day)
        self.options = option_minute_bars(symbol, day, contracts, open_spot)

    def spot(self, now: datetime):
        """Close of the last 1-minute bar closed by `now` (the live trigger-bar price)."""
        for minutes in range(1, 30):
            bar = self.spots.get(now - timedelta(minutes=minutes))
            if bar is not None:
                return float(bar.close)
        return None

    def last_trade(self, contract: str, now: datetime):
        """(timestamp, price) of the contract's last minute bar closed by `now`."""
        last = None
        for timestamp, price in self.options.get(contract, []):
            if timestamp + timedelta(minutes=1) > now:
                break
            last = (timestamp, price)
        return last

    def chain(self, now: datetime, spot: float) -> list[OptionQuote]:
        quotes = []
        for contract in self.contracts.values():
            gamma = delta = None
            bid = ask = 0.0
            trade = self.last_trade(contract.symbol, now)
            if trade is not None:
                traded_at, price = trade
                trade_spot = self.spot(traded_at + timedelta(minutes=1)) or spot
                vol = implied_vol(contract.side, price, trade_spot, contract.strike, years_left(traded_at, self.day))
                if vol is not None:
                    years = years_left(now, self.day)
                    gamma = black_scholes_gamma(spot, contract.strike, vol, years)
                    delta = black_scholes_delta(contract.side, spot, contract.strike, vol, years)
                if now - traded_at <= timedelta(minutes=LIQUID_MINUTES + 1):
                    bid, ask = price * (1 - ARGS.slippage), price * (1 + ARGS.slippage)
            quotes.append(
                OptionQuote(contract.symbol, contract.side, contract.strike, bid, ask, contract.open_interest, gamma, delta, spot)
            )
        return quotes

    def bid(self, contract: str, now: datetime):
        trade = self.last_trade(contract, now)
        return None if trade is None else trade[1] * (1 - ARGS.slippage)


def select_contract(direction: str, spot: float, chain: list[OptionQuote]):
    """The live select_contract, with a recent trade standing in for the spread check."""
    side = "call" if direction == "long" else "put"
    contracts = [
        q
        for q in chain
        if q.side == side
        and (q.strike > spot if side == "call" else q.strike < spot)
        and q.delta is not None
        and MIN_DELTA <= abs(q.delta) <= MAX_DELTA
        and q.ask > 0
    ]
    return min(contracts, key=lambda q: abs(TARGET_DELTA - abs(q.delta)), default=None)


def in_window(now: datetime) -> bool:
    current = now.astimezone(MARKET_TZ).time()
    start, stop = config.TRADING_START, config.TRADING_STOP
    if start is not None and stop is not None and start > stop:
        return current >= start or current < stop
    return (start is None or current >= start) and (stop is None or current < stop)


@dataclass
class Position:
    contract: str
    underlying: str
    side: str
    qty: int
    entry_price: float
    entry_time: datetime
    description: str
    stop: float = None
    managed_from: datetime = None


@dataclass
class Account:
    cash: float
    positions: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    consecutive_losses: int = 0
    # Times MAX_CONSECUTIVE_LOSS would have switched STATE off; the replay assumes a manual reset each time.
    kill_switch_trips: list = field(default_factory=list)

    def close(self, position: Position, price: float, now: datetime, reason: str):
        proceeds = price * 100 * position.qty
        self.cash += proceeds
        self.positions.remove(position)
        pnl = proceeds - position.entry_price * 100 * position.qty
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        self.trades.append(
            {
                "underlying": position.underlying,
                "contract": position.contract,
                "side": position.side,
                "qty": position.qty,
                "entry_time": position.entry_time.astimezone(MARKET_TZ).isoformat(),
                "entry": round(position.entry_price, 2),
                "exit_time": now.astimezone(MARKET_TZ).isoformat(),
                "exit": round(price, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
                "cash_after": round(self.cash, 2),
                "description": position.description,
            }
        )
        if 0 < config.MAX_CONSECUTIVE_LOSSES <= self.consecutive_losses:
            self.kill_switch_trips.append(now)
            self.consecutive_losses = 0


def main():
    symbols = [s.strip().upper() for s in ARGS.assets.split(",") if s.strip()]
    print(f"Loading {ARGS.year} data for {', '.join(symbols)}", flush=True)
    fifteen = {s: year_bars(s, TimeFrame(CROSS_MINUTES, TimeFrameUnit.Minute), "15m") for s in symbols}
    daily = {s: year_bars(s, TimeFrame.Day, "1d") for s in symbols}
    expiries = {s: expiring_contracts(s) for s in symbols}

    # Fresh crosses: the run time a strategy would first see each 15m bar close.
    crosses = defaultdict(dict)  # bar close time -> {symbol: (direction, bar timestamp, close)}
    for s, bars in fifteen.items():
        for i in range(MIN_BARS - 1, len(bars)):
            window = bars[max(0, i + 1 - BAR_LIMIT) : i + 1]
            if len(window) < MIN_BARS:
                continue
            closed_at = bars[i].timestamp + timedelta(minutes=CROSS_MINUTES)
            if closed_at.astimezone(MARKET_TZ).year != ARGS.year:
                continue
            direction = zero_line_crosses(window)[-1]
            if direction is not None:
                crosses[closed_at][s] = (direction, bars[i].timestamp, float(bars[i].close))

    def adv(s: str, day: date) -> float:
        volumes = [b.volume for b in daily[s] if b.timestamp.astimezone(MARKET_TZ).date() < day][-ADV_DAYS:]
        return sum(volumes) / len(volumes) if volumes else 0.0

    calendar = cached(
        f"calendar-{ARGS.year}",
        lambda: trading_client.get_calendar(GetCalendarRequest(start=date(ARGS.year, 1, 1), end=date(ARGS.year, 12, 31))),
    )
    closes = {c.date: datetime.combine(c.date, c.close.time() if isinstance(c.close, datetime) else c.close, MARKET_TZ) for c in calendar}
    account = Account(cash=ARGS.capital)
    claimed = set()
    stats = defaultdict(int)
    flatten_at = time.fromisoformat(config.OPTION_FLATTEN_TIME)
    days: dict = {}
    reversal_cache: dict = {}

    def trend_reversed(position: Position, now: datetime) -> bool:
        """Whether the latest closed bar since entry has a zero-line MACD cross against the option."""
        minutes = ARGS.exit_macd_minutes
        day = now.astimezone(MARKET_TZ).date()
        if (position.underlying, day) not in reversal_cache:
            source = fifteen[position.underlying] if minutes == CROSS_MINUTES else reversal_bars(position.underlying, day, minutes)
            reversal_cache[(position.underlying, day)] = source
        bars = [b for b in reversal_cache[(position.underlying, day)] if b.timestamp + timedelta(minutes=minutes + 1) <= now]
        bars = bars[-BAR_LIMIT:]
        if len(bars) < MIN_BARS or bars[-1].timestamp + timedelta(minutes=minutes) <= position.entry_time:
            return False
        key = (position.underlying, minutes, bars[-1].timestamp)
        if key not in reversal_cache:
            reversal_cache[key] = zero_line_crosses(bars)[-1]
        return reversal_cache[key] == ("short" if position.side == "call" else "long")

    def day_data(s: str, day: date):
        if (s, day) not in days:
            contracts = expiries[s].get(day, [])
            opening = next((b for b in fifteen[s] if b.timestamp >= datetime.combine(day, SESSION_OPEN, MARKET_TZ)), None)
            days[(s, day)] = Day(s, day, contracts, float(opening.open)) if contracts and opening else None
        return days[(s, day)]

    for day, close_time in sorted(closes.items()):
        stats["sessions"] += 1
        if any(expiries[s].get(day) for s in symbols):
            stats["sessions_with_0dte"] += 1
        session_crosses = {t: c for t, c in crosses.items() if t.astimezone(MARKET_TZ).date() == day}
        if not session_crosses and not account.positions:
            continue
        now = datetime.combine(day, SESSION_OPEN, MARKET_TZ)
        while now < close_time:
            now += timedelta(minutes=1)
            # risk._manage_option_positions runs before entries each run.
            for position in list(account.positions):
                data = day_data(position.underlying, day)
                bid = data.bid(position.contract, now) if data else None
                if now.astimezone(MARKET_TZ).time() >= flatten_at:
                    account.close(position, bid if bid is not None else 0.0, now, f"flatten {config.OPTION_FLATTEN_TIME}")
                    continue
                if bid is None or now < position.managed_from:
                    continue
                if ARGS.exit_macd_minutes:
                    if trend_reversed(position, now):
                        account.close(position, bid, now, f"{ARGS.exit_macd_minutes}m MACD reversal")
                    continue
                if config.TAKE_PROFIT > 0 and bid >= position.entry_price * (1 + config.TAKE_PROFIT):
                    account.close(position, position.entry_price * (1 + config.TAKE_PROFIT), now, "take profit")
                    continue
                if config.STOP_LOSS > 0:
                    if position.stop is not None and bid <= position.stop:
                        account.close(position, bid, now, "trailing stop")
                        continue
                    new_stop = round(bid * (1 - config.STOP_LOSS), 2)
                    if position.stop is None or new_stop > position.stop:
                        position.stop = new_stop

            # Strategy: candidates whose 15m bar closed within FRESH_MINUTES, ranked by ADV.
            candidates = {}
            for closed_at, by_symbol in session_crosses.items():
                if timedelta(minutes=1) <= now - closed_at <= timedelta(minutes=FRESH_MINUTES):
                    candidates.update(by_symbol)
            if not candidates:
                continue
            signal = None
            for s in sorted(candidates, key=lambda s: adv(s, day), reverse=True):
                direction, bar_timestamp, close = candidates[s]
                data = day_data(s, day)
                if data is None:
                    stats["skipped_no_0dte"] += 1
                    continue
                spot = data.spot(now) or close
                chain = data.chain(now, spot)
                gex = chain_gex(chain, spot)
                if gex is None or gex <= config.GEX_THRESHOLD:
                    stats["skipped_not_positive_gamma"] += 1
                    continue
                quote = select_contract(direction, spot, chain)
                if quote is None:
                    stats["skipped_no_contract"] += 1
                    continue
                signal = (s, direction, bar_timestamp, quote, gex)
                break
            if signal is None:
                continue

            # risk.process_strategy_signals
            s, direction, bar_timestamp, quote, gex = signal
            if (quote.symbol, bar_timestamp) in claimed:
                continue
            claimed.add((quote.symbol, bar_timestamp))
            stats["signals"] += 1
            held = [p for p in account.positions if p.underlying == s]
            if any(p.side == quote.side for p in held):
                stats["blocked_same_type_held"] += 1
                continue
            if held:
                for p in held:
                    account.close(p, day_data(s, day).bid(p.contract, now) or 0.0, now, "opposite signal")
                stats["reversal_closes"] += 1
                continue
            if len(account.positions) >= config.MAX_OPEN_POSITIONS:
                stats["blocked_max_positions"] += 1
                continue
            if not in_window(now):
                stats["blocked_outside_window"] += 1
                continue
            qty = math.floor(account.cash * config.WAGER / (quote.ask * 100))
            if qty < 1:
                stats["blocked_wager_too_small"] += 1
                continue
            account.cash -= quote.ask * 100 * qty
            account.positions.append(
                Position(
                    quote.symbol,
                    s,
                    quote.side,
                    qty,
                    quote.ask,
                    now,
                    f"{direction} cross, GEX {gex:,.0f}, delta {quote.delta:.2f}, spot {spot:.2f}",
                    managed_from=now + timedelta(minutes=1),
                )
            )
            stats["entries"] += 1

        # Anything left at the close (e.g. a half day before OPTION_FLATTEN_TIME) settles at intrinsic value.
        for position in list(account.positions):
            data = day_data(position.underlying, day)
            spot = data.spot(close_time + timedelta(minutes=1)) if data else None
            strike = data.contracts[position.contract].strike if data else 0
            value = 0.0 if spot is None else max(spot - strike, 0) if position.side == "call" else max(strike - spot, 0)
            account.close(position, value, close_time, "expired at the close")
        print(f"{day} cash {account.cash:,.0f} trades {len(account.trades)}", end="\r", flush=True)

    report(account, stats)


def report(account: Account, stats: dict):
    trades = account.trades
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    equity, peak, drawdown = ARGS.capital, ARGS.capital, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    trips = account.kill_switch_trips
    halted = trips[0] if trips else None
    halted_pnl = sum(t["pnl"] for t in trades if halted is None or t["exit_time"] <= halted.astimezone(MARKET_TZ).isoformat())
    by_underlying = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_underlying[t["underlying"]][0] += 1
        by_underlying[t["underlying"]][1] += t["pnl"]
    by_reason = defaultdict(int)
    for t in trades:
        by_reason[t["reason"]] += 1
    summary = {
        "settings": {
            "WAGER": config.WAGER,
            "TAKE_PROFIT": None if ARGS.exit_macd_minutes else config.TAKE_PROFIT,
            "STOP_LOSS": None if ARGS.exit_macd_minutes else config.STOP_LOSS,
            "MAX_CONSECUTIVE_LOSS": config.MAX_CONSECUTIVE_LOSSES,
            "MAX_OPEN_POSITIONS": config.MAX_OPEN_POSITIONS,
            "TRADING_START": str(config.TRADING_START),
            "TRADING_STOP": str(config.TRADING_STOP),
            "OPTION_FLATTEN_TIME": config.OPTION_FLATTEN_TIME,
            "GEX_THRESHOLD": config.GEX_THRESHOLD,
            "MIN_DELTA": MIN_DELTA,
            "MAX_DELTA": MAX_DELTA,
            "capital": ARGS.capital,
            "slippage": ARGS.slippage,
            "exit_macd_minutes": ARGS.exit_macd_minutes,
        },
        "stats": dict(stats),
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None,
        "net_pnl": round(sum(pnls), 2),
        "return": round(sum(pnls) / ARGS.capital, 4),
        "max_drawdown": round(drawdown, 4),
        "kill_switch_trips": [trip.astimezone(MARKET_TZ).isoformat() for trip in trips],
        "net_pnl_until_first_trip": round(halted_pnl, 2),
        "by_underlying": {k: {"trades": v[0], "pnl": round(v[1], 2)} for k, v in sorted(by_underlying.items())},
        "exit_reasons": dict(by_reason),
    }
    print()
    print(json.dumps(summary, indent=2))
    if ARGS.out:
        Path(ARGS.out).write_text(json.dumps({"summary": summary, "trades": trades}, indent=2))


if __name__ == "__main__":
    main()
