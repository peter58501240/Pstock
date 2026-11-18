"""Historical-data integrity gate for daily Taiwanese stock screening.

Benchmark calendars contain actual trading sessions (not guessed weekdays).
Only the most recent uninterrupted session sequence can enter indicators.
Do not manufacture prices for trading halts, absent quotes, or failed downloads.
"""

from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import math


def valid_bar(row):
    if not isinstance(row, (list, tuple)) or len(row) != 6:
        return False
    if not isinstance(row[0], str) or len(row[0]) != 10:
        return False
    try:
        date.fromisoformat(row[0])
        o, high, low, close, volume = (float(v) for v in row[1:])
        return (all(math.isfinite(v) for v in (o, high, low, close, volume))
                and o > 0 and close > 0 and low > 0
                and high >= max(o, close, low)
                and low <= min(o, close, high)
                and volume >= 0)
    except (TypeError, ValueError, OverflowError):
        return False


def recent_continuous_history(bars, market_calendar, trade_day, min_bars=70):
    """Return (status, usable_bars, missing_dates).

    The calendar is obtained independently from liquid, continuously traded
    exchange benchmarks, plus the verified latest exchange snapshot.

    'ready' means >= min_bars *actual consecutive market sessions* after the
    most recent gap, rather than merely >= min_bars cached observations.
    'short' means fewer than min_bars sessions since listing or first quote.
    'gap' means a missing exchange session inside a stock's observed range.
    'stale' means the stock has no valid latest official daily bar.
    """
    calendar = sorted({d for d in market_calendar if isinstance(d, str)
                       and d <= trade_day})
    if len(calendar) < min_bars or calendar[-1] != trade_day:
        return "calendar_unavailable", [], []
    by_date = {b[0]: b for b in bars if valid_bar(b) and b[0] <= trade_day}
    if trade_day not in by_date:
        return "stale", [], [trade_day]
    first = min(by_date)
    tail = []
    gap = None
    for day in reversed(calendar):
        if day < first:
            break
        if day in by_date:
            tail.append(by_date[day])
        else:
            gap = day
            break
    tail.reverse()
    if len(tail) >= min_bars:
        return "ready", tail, []
    if gap is not None:
        return "gap", tail, [gap]
    return "short", tail, []


def recent_reference_is_verified(last_reference_day, trade_day):
    """Reject a calendar with an unverified weekday between source and today.

    On the day after a market holiday, this can conservatively fail closed
    until Yahoo catches up; it must never silently compress trading days.
    """
    try:
        last = date.fromisoformat(last_reference_day)
        today = date.fromisoformat(trade_day)
        if last > today:
            return False
        cursor = last + timedelta(days=1)
        while cursor < today:
            if cursor.weekday() < 5:
                return False
            cursor += timedelta(days=1)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def refresh_prices(eligible, history, fetcher, limit, cursor="", workers=4):
    """Refresh at most 'limit' histories, rotating across the ENTIRE universe.

    A persistent lexicographic ticker cursor avoids starving lower-turnover
    names when eligible stocks exceed the daily request cap. Every eligible
    stock is periodically re-fetched to repair missed sessions. Current-day
    official bars are merged into all cached names independently.
    """
    ordered = sorted(eligible, key=lambda x: x["code"])
    last_code = str(cursor or "")
    start = next((i for i, stock in enumerate(ordered)
                  if stock["code"] > last_code), 0)
    count = min(len(ordered), max(0, int(limit)))
    targets = [ordered[(start + i) % len(ordered)] for i in range(count)] if ordered else []
    next_cursor = targets[-1]["code"] if targets else last_code
    failed, refreshed = [], set()
    if not targets:
        return refreshed, failed, 0, next_cursor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetcher, stock): stock for stock in targets}
        for task in as_completed(futures):
            stock = futures[task]
            try:
                bars = task.result()
                if not bars or not all(valid_bar(b) for b in bars):
                    raise ValueError("歷史日 K 無效或為空")
                history[stock["code"]] = bars
                refreshed.add(stock["code"])
            except Exception as exc:
                failed.append((stock["code"], str(exc)))
    return refreshed, sorted(failed), len(targets), next_cursor


def self_test():
    start = date(2026, 1, 5)
    dates = [(start + timedelta(days=i)).isoformat()
             for i in range(120)
             if (start + timedelta(days=i)).weekday() < 5]
    # Include the latest official session; dates exclude weekends naturally.
    complete = [[d, 30, 31, 29, 30.5, 1_200_000] for d in dates]
    status, tail, gaps = recent_continuous_history(complete, dates, dates[-1])
    assert status == "ready" and len(tail) == len(dates) and not gaps

    # Had 80+ cached bars, skipped a non-eligible trading session, then
    # appended today's official bar. Length and latest-date checks would pass.
    missing_date = dates[-4]
    missing = [b for b in complete if b[0] != missing_date]
    assert len(missing) >= 70 and missing[-1][0] == dates[-1]
    status, tail, gaps = recent_continuous_history(missing, dates, dates[-1])
    assert status == "gap" and gaps == [missing_date], (status, gaps)

    # A later eligible-day Yahoo backfill must repair the missing session.
    sample = [{"code": "2330", "volume": 1_000_000, "close": 30}]
    history = {"2330": missing}
    refreshed, failed, count, cursor = refresh_prices(sample, history,
                                             lambda stock: complete, 1)
    assert count == 1 and refreshed == {"2330"} and not failed
    status, tail, gaps = recent_continuous_history(history["2330"], dates, dates[-1])
    assert status == "ready" and len(tail) == len(dates)

    # A Yahoo outage must NOT make an old cache look valid.
    history = {"2330": missing}
    def broken_fetch(stock):
        raise RuntimeError("mock Yahoo timeout")
    refreshed, failed, count, cursor = refresh_prices(sample, history, broken_fetch, 1)
    assert not refreshed and len(failed) == 1
    assert recent_continuous_history(history["2330"], dates, dates[-1])[0] == "gap"

    # A newly listed 30-session history is short, not a download failure.
    assert recent_continuous_history(complete[-30:], dates, dates[-1])[0] == "short"

    # A long trading halt that ended >70 sessions ago does not contaminate
    # modern indicators; the latest 70+ consecutive bars are still valid.
    old_missing = [b for b in complete if b[0] != dates[2]]
    assert recent_continuous_history(old_missing, dates, dates[-1])[0] == "ready"

    # Rotation is persistent and must eventually include every eligible name,
    # not permanently select the highest turnover cohort.
    stocks = [{"code": code, "volume": 1_000_000, "close": 30}
              for code in ("1101", "1102", "1103", "1104", "1105")]
    seen = set()
    cursor = ""
    cache = {}
    for _ in range(3):
        downloaded, failed, n, cursor = refresh_prices(
            stocks, cache, lambda stock: complete, 2, cursor=cursor, workers=2)
        assert n == 2 and not failed
        seen.update(downloaded)
    assert seen == {s["code"] for s in stocks}, (seen, cursor)

    # Freshness needs independent proof between benchmark and official day.
    assert recent_reference_is_verified("2026-11-17", "2026-11-18")
    assert recent_reference_is_verified("2026-11-13", "2026-11-16")
    assert not recent_reference_is_verified("2026-11-16", "2026-11-18")
    assert not recent_reference_is_verified("2026-11-13", "2026-11-18")

    # Wrong latest date, and calendar quality failures, are rejected.
    assert recent_continuous_history(complete[:-1], dates, dates[-1])[0] == "stale"
    assert recent_continuous_history(complete, dates[-20:], dates[-1])[0] == "calendar_unavailable"
    assert valid_bar([dates[-1], 30, 29, 31, 30.5, 100]) is False
    print("HISTORY INTEGRITY SELF TEST PASS")


if __name__ == "__main__":
    self_test()
