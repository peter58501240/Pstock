#!/usr/bin/env python3
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SYMBOLS = {
    "00935": "野村臺灣新科技50",
    "2030": "彰源",
    "3481": "群創",
    "3491": "昇達科",
    "4919": "新唐",
    "5347": "世界",
    "5471": "松翰",
    "6147": "頎邦",
    "6442": "光聖",
    "6706": "惠特",
}
UA = "Mozilla/5.0 (compatible; Pstock/1.0; +https://github.com/peter58501240/Pstock)"
TZ = timezone(timedelta(hours=8))

def get_json(url, timeout=30):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    })
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_yahoo(symbol, suffix):
    now = int(time.time())
    start = now - 800 * 86400
    q = urlencode({
        "period1": start,
        "period2": now + 86400,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "false",
    })
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.{suffix}?{q}"
    data = get_json(url)
    result = (data.get("chart") or {}).get("result")
    if not result:
        err = (data.get("chart") or {}).get("error")
        raise RuntimeError(f"Yahoo no result: {err}")
    r = result[0]
    ts = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    rows = []
    for i, t in enumerate(ts):
        vals = []
        for arr in (opens, highs, lows, closes, volumes):
            vals.append(arr[i] if i < len(arr) else None)
        o, h, l, c, v = vals
        if None in (o, h, l, c):
            continue
        day = datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")
        rows.append({
            "date": day,
            "open": round(float(o), 4),
            "high": round(float(h), 4),
            "low": round(float(l), 4),
            "close": round(float(c), 4),
            "volume": int(v or 0),
        })
    if len(rows) < 120:
        raise RuntimeError(f"Only {len(rows)} rows")
    return rows

def main():
    tickers = {}
    errors = []
    for code, name in SYMBOLS.items():
        rows = None
        market = None
        last_error = None
        for suffix, label in (("TW", "上市"), ("TWO", "上櫃")):
            try:
                rows = fetch_yahoo(code, suffix)
                market = label
                break
            except Exception as e:
                last_error = f"{suffix}: {type(e).__name__}: {e}"
        if rows:
            tickers[code] = {
                "name": name,
                "market": market,
                "bars": rows,
                "last_date": rows[-1]["date"],
            }
            print(f"{code} {name}: {len(rows)} bars ({market})")
        else:
            errors.append(f"{code}: {last_error}")
            print(f"WARNING {code}: {last_error}")

    payload = {
        "updated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "source": "Yahoo Finance daily OHLCV",
        "tickers": tickers,
        "errors": errors,
    }
    out = Path("data/history.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {out} with {len(tickers)} tickers")

if __name__ == "__main__":
    main()
