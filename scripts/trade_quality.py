"""Post-close trading-quality diagnostics for Pstock.

All calculations use the current completed session and older bars ONLY.
The 'entry reference' is the close, NOT an executable next-day fill.
No stop, target or R/R value is guaranteed by the market.
"""
import math


def avg(seq):
    return sum(seq) / len(seq)


def atr14(bars):
    if len(bars) < 16:
        return None
    tr = []
    for i in range(len(bars)-14, len(bars)):
        hi, lo, prev = float(bars[i][2]), float(bars[i][3]), float(bars[i-1][4])
        tr.append(max(hi-lo, abs(hi-prev), abs(lo-prev)))
    return avg(tr)


def confirmed_pivot_lows(bars, lookback=20):
    """Price pivots need two completed newer bars for confirmation."""
    lows = [float(b[3]) for b in bars]
    result = []
    # exclude last two candles: a pivot must be confirmed, not projected.
    for i in range(max(2, len(bars)-lookback), len(bars)-2):
        if (lows[i] <= min(lows[i-2:i]) and
                lows[i] < min(lows[i+1:i+3])):
            result.append((i, lows[i]))
    return result


def overhead_resistance(bars, price, atr):
    """Nearest confirmed old pivot HIGH above current price; no invented target."""
    highs = [float(b[2]) for b in bars]
    pivots = []
    # Must leave at least 2 completed bars to confirm each high.
    for i in range(max(2, len(bars)-90), len(bars)-2):
        if highs[i] >= max(highs[i-2:i]) and highs[i] > max(highs[i+1:i+3]):
            if highs[i] > price + 0.5 * atr:
                pivots.append(highs[i])
    return min(pivots) if pivots else None


def assess_quality(bars, group, ma10, ma20, vol_ratio, break3):
    """Return structural stop, breakout/retest stage, overhead risk and trading status.

    Red flags take priority over technical strength. An 'interesting' stock is
    not necessarily an executable trade. Risk rules are exploratory defaults.
    """
    n = len(bars)
    if n < 70:
        return {"quality_state": "資料不足", "quality_status": "avoid"}
    last = bars[-1]
    c, o, low = float(last[4]), float(last[1]), float(last[3])
    prev_c = float(bars[-2][4])
    ma10, ma20 = float(ma10), float(ma20)
    a = atr14(bars)
    if not a or not math.isfinite(a) or a <= 0 or c <= 0 or ma20 <= 0 or ma10 <= 0:
        return {"quality_state": "無有效波動資料", "quality_status": "avoid"}
    pivots = [x for _, x in confirmed_pivot_lows(bars) if 0 < x < c]
    if pivots:
        # Nearest confirmed pivot LOW below the market, rather than a line
        # mechanically attached to MA10, which can be only 0.23% away.
        support = max(pivots)
        source = "近期確認轉折低點"
    else:
        support = min(float(b[3]) for b in bars[-11:-1])
        source = "前 10 日區間低點（待確認）"
    stop_raw = min(support - 0.25 * a, c - 1.2 * a) if 0 < support < c else None
    stop_pct = ((c-stop_raw)/c*100 if stop_raw and 0 < stop_raw < c else None)
    risk = c-stop_raw if stop_pct is not None else None

    # Breakout needs a completed close beyond 10-day prior highs and volume.
    prior_high10 = max(float(b[2]) for b in bars[-11:-1])
    breakout = c > prior_high10 and c > prev_c and vol_ratio >= 1.1
    # Retest must have tagged the moving-average area and closed back above it.
    support_retest = (low <= ma10 + 0.35*a and c >= ma10 and c > o and c >= prev_c
                      and vol_ratio <= 1.4 and c >= ma20*0.98)
    # Earlier turnaround can qualify without MA20 already pointing upward.
    early_turn = (group == "B" and break3 and c > o and vol_ratio >= 0.9)
    if breakout:
        stage = "突破確認"
    elif support_retest:
        stage = "量縮回踩止跌" if vol_ratio < 1 else "回踩轉強"
    elif early_turn:
        stage = "止跌轉強"
    else:
        stage = "尚未形成可執行型態"

    overhead = overhead_resistance(bars, c, a)
    rr = ((overhead-c)/risk if overhead is not None and risk and risk > 0 else None)
    distance_ma20 = (c/ma20-1)*100
    distance_ma10 = (c/ma10-1)*100
    is_extended = distance_ma20 > 8 or distance_ma10 > 7
    risk_oversized = stop_pct is None or stop_pct > 8
    rr_low = rr is not None and rr < 2
    setup_present = stage != "尚未形成可執行型態"

    if group == "C":
        status, label = "wait", "築底觀察，尚未確認"
    elif is_extended:
        status, label = "avoid", "乖離過大，不追價"
    elif risk_oversized:
        status, label = "avoid", "結構停損超過 8%"
    elif rr_low:
        status, label = "avoid", "上方壓力近，盈虧比不足 2"
    elif not setup_present:
        status, label = "wait", "等待突破／回踩確認"
    else:
        status, label = "watch", "隔日關注，非直接買進"

    flags = []
    if is_extended:
        flags.append("距均線過遠")
    if risk_oversized:
        flags.append("失敗線太遠或不可用")
    if rr_low:
        flags.append("已知壓力使盈虧比不足 2")
    if rr is None:
        flags.append("上方缺少可信的確認壓力，無法估算盈虧比")
    if source.endswith("（待確認）"):
        flags.append("支撐尚未構成確認轉折")
    if group == "C":
        flags.append("尚屬觀察階段")

    return {
        "atr14": round(a, 3),
        "structure_support": round(support, 2) if support > 0 else None,
        "stop_source": source,
        "stop": round(stop_raw, 2) if stop_pct is not None else None,
        "stop_pct": round(stop_pct, 2) if stop_pct is not None else None,
        "stop_reference": "當日收盤價，非隔日成交價",
        "reference_entry_close": round(c, 2),
        "entry_type": stage,
        "entry_confirmed_at_close": setup_present,
        "entry_quality": status,
        "quality_status": status,
        "quality_state": label,
        "resistance": round(overhead, 2) if overhead is not None else None,
        "rr": round(rr, 2) if rr is not None else None,
        "risk_notes": flags[:4],
        "dist_ma10": round(distance_ma10, 2),
        "dist_ma20": round(distance_ma20, 2),
        "atr_pct": round(a/c*100, 2),
    }


def quality_score(item, percentile):
    """100-point order-of-review score, not a win-probability or entry order."""
    group = item.get("group")
    relative = 25 * min(1.0, max(0.0, percentile))
    structure = {"A": 25, "B": 20, "C": 12, "O": 15}.get(group, 10)
    volume = min(10, max(0, (float(item.get("vol_ratio", 0))-0.7)*12))
    setup = {"突破確認": 19, "量縮回踩止跌": 20,
             "回踩轉強": 17, "止跌轉強": 15}.get(item.get("entry_type"), 5)
    setup = max(0, setup - max(0, item.get("dist_ma20", 0)-3)*2)
    stop_pct = item.get("stop_pct")
    rr = item.get("rr")
    if stop_pct is None or stop_pct > 8 or stop_pct < 1:
        risk = 0
    elif rr is not None and rr < 2:
        risk = 0
    elif 2 <= stop_pct <= 5:
        risk = 20
    elif stop_pct <= 8:
        risk = 12
    else:
        risk = 6
    if rr is None:
        risk = min(risk, 12)  # no known target: never grant full risk points

    score = min(100, round(relative+structure+volume+setup+risk))
    status = item.get("quality_status")
    if status == "avoid":
        score = min(score, 55)
    elif status == "wait":
        score = min(score, 70)
    return score


def run_self_test():
    from datetime import date, timedelta
    start = date(2026, 1, 1)
    close = [100 + (i*.18) for i in range(85)]
    bars = [[(start+timedelta(days=i)).isoformat(), p-.1, p+.9, p-.8, p, 1000000]
            for i, p in enumerate(close)]
    p = assess_quality(bars, "A", avg(close[-10:]), avg(close[-20:]), 1.2, True)
    assert p["stop"] is not None and p["stop"] < bars[-1][4]
    assert p["stop_pct"] > .23, p
    assert p["stop_source"], p
    assert quality_score({**p, "group": "A", "vol_ratio": 1.2}, .9) <= 100
    assert quality_score({**p, "group": "A", "vol_ratio": 1.2,
                          "quality_status": "avoid"}, .9) <= 55
    # A nearby confirmed resistance must NOT be treated as 2R potential.
    assert quality_score({**p, "group": "A", "vol_ratio": 1.2,
                          "stop_pct": 2, "rr": 0.5}, .9) < 90
    # Market/target unknown is explicitly absent; never fabricate R/R.
    flat = [[(start+timedelta(days=i)).isoformat(), 50.0, 50.8, 49.0, 50.1, 500000]
            for i in range(85)]
    q = assess_quality(flat, "C", 50.1, 50.1, 0.8, False)
    assert q["quality_status"] == "wait"
    return True


if __name__ == "__main__":
    assert run_self_test()
    print("TRADE QUALITY SELF TEST PASS")
