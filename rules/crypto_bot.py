#!/usr/bin/env python3
"""PAPER crypto trend-following bot. Simulated only: no real money, wallets or exchange accounts.
Data: Coinbase Exchange public 1h candles + ticker (Binance.com returns HTTP 451 from US IPs).
Rules: price above EMA20 and EMA50 (1h); trigger = fresh 1h close above the prior 24h high (BREAKOUT)
or an EMA20 pullback bounce (PULLBACK). Stop = entry - 1.5 x ATR(14). Target = 2R. Risk 1% of equity,
no leverage, max 3 open, max 5 entries/day (UTC), pause after 2 losses/day. Simulated fee 0.10%/side.
Exits are checked against real 1h candle highs/lows after entry (stop checked first = conservative)."""
import json, os, csv, urllib.request
from datetime import datetime, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PF = os.path.join(ROOT, "data", "crypto_portfolio.json")
JR = os.path.join(ROOT, "data", "crypto_journal.csv")
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
START, RISK, MAX_OPEN, MAX_DAY, MAX_LOSS, FEE = 10000.0, 0.01, 3, 5, 2, 0.001
COLS = ["date","ticker","setup","direction","thesis","entry_time","entry_price","stop","target","shares","risk_$","exit_time","exit_price","exit_reason","pnl_$","R_multiple","followed_rules(Y/N)","grade","lessons"]
def get(u): return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "paper-bot"}), timeout=20).read())
def now(): return datetime.now(timezone.utc)
def iso(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def candles(sym):
    rows = get(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles?granularity=3600")  # [t, low, high, open, close, vol] newest first
    rows.sort(key=lambda r: r[0])
    cutoff = int(now().timestamp()) // 3600 * 3600
    return [dict(t=r[0], l=r[1], h=r[2], o=r[3], c=r[4], v=r[5]) for r in rows if r[0] < cutoff]  # closed candles only
def price(sym): return float(get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker")["price"])
def ema(vals, n):
    k = 2 / (n + 1); e = sum(vals[:n]) / n
    for v in vals[n:]: e = v * k + e * (1 - k)
    return e
def atr(cs, n=14):
    tr = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(cs, cs[1:])]
    return sum(tr[-n:]) / n
def load():
    if os.path.exists(PF): return json.load(open(PF))
    return dict(account="crypto-paper", starting_balance=START, cash=START, open=[], closed=[], equity_curve=[], day={}, runs=[])
def r2(x, d=2): return round(x, d) if abs(x) >= 1 else round(x, 6)
def main():
    p = load(); t = now(); today = t.strftime("%Y-%m-%d"); notes = []
    if p["day"].get("date") != today: p["day"] = dict(date=today, entries=0, losses=0)
    cache = {}
    def cs(sym):
        if sym not in cache: cache[sym] = candles(sym)
        return cache[sym]
    # 1) manage open positions with real candles since entry
    still = []
    for pos in p["open"]:
        try:
            after = [c for c in cs(pos["coin"]) if c["t"] >= pos["entry_candle"]]
            hit = None
            for c in after:
                if c["l"] <= pos["stop"]: hit = ("stop", pos["stop"], c["t"] + 3600); break
                if c["h"] >= pos["target"]: hit = ("target", pos["target"], c["t"] + 3600); break
            if not hit:
                lp = price(pos["coin"]); pos["last_price"] = lp
                if lp <= pos["stop"]: hit = ("stop", lp, int(t.timestamp()))
                elif lp >= pos["target"]: hit = ("target", lp, int(t.timestamp()))
            if hit:
                why, px, ts = hit
                gross = (px - pos["entry"]) * pos["qty"]; fee = px * pos["qty"] * FEE
                pnl = gross - fee - pos["entry_fee"]
                p["cash"] += px * pos["qty"] - fee
                pos.update(exit=r2(px), exit_time=iso(ts), exit_reason=why, pnl=round(pnl, 2), R=round(pnl / pos["risk"], 2))
                p["closed"].append(pos)
                if pnl < 0 and pos["entry_time"][:10] == today: p["day"]["losses"] += 1
                elif pnl < 0: p["day"]["losses"] += 1
                notes.append(f"CLOSED {pos['coin']} at {r2(px)} ({why}), P&L ${pnl:.2f} ({pos['R']}R)")
            else: still.append(pos)
        except Exception as e:
            still.append(pos); notes.append(f"{pos['coin']}: could not update ({e})")
    p["open"] = still
    # 2) equity at live prices
    def equity():
        eq = p["cash"]
        for pos in p["open"]:
            try: lp = pos.get("last_price") or price(pos["coin"])
            except Exception: lp = pos["entry"]
            eq += lp * pos["qty"]
        return eq
    # 3) new signals
    for sym in COINS:
        if any(o["coin"] == sym for o in p["open"]): notes.append(f"{sym}: already open"); continue
        if len(p["open"]) >= MAX_OPEN: notes.append(f"{sym}: skipped, max {MAX_OPEN} open"); continue
        if p["day"]["entries"] >= MAX_DAY: notes.append(f"{sym}: skipped, {MAX_DAY} entries today"); continue
        if p["day"]["losses"] >= MAX_LOSS: notes.append(f"{sym}: paused, {MAX_LOSS} losses today"); continue
        try:
            c = cs(sym)
            if len(c) < 80: notes.append(f"{sym}: not enough candles"); continue
            closes = [x["c"] for x in c]; last, prev = c[-1], c[-2]
            e20, e50 = ema(closes, 20), ema(closes, 50); e20p = ema(closes[:-1], 20)
            hi24 = max(x["h"] for x in c[-25:-1]); a = atr(c)
            trend = last["c"] > e20 and last["c"] > e50 and e20 > e50
            brk = last["c"] > hi24 and prev["c"] <= max(x["h"] for x in c[-26:-2])
            pb = prev["l"] <= e20p * 1.002 and last["c"] > e20 and last["c"] > prev["c"] and last["c"] > last["o"]
            ctx = f"close {r2(last['c'])}, EMA20 {r2(e20)}, EMA50 {r2(e50)}, 24h high {r2(hi24)}, ATR {r2(a)}"
            if not trend: notes.append(f"{sym}: no signal, trend filter fails ({ctx})"); continue
            if not (brk or pb): notes.append(f"{sym}: no signal, uptrend but no fresh breakout or pullback bounce ({ctx})"); continue
            setup = "BREAKOUT" if brk else "PULLBACK"
            entry = price(sym); stop = entry - 1.5 * a
            if stop <= 0 or entry <= stop: notes.append(f"{sym}: invalid stop"); continue
            target = entry + 2 * (entry - stop); eq = equity(); risk = eq * RISK
            qty = risk / (entry - stop); qty = min(qty, (p["cash"] * 0.999) / (entry * (1 + FEE)))
            if qty * entry < 10: notes.append(f"{sym}: signal but size < $10 (cash limited)"); continue
            fee = entry * qty * FEE; p["cash"] -= entry * qty + fee
            thesis = (f"{setup}: 1h close {r2(last['c'])} above EMA20 {r2(e20)} and EMA50 {r2(e50)} (uptrend)" +
                      (f", fresh close above prior 24h high {r2(hi24)}." if brk else f", pulled back to EMA20 and bounced with a green candle.") +
                      f" Stop 1.5xATR({r2(a)}) below entry, target 2R. Invalid if it closes back below EMA20.")
            pos = dict(coin=sym, setup=setup, entry=r2(entry), entry_time=iso(int(t.timestamp())), entry_candle=last["t"] + 3600,
                       stop=r2(stop), target=r2(target), qty=qty, risk=round((entry - stop) * qty, 2), entry_fee=fee, thesis=thesis, last_price=entry)
            p["open"].append(pos); p["day"]["entries"] += 1
            notes.append(f"OPENED {sym} {setup} at {r2(entry)}, stop {r2(stop)}, target {r2(target)}, qty {qty:.6f}, risk ${pos['risk']:.2f}")
        except Exception as e:
            notes.append(f"{sym}: data error ({e})")
    eq = equity()
    p["equity"] = round(eq, 2); p["updated"] = iso(int(t.timestamp()))
    p["equity_curve"].append(dict(t=p["updated"], equity=p["equity"])); p["equity_curve"] = p["equity_curve"][-2000:]
    p["runs"] = ([dict(t=p["updated"], notes=notes)] + p["runs"])[:50]
    os.makedirs(os.path.dirname(PF), exist_ok=True)
    json.dump(p, open(PF, "w"), indent=1)
    with open(JR, "w", newline="") as f:
        w = csv.writer(f); w.writerow(COLS)
        for x in p["closed"] + p["open"]:
            w.writerow([x["entry_time"][:10], x["coin"], x["setup"], "long", x["thesis"], x["entry_time"], x["entry"], x["stop"], x["target"], round(x["qty"], 6), x["risk"],
                        x.get("exit_time", ""), x.get("exit", ""), x.get("exit_reason", "OPEN"), x.get("pnl", ""), x.get("R", ""), "Y", "", ""])
    print("\n".join(notes)); print(f"Equity ${eq:,.2f}  cash ${p['cash']:,.2f}  open {len(p['open'])}")
if __name__ == "__main__": main()
