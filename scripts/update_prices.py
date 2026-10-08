#!/usr/bin/env python3
import json
import re
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

SYMBOLS = {
    "00935", "2030", "3481", "3491", "4919",
    "5347", "5471", "6147", "6442", "6706"
}
UA = "Mozilla/5.0 (compatible; Pstock/1.0; +https://github.com/peter58501240/Pstock)"
TZ = timezone(timedelta(hours=8))

def get_text(url, timeout=30):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/html,*/*",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    })
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset)
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")

def roc_date_to_iso(s):
    s = re.sub(r"\D", "", str(s))
    if len(s) < 7:
        return None
    y = int(s[:-4]) + 1911
    m = int(s[-4:-2])
    d = int(s[-2:])
    return f"{y:04d}-{m:02d}-{d:02d}"

def clean_price(s):
    s = str(s).replace(",", "").strip()
    if not s or s in {"--", "---", "----", "N/A"}:
        return None
    s = s.replace("+", "")
    try:
        return float(s)
    except ValueError:
        return None

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.in_cell = False
        self.cell_parts = []
        self.row = []

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.in_cell = True
            self.cell_parts = []
        elif tag == "tr":
            self.row = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            txt = " ".join("".join(self.cell_parts).split())
            self.row.append(txt)
            self.in_cell = False
        elif tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.row = []

def fetch_twse():
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL"
    text = get_text(url)
    items = json.loads(text)
    out = {}
    for item in items:
        code = str(item.get("Code", "")).strip()
        if code not in SYMBOLS:
            continue
        price = clean_price(item.get("ClosingPrice"))
        if price is None:
            continue
        market_date = roc_date_to_iso(item.get("Date"))
        out[code] = {
            "close": price,
            "name": str(item.get("Name", "")).strip(),
            "source": "TWSE",
            "market_date": market_date,
        }
    return out

def fetch_tpex():
    url = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&o=htm"
    html = get_text(url)

    m = re.search(r"資料日期\s*[:：]?\s*(\d{2,3})/(\d{1,2})/(\d{1,2})", html)
    market_date = None
    if m:
        y, mo, d = map(int, m.groups())
        market_date = f"{y + 1911:04d}-{mo:02d}-{d:02d}"

    parser = TableParser()
    parser.feed(html)
    out = {}
    for row in parser.rows:
        if len(row) < 3:
            continue
        code = row[0].strip()
        if code not in SYMBOLS:
            continue
        price = clean_price(row[2])
        if price is None:
            continue
        out[code] = {
            "close": price,
            "name": row[1].strip() if len(row) > 1 else "",
            "source": "TPEx",
            "market_date": market_date,
        }
    return out

def main():
    merged = {}
    errors = []

    try:
        merged.update(fetch_twse())
    except Exception as e:
        errors.append(f"TWSE: {type(e).__name__}: {e}")

    try:
        merged.update(fetch_tpex())
    except Exception as e:
        errors.append(f"TPEx: {type(e).__name__}: {e}")

    now = datetime.now(TZ)
    found_dates = sorted({
        v.get("market_date") for v in merged.values() if v.get("market_date")
    })
    missing = sorted(SYMBOLS - set(merged))

    payload = {
        "updated_at": now.isoformat(timespec="seconds"),
        "market_date": found_dates[-1] if found_dates else None,
        "prices": merged,
        "missing": missing,
        "errors": errors,
    }

    out_path = Path("data/prices.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))

    # Do not fail deployment if one source is temporarily unavailable.
    # The website keeps a last-known fallback price for any missing symbol.
    if not merged:
        print("WARNING: no prices fetched; website will use fallback prices.")

if __name__ == "__main__":
    main()
