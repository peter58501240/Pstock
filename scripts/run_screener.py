#!/usr/bin/env python3
"""Pstock daily market screener. Stdlib only; informational watchlist, not trade orders.

Daily official TWSE/TPEx snapshots define the universe and the last price.
Yahoo daily OHLCV initializes per-stock history; subsequent runs use a GHA cache
and merge official snapshots. Never score stale, short, or missing series.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
import os
from pathlib import Path
import re
import statistics
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TZ = timezone(timedelta(hours=8))
USER_AGENT = "Mozilla/5.0 (compatible; Pstock-Screener/1.0)"
TWSE = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
CACHE_PATH = Path(os.getenv("SCREENER_HISTORY_PATH", "/tmp/pstock-screener-history.json.gz"))
OUTPUT = Path("data/screener.json")
MIN_BARS = 70
MAX_BOOTSTRAP = int(os.getenv("SCREENER_BOOTSTRAP_LIMIT", "650"))


def num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(str(value).replace(",", "").strip())
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def iso_date(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 7:
        return f"{int(digits[:3])+1911:04d}-{digits[3:5]}-{digits[5:]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    if len(digits) >= 9 and "/" in str(raw):
        parts = str(raw).strip().split("/")
        if len(parts) == 3 and all(s.isdigit() for s in parts):
            y = int(parts[0])
            return f"{y+1911 if y<1911 else y:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return None


def get_json(url, attempts=2, timeout=26):
    last = None
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as res:
                raw = res.read()
            obj = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(obj, (list, dict)):
                raise ValueError("unexpected payload")
            return obj
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def normalize_snapshot(raw_rows, market):
    """Return only four-digit ordinary-stock codes, valid OHLCV, and dates."""
    result = {}
    for r in raw_rows:
        code = str(r.get("Code") if market == "上市" else r.get("SecuritiesCompanyCode", "")).strip()
        if not re.fullmatch(r"[1-9]\d{3}", code) or code.startswith("91"):
            continue
        if code.startswith("28"):  # preserve original non-financial stock universe
            continue
        name = str(r.get("Name") if market == "上市" else r.get("CompanyName", "")).strip()
        day = iso_date(r.get("Date"))
        keys = (("OpeningPrice", "HighestPrice", "LowestPrice", "ClosingPrice", "TradeVolume")
                if market == "上市" else ("Open", "High", "Low", "Close", "TradingShares"))
        vals = [num(r.get(k)) for k in keys]
        if not day or any(v is None for v in vals):
            continue
        o, h, lo, c, v = vals
        if not (c > 0 and o > 0 and h >= max(o, c, lo) and lo <= min(o, c, h) and v >= 0):
            continue
        result[code] = {
            "code": code, "name": name, "market": market,
            "date": day, "open": o, "high": h, "low": lo,
            "close": c, "volume": int(v)
        }
    return result


def fetch_snapshots():
    snapshots, errors, dates = {}, [], {}
    for market, url in (("上市", TWSE), ("上櫃", TPEX)):
        try:
            rows = get_json(url)
            if not isinstance(rows, list):
                raise ValueError("資料格式不是清單")
            parsed = normalize_snapshot(rows, market)
            if len(parsed) < 200:
                raise ValueError(f"有效普通股僅 {len(parsed)} 檔")
            seen = {}
            for r in parsed.values():
                seen[r["date"]] = seen.get(r["date"], 0) + 1
            newest = max(seen, key=seen.get)
            if seen[newest] < len(parsed) * .90:
                raise ValueError("同一市場資料日期分歧")
            parsed = {k: v for k, v in parsed.items() if v["date"] == newest}
            dates[market] = newest
            snapshots.update(parsed)
        except Exception as exc:
            errors.append(f"{market}行情取得失敗：{exc}")
    return snapshots, dates, errors


def cache_read():
    try:
        with gzip.open(CACHE_PATH, "rt", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema") != 1:
            return {}
        return data.get("tickers", {})
    except (OSError, ValueError, TypeError):
        return {}


def cache_write(history):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
        json.dump({"schema": 1, "tickers": history}, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(CACHE_PATH)


def fetch_yahoo(stock):
    code, market = stock["code"], stock["market"]
    suffix = "TW" if market == "上市" else "TWO"
    now = int(time.time())
    qs = urlencode({"period1": now - 440 * 86400, "period2": now + 86400,
                    "interval": "1d", "events": "history", "includeAdjustedClose": "false"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.{suffix}?{qs}"
    j = get_json(url, attempts=1, timeout=18)
    result = ((j.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo 沒有日 K 資料")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, stamp in enumerate(timestamps):
        try:
            v = [num((quote.get(k) or [])[i]) for k in ("open", "high", "low", "close", "volume")]
            if any(z is None for z in v[:4]):
                continue
            o, h, lo, c, volume = v
            if c <= 0 or h < max(o, c, lo) or lo > min(o, c, h):
                continue
            day = datetime.fromtimestamp(stamp, TZ).date().isoformat()
            bars.append([day, round(o, 4), round(h, 4), round(lo, 4), round(c, 4), int(volume or 0)])
        except (IndexError, TypeError, ValueError):
            continue
    if len(bars) < MIN_BARS:
        raise ValueError("Yahoo K 線筆數不足")
    return bars[-260:]


def upsert_bar(bars, stock):
    today = [stock["date"], stock["open"], stock["high"], stock["low"],
             stock["close"], stock["volume"]]
    merged = {b[0]: b for b in bars if isinstance(b, list) and len(b) == 6}
    merged[today[0]] = today  # official exchange snapshot takes precedence
    return [merged[d] for d in sorted(merged)][-260:]


def avg(vals):
    return sum(vals) / len(vals)


def ema(values, length):
    alpha = 2 / (length + 1)
    out = []
    v = values[0]
    for x in values:
        v = alpha * x + (1 - alpha) * v
        out.append(v)
    return out


def evaluate(stock, bars):
    """Compute only from complete, daily bars ending on today's official trade date."""
    if len(bars) < MIN_BARS or bars[-1][0] != stock["date"]:
        return None
    c = [float(b[4]) for b in bars]
    h = [float(b[2]) for b in bars]
    lo = [float(b[3]) for b in bars]
    v = [float(b[5]) for b in bars]
    if c[-21] <= 0 or c[-61] <= 0:
        return None
    ma5, ma10, ma20, ma60 = (avg(c[-n:]) for n in (5, 10, 20, 60))
    ma20_old = avg(c[-23:-3])
    wclose = [(h[i] + lo[i] + 2*c[i])/4 for i in range(len(c))]
    d = [a-b for a, b in zip(ema(wclose, 12), ema(wclose, 26))]
    hist = [a-b for a, b in zip(d, ema(d, 9))]
    mid = ma20
    width = 4 * statistics.pstdev(c[-20:]) / mid * 100
    old_mid = avg(c[-21:-1])
    old_width = 4 * statistics.pstdev(c[-21:-1]) / old_mid * 100
    ret20 = (c[-1]/c[-21]-1)*100
    dist = (c[-1]/ma20-1)*100
    volume_lots = v[-1]/1000
    vol_ratio = v[-1]/max(avg(v[-21:-1]), 1)
    break3 = c[-1] > max(h[-4:-1])
    recovery = hist[-1] > hist[-2] >= hist[-3]
    ma20_up = ma20 > ma20_old
    prior_low = min(lo[-11:-1])
    recent_low = min(lo[-5:])
    structure_good = recent_low >= prior_low * .98
    ret5 = (c[-1]/c[-6]-1)*100

    # A = existing leader; B = confirmed short-term turn; C = early watch.
    group = None
    reasons = []
    if c[-1] > ma20 and ma20_up and hist[-1] > 0 and (break3 or c[-1] >= ma10):
        group = "A"
        reasons = ["站上上揚 MA20", "MACD 動能為正", "維持相對強勢結構"]
    elif c[-1] > ma5 and c[-1] > ma10 and recovery and structure_good and (break3 or ret5 > 2):
        group = "B"
        reasons = ["收復 MA5／MA10", "MACD 柱狀體連續改善", "低點結構改善"]
    elif c[-1] >= ma5*.99 and hist[-1] > hist[-2] and structure_good and ret5 > -3:
        group = "C"
        reasons = ["短線賣壓減弱", "MACD 柱狀體改善", "觀察是否突破"]
    if not group:
        return None

    support = min(lo[-8:-1])
    stop = max(support, ma10 if ma10 < c[-1] else 0)
    if not 0 < stop < c[-1]:
        stop = support
    stop_pct = (c[-1] - stop) / c[-1]*100 if 0 < stop < c[-1] else None
    momentum = "剛翻正" if hist[-1] > 0 and hist[-2] <= 0 else ("正值" if hist[-1] > 0 else "改善中")
    bb_expanding = width > old_width
    if bb_expanding:
        reasons.append("布林帶寬擴大")
    return {
        "code": stock["code"], "name": stock["name"], "market": stock["market"],
        "group": group, "close": round(c[-1], 2), "ret20": round(ret20, 2),
        "ret5": round(ret5, 2), "volume_lots": round(volume_lots),
        "vol_ratio": round(vol_ratio, 2), "ma20": round(ma20, 2),
        "ma60": round(ma60, 2), "dist_ma20": round(dist, 2),
        "macd": momentum, "macd_hist": round(hist[-1], 4),
        "bb_width": round(width, 2), "bb_expanding": bb_expanding,
        "breakout": break3, "stop": round(stop, 2) if stop_pct is not None else None,
        "stop_pct": round(stop_pct, 2) if stop_pct is not None else None,
        "resistance": round(max(h[-11:-1]), 2), "reasons": reasons,
        "_ret20": ret20, "_hist_rise": recovery, "_ma20_up": ma20_up
    }


def rank_candidates(items):
    if not items:
        return []
    returns = sorted(x["_ret20"] for x in items)
    for item in items:
        pct_rank = sum(r <= item["_ret20"] for r in returns) / len(returns)
        rs_score = 30 * pct_rank
        structure = (20 if item["group"] == "A" else 15 if item["group"] == "B" else 8)
        structure += 5 if item["_ma20_up"] else 0
        vol_score = min(15, max(0, (item["vol_ratio"]-0.5)*12))
        entry_score = max(0, 20 - max(0, item["dist_ma20"]-3)*2)
        risk_score = (10 if item["stop_pct"] is not None and item["stop_pct"] <= 6
                      else 5 if item["stop_pct"] is not None and item["stop_pct"] <= 8 else 0)
        item["score"] = round(min(100, rs_score + structure + vol_score + entry_score + risk_score))
        item["rs_percentile"] = round(pct_rank * 100)
        item["state"] = ("觀察" if item["group"] == "C" else
                         "等待回踩" if item["dist_ma20"] > 10 else
                         "風險過高" if item["stop_pct"] is None or item["stop_pct"] > 8 else
                         "等待確認" if not item["breakout"] else "突破觀察")
        for k in ("_ret20", "_hist_rise", "_ma20_up"):
            item.pop(k, None)
    items.sort(key=lambda x: (-x["score"], x["code"]))
    # Keep multiple groups so one market regime cannot hide all early candidates.
    result = []
    for group in "ABC":
        result.extend([x for x in items if x["group"] == group][:15])
    return sorted(result, key=lambda x: (-x["score"], x["code"]))


def run():
    now = datetime.now(TZ)
    report = {"schema": 1, "updated_at": now.isoformat(timespec="seconds"),
              "market_date": None, "market_dates": {}, "status": "unavailable",
              "universe": 0, "eligible": 0, "history_ready": 0,
              "scanned": 0, "candidates": [], "warnings": [], "errors": []}
    try:
        snapshots, dates, errors = fetch_snapshots()
        report["errors"].extend(errors)
        report["market_dates"] = dates
        if not snapshots:
            raise RuntimeError("上市與上櫃行情皆無法取得")
        trade_day = max(dates.values())
        report["market_date"] = trade_day
        report["universe"] = len(snapshots)
        if len(dates) != 2 or any(x != trade_day for x in dates.values()):
            report["warnings"].append("上市、上櫃資料日期不同；只計算最新交易日市場")
        stocks = [s for s in snapshots.values() if s["date"] == trade_day]
        # Daily volume and turnover are *eligibility* only, not entry signals.
        eligible = [s for s in stocks if s["close"] >= 10 and
                    s["volume"] >= 700_000 and s["close"]*s["volume"] >= 20_000_000]
        report["eligible"] = len(eligible)
        history = cache_read()
        # Priority: highly liquid names first, plus all prior successes.
        missing = [s for s in sorted(eligible, key=lambda s: -s["volume"]*s["close"])
                   if len(history.get(s["code"], [])) < MIN_BARS]
        todo = missing[:max(0, MAX_BOOTSTRAP)]
        if todo:
            print(f"Bootstrapping {len(todo)} / {len(missing)} eligible histories", flush=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(fetch_yahoo, s): s for s in todo}
                failures = 0
                for f in as_completed(futures):
                    stock = futures[f]
                    try:
                        history[stock["code"]] = f.result()
                    except Exception as exc:
                        failures += 1
                        if failures <= 3:
                            report["warnings"].append(f"{stock['code']} 歷史下載失敗：{exc}")
            print(f"Bootstrap failures: {failures}", flush=True)
        items = []
        for stock in eligible:
            bars = history.get(stock["code"], [])
            if len(bars) < MIN_BARS:
                continue
            bars = upsert_bar(bars, stock)
            history[stock["code"]] = bars
            if bars[-1][0] != trade_day:
                continue
            report["history_ready"] += 1
            evaluated = evaluate(stock, bars)
            if evaluated:
                items.append(evaluated)
        cache_write(history)
        report["scanned"] = report["history_ready"]
        report["candidates"] = rank_candidates(items)
        fresh = len(dates) == 2 and all(x == trade_day for x in dates.values())
        coverage = report["history_ready"]/max(1, report["eligible"])
        if coverage < .95:
            report["warnings"].append(
                f"歷史資料建置中：已完成 {report['history_ready']}／{report['eligible']} 檔（{coverage:.0%}）")
        report["status"] = "ready" if fresh and coverage >= .95 else "partial"
        print(f"Snapshot {dates}; ready {report['history_ready']}/{report['eligible']}; "
              f"candidates {len(report['candidates'])}; status {report['status']}", flush=True)
    except Exception as exc:
        report["errors"].append(str(exc))
        print(f"ERROR: {exc}", flush=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT}", flush=True)
    return report


def self_test():
    assert iso_date("1151116") == "2026-11-16"
    assert iso_date("115/11/16") == "2026-11-16"
    assert num("1,230") == 1230
    sample = {"Date": "1151116", "Code": "2330", "Name": "範例",
              "OpeningPrice": "50", "HighestPrice": "52", "LowestPrice": "49",
              "ClosingPrice": "51", "TradeVolume": "1000000"}
    assert len(normalize_snapshot([sample], "上市")) == 1
    sample["Code"] = "0050"
    assert not normalize_snapshot([sample], "上市")
    end = datetime(2026, 11, 16).date()
    closes = [20 + i*.2 for i in range(90)]
    bars = [[(end-timedelta(days=89-i)).isoformat(), x, x+1, x-1, x, 1400000]
            for i, x in enumerate(closes)]
    stock = {"code": "2330", "name": "測試", "market": "上市", "date": bars[-1][0]}
    evaluated = evaluate(stock, bars)
    assert evaluated is not None and evaluated["group"] == "A", evaluated
    assert rank_candidates([evaluated])[0]["score"] >= 0
    print("SELF TEST PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    self_test() if args.self_test else run()
