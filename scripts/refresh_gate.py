"""Decide whether a scheduled Pages refresh is still necessary.

Only a manifest actually served by the public site can prove that a build
was published. An Actions cache entry or successful build is not sufficient.
Manual runs and pushes are always allowed by the workflow.
"""
import argparse
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from update_prices import SYMBOLS as PRICE_SYMBOLS
from update_history import SYMBOLS as CHART_SYMBOLS

TZ = timezone(timedelta(hours=8))
SITE = "https://peter58501240.github.io/Pstock/data/refresh_manifest.json"
MANIFEST = Path("data/refresh_manifest.json")


def target_session(now):
    """Most recent possible closed weekday, in Taiwan time.

    This is deliberately conservative: a weekday holiday may trigger a
    redundant refresh, but is never assumed closed without exchange evidence.
    Delayed runs after midnight still target the previous closed session.
    """
    local = now.astimezone(TZ)
    day = local.date()
    if local.time() < time(13, 30):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


def assess(prices, charts, screener):
    """Return market date and reasons preventing a scheduled skip."""
    day = screener.get("market_date")
    reasons = []
    if not isinstance(day, str) or not day:
        return None, ["智慧選股無行情日期"]
    market_dates = screener.get("market_dates") or {}
    if set(market_dates) != {"上市", "上櫃"} or any(d != day for d in market_dates.values()):
        reasons.append("上市櫃行情日期不一致")
    if screener.get("errors") or screener.get("status") not in ("ready", "partial"):
        reasons.append("智慧選股執行異常")
    if not screener.get("calendar_sources"):
        reasons.append("交易日曆未驗證")
    if screener.get("history_gaps") or screener.get("history_unavailable"):
        reasons.append("選股歷史 K 線缺漏")
    eligible = screener.get("eligible", 0)
    ready = screener.get("history_ready", 0)
    short = screener.get("insufficient_history") or []
    # Intentionally excluded short histories (e.g. newly listed stocks)
    # need not force a duplicate full-market download every afternoon.
    if not isinstance(eligible, int) or not isinstance(ready, int) or eligible <= 0 or ready <= 0 or ready + len(short) != eligible:
        reasons.append("初篩股票未完整分類")
    if any(not isinstance(x.get("available"), int) or x["available"] < 1
           or not isinstance(x.get("required"), int) or x["available"] >= x["required"]
           for x in short):
        reasons.append("短歷史分類異常")
    refresh = screener.get("history_refresh") or {}
    if refresh.get("attempted") != eligible or refresh.get("failed") != 0:
        reasons.append("尚有股票待補抓或歷史下載失敗")
    quotes = prices.get("prices") or {}
    if (prices.get("market_date") != day or prices.get("errors") or prices.get("missing")
            or prices.get("stale_codes") or not set(PRICE_SYMBOLS).issubset(quotes)
            or any(quotes[c].get("market_date") != day for c in PRICE_SYMBOLS if c in quotes)):
        reasons.append("庫存／已實現股價不完整或過期")
    tickers = charts.get("tickers") or {}
    if (charts.get("errors") or not set(CHART_SYMBOLS).issubset(tickers)
            or any(tickers[c].get("last_date") != day for c in CHART_SYMBOLS if c in tickers)):
        reasons.append("個股圖表歷史 K 線不完整或過期")
    return day, reasons


def manifest_for(prices, charts, screener, sha):
    day, reasons = assess(prices, charts, screener)
    return {"schema": 1, "commit": sha, "market_date": day,
            "complete": not reasons, "reasons": reasons,
            "generated_at": datetime.now(TZ).isoformat(timespec="seconds")}


def may_skip(manifest, sha, expected_day):
    return (isinstance(manifest, dict) and manifest.get("schema") == 1
            and bool(sha) and manifest.get("commit") == sha
            and manifest.get("market_date") == expected_day
            and manifest.get("complete") is True and manifest.get("reasons") == [])


def check_live(sha, now):
    expected = target_session(now)
    try:
        url = SITE + "?" + urlencode({"v": now.timestamp()})
        req = Request(url, headers={"Cache-Control": "no-cache",
                                    "User-Agent": "Pstock-refresh-gate/1"})
        with urlopen(req, timeout=20) as response:
            manifest = json.load(response)
        if may_skip(manifest, sha, expected):
            print(f"已發布 {expected} 完整行情及同版本網站；略過重複排程。")
            return False
        print(f"需要更新：目標 {expected}，已發布日期 {manifest.get('market_date') if isinstance(manifest, dict) else None}，"
              f"品質檢查 {manifest.get('reasons') if isinstance(manifest, dict) else None}")
    except Exception as exc:
        print(f"無法驗證已發布網站（{type(exc).__name__}），保守執行更新。")
    return True


def write_manifest(sha):
    try:
        def read(name):
            return json.loads(Path("data", name).read_text(encoding="utf-8"))
        manifest = manifest_for(read("prices.json"), read("history.json"), read("screener.json"), sha)
    except Exception as exc:
        manifest = {"schema": 1, "commit": sha, "market_date": None,
                    "complete": False, "reasons": [f"無法驗證輸出：{type(exc).__name__}"]}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


def self_test():
    day = "2026-11-27"
    prices = {"market_date": day, "prices": {c: {"market_date": day} for c in PRICE_SYMBOLS},
              "missing": [], "errors": [], "stale_codes": []}
    charts = {"tickers": {c: {"last_date": day} for c in CHART_SYMBOLS}, "errors": []}
    scan = {"market_date": day, "market_dates": {"上市": day, "上櫃": day},
            "status": "partial", "calendar_sources": ["2330", "0050"],
            "eligible": 2, "history_ready": 1, "insufficient_history": [
                {"available": 55, "required": 70}],
            "history_refresh": {"attempted": 2, "failed": 0}}
    good = manifest_for(prices, charts, scan, "commit")
    assert may_skip(good, "commit", day), good
    assert not may_skip(good, "other", day)
    assert not may_skip(good, "commit", "2026-11-26")
    assert not may_skip(None, "commit", day)
    for bad in (
        {"history_gaps": [{"code": "2330"}]},
        {"history_unavailable": ["2330"]},
        {"history_refresh": {"attempted": 1, "failed": 0}},
        {"history_refresh": {"attempted": 2, "failed": 1}},
        {"market_dates": {"上市": day, "上櫃": "2026-11-26"}},
        {"errors": ["test"]}, {"status": "unavailable"},
        {"history_ready": 0},
    ):
        assert not may_skip(manifest_for(prices, charts, {**scan, **bad}, "commit"), "commit", day), bad
    assert not manifest_for({**prices, "missing": ["0050"]}, charts, scan, "commit")["complete"]
    assert not manifest_for(prices, {**charts, "errors": ["test"]}, scan, "commit")["complete"]
    assert target_session(datetime(2026, 11, 27, 15, 17, tzinfo=TZ)) == day
    assert target_session(datetime(2026, 11, 28, 2, 0, tzinfo=TZ)) == day
    assert target_session(datetime(2026, 11, 30, 9, 0, tzinfo=TZ)) == day
    assert target_session(datetime(2026, 11, 30, 17, 23, tzinfo=TZ)) == "2026-11-30"
    print("REFRESH GATE SELF TEST PASS")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("check", "write", "self-test"))
    args = p.parse_args()
    sha = os.getenv("GITHUB_SHA", "")
    if args.mode == "self-test":
        self_test()
    elif args.mode == "write":
        write_manifest(sha)
    else:
        refresh = check_live(sha, datetime.now(TZ))
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write("refresh=" + str(refresh).lower() + "\n")
