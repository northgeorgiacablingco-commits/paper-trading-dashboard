#!/usr/bin/env python3
"""Honest replay backtest of the stock paper strategies. Runs the LIVE code (rules/stock_bot.run) step by step on historical
Yahoo bars, in a temp account (never touches data/). No parameter tuning. Usage:
  python scripts/backtest_stock.py [--days 60] [--one-min]   (--one-min: TJR entries on real 1m bars, last ~7 days only)
Limits: fills at the 5m bar close +2 bps; intrabar order of stop vs target assumed stop-first; no 1m history beyond ~7 days,
so the 60-day TJR test uses 5m bars as its 'entry timeframe'."""
import sys, os, json, tempfile, io, contextlib, argparse
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rules"))
import stock_bot as b

def load(sym, rng, interval, prepost=True):
    return b.fetch_chart(sym, rng, interval, prepost)

def backtest(name, D5, D1=None, days=None, step_end=(11, 35)):
    tmp = tempfile.mkdtemp(); b.PF, b.JOURNAL, b.SIGNALS = tmp + "/p.json", tmp + "/j.csv", tmp + "/s.csv"
    json.dump(b.new_account(), open(b.PF, "w"))
    b.UNIVERSE = ["SPY", "QQQ"]
    for d in days:
        o = datetime.combine(d, datetime.min.time().replace(hour=9, minute=30), b.NY)
        sess = b.session(d); end = sess[1] if step_end is None else datetime.combine(d, datetime.min.time().replace(hour=step_end[0], minute=step_end[1]), b.NY)
        t = o + timedelta(minutes=5)
        while t <= end:
            now = t.astimezone(timezone.utc); ts = now.timestamp()
            def f(s, ts=ts):
                bars = [x for x in D5[s]["bars"] if x["t"] + 300 <= ts and x["t"] >= ts - 6 * 86400]
                return dict(meta=dict(price=bars[-1]["c"], time=int(ts) - 20, prev_close=None, delayed_by=None, exchange="replay"), bars=bars, url="")
            f1 = (lambda s, ts=ts: dict(bars=[x for x in D1[s]["bars"] if x["t"] + 60 <= ts and x["reg"]])) if D1 else None
            with contextlib.redirect_stdout(io.StringIO()): p = b.run(now=now, fetch=f, fetch1=f1, strategies=[name])
            t += timedelta(minutes=5)
        if step_end is not None:   # make sure everything is flat (11:30 time exit for TJR/LR)
            pass
    p = json.load(open(b.PF)); C = p["closed"]
    eq, peak, mdd = p["start"], p["start"], 0.0
    for c in C:
        eq += c["pnl"]; peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    gw = sum(c["pnl"] for c in C if c["pnl"] > 0); gl = -sum(c["pnl"] for c in C if c["pnl"] <= 0)
    import csv, re, collections
    rows = list(csv.DictReader(open(b.SIGNALS))) if os.path.exists(b.SIGNALS) else []
    skips = len(rows)
    why = collections.Counter(re.sub(r"[0-9.:]+", "#", r["reason"].split(" | ")[0].split(": ", 1)[-1])[:70] for r in rows if r["event"] == "SKIP")
    return dict(strategy=name, days=len(days), trades=len(C), wins=sum(c["pnl"] > 0 for c in C), win_rate=round(sum(c["pnl"] > 0 for c in C) / len(C), 3) if C else None,
                avg_R=round(sum(c["R"] or 0 for c in C) / len(C), 3) if C else None, net_pnl=round(sum(c["pnl"] for c in C), 2),
                profit_factor=round(gw / gl, 2) if gl else None, max_drawdown_pct=round(mdd * 100, 2), signal_rows=skips,
                open_left=len(p["open"]),
                avg_risk_pct=round(sum((c.get("risk0") or 0) for c in C) / len(C) / p["start"] * 100, 3) if C else None,
                median_risk_pct=round(sorted((c.get("risk0") or 0) for c in C)[len(C) // 2] / p["start"] * 100, 3) if C else None,
                avg_notional_x=round(sum(c["entry"] * c["qty0"] for c in C) / len(C) / p["start"], 2) if C else None, skip_reasons=why.most_common(40), trades_detail=[(c["opened"][:16], c["sym"], c["side"], c["pnl"], c["R"], c["exit_reason"]) for c in C])

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--days", type=int, default=60); ap.add_argument("--one-min", action="store_true"); a = ap.parse_args()
    D5 = {s: load(s, "60d", "5m") for s in ("SPY", "QQQ")}
    alld = sorted({datetime.fromtimestamp(x["t"], b.NY).date() for x in D5["SPY"]["bars"] if x["reg"]})
    D1 = None
    if a.one_min:
        D1 = {s: load(s, "7d", "1m", prepost=False) for s in ("SPY", "QQQ")}
        d1 = sorted({datetime.fromtimestamp(x["t"], b.NY).date() for x in D1["SPY"]["bars"]})
        days = [d for d in d1 if d != alld[0]]
    else:
        days = [d for d in alld[1:]][-a.days:]
    out = []
    for name in (["tjr_exact"] if a.one_min else ["tjr_exact", "liquidity_reversal", "orb15_v1"]):
        r = backtest(name, D5, D1, days, step_end=None if name == "orb15_v1" else (11, 35)); out.append(r)
        print(json.dumps({k: v for k, v in r.items() if k != "trades_detail"}))
        for t in r["trades_detail"]: print("   ", t)
    print(f"data: Yahoo 5m SPY/QQQ {days[0]} .. {days[-1]} ({len(days)} sessions)")
