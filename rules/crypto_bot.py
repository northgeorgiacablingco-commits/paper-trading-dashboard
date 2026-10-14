#!/usr/bin/env python3
"""PAPER crypto trend-following bot over the liquid USD spot market. Simulated only: no real money, wallets or exchanges.
Universe (each run): CoinGecko top 250 by market cap ∩ Coinbase Exchange online USD pairs.
Filters: 24h volume >= $5M, >= 30 days of daily candles on Coinbase, no stablecoins / wrapped / staked / pegged tokens.
Ranking: momentum score = 24h% + 0.5 x 7d% + 10 x log2(volume surge), surge = 24h $vol / 30d avg daily $vol.
Rules on the top candidates (1h, closed candles only): close above EMA20 and EMA50 with EMA20 > EMA50; trigger = fresh
close above the prior 24h high (BREAKOUT) or an EMA20 pullback bounce (PULLBACK). Stop = entry - 1.5 x ATR(14), target 2R.
Risk 1% of equity, no leverage, max 3 open, max 5 entries/UTC day, pause after 2 losses/day.
Costs: fee+slippage 0.1%/side for top-20 market cap, 0.2%/side for smaller caps. Exits use real 1h highs/lows after entry."""
import json, os, csv, math, time, urllib.request
from datetime import datetime, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PF = os.path.join(ROOT, "data", "crypto_portfolio.json")
JR = os.path.join(ROOT, "data", "crypto_journal.csv")
CORE = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK"]
START, MAX_OPEN, MAX_DAY, MAX_LOSS = 10000.0, 3, 5, 2
MIN_VOL, MEME_VOL, MIN_DAYS, TOP_N = 5e6, 2e6, 30, 80
# Sleeves (paper): risk is % of TOTAL account equity, capped by the sleeve's own cash.
SLEEVES = {"core": dict(alloc=7000.0, risk=0.01), "alts": dict(alloc=2000.0, risk=0.0075), "meme": dict(alloc=1000.0, risk=0.005, max_open=2, cost=0.005)}
STABLE = {"USDT","USDC","DAI","FDUSD","TUSD","USDE","USDS","PYUSD","USDD","FRAX","GUSD","USDP","LUSD","EURC","EURT","USD1","RLUSD","USDG","USDTB","BUSD","SUSDE","XAUT","PAXG","USTC"}
BADWORDS = ("wrapped", "staked", "bridged", "liquid staking", " usd", "usd ", "pegged", "restaked", "tokenized")
COLS = ["date","ticker","setup","direction","thesis","entry_time","entry_price","stop","target","shares","risk_$","exit_time","exit_price","exit_reason","pnl_$","R_multiple","followed_rules(Y/N)","grade","lessons"]
CB = "https://api.exchange.coinbase.com"
_last = [0.0]
def get(u, pace=0.12):
    for attempt in range(4):
        wait = pace - (time.time() - _last[0])
        if wait > 0: time.sleep(wait)
        _last[0] = time.time()
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "paper-bot"}), timeout=20).read())
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(1.5 * (attempt + 1)); continue
            raise
    raise RuntimeError("rate limited: " + u)
def now(): return datetime.now(timezone.utc)
def iso(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def r2(x): return round(x, 2) if abs(x) >= 100 else round(x, 4) if abs(x) >= 1 else round(x, 8)
_c1h = {}
def candles(sym):
    if sym not in _c1h:
        rows = sorted(get(f"{CB}/products/{sym}-USD/candles?granularity=3600"), key=lambda r: r[0])
        cut = int(now().timestamp()) // 3600 * 3600
        _c1h[sym] = [dict(t=r[0], l=r[1], h=r[2], o=r[3], c=r[4], v=r[5]) for r in rows if r[0] < cut]
    return _c1h[sym]
def daily(sym):
    rows = sorted(get(f"{CB}/products/{sym}-USD/candles?granularity=86400"), key=lambda r: r[0])
    return [dict(t=r[0], c=r[4], v=r[5]) for r in rows]
def price(sym): return float(get(f"{CB}/products/{sym}-USD/ticker")["price"])
def ema(v, n):
    k = 2 / (n + 1); e = sum(v[:n]) / n
    for x in v[n:]: e = x * k + e * (1 - k)
    return e
def atr(cs, n=14):
    tr = [max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])) for p, c in zip(cs, cs[1:])]
    return sum(tr[-n:]) / n
def load():
    p = json.load(open(PF)) if os.path.exists(PF) else dict(account="crypto-paper", starting_balance=START, cash=START, open=[], closed=[], equity_curve=[], day={}, runs=[])
    if "sleeves" not in p:
        if p["open"]: raise SystemExit("cannot migrate to sleeves with open positions")
        p["sleeves"] = {k: dict(alloc=v["alloc"], cash=v["alloc"]) for k, v in SLEEVES.items()}
    return p
def sync_cash(p): p["cash"] = round(sum(v["cash"] for v in p["sleeves"].values()), 6)
def universe(notes):
    cg = get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&price_change_percentage=24h,7d", pace=0)
    try: meme = {c["symbol"].upper() for c in get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&category=meme-token&order=market_cap_desc&per_page=250&page=1", pace=0)}
    except Exception: meme = set()
    cbp = {p["base_currency"] for p in get(f"{CB}/products") if p["quote_currency"] == "USD" and p["status"] == "online" and not p.get("trading_disabled")}
    out, seen = [], set()
    for c in cg:
        s = c["symbol"].upper(); nm = (c.get("name") or "").lower()
        if s in seen or s not in cbp or s in STABLE or any(w in " " + nm + " " for w in BADWORDS): continue
        if (c.get("total_volume") or 0) < MEME_VOL: continue
        seen.add(s)
        out.append(dict(sym=s, name=c["name"], image=c.get("image"), rank=c.get("market_cap_rank") or 999, price=c.get("current_price"),
                        meme=s in meme, vol=c.get("total_volume") or 0, p24=c.get("price_change_percentage_24h_in_currency") or 0, p7=c.get("price_change_percentage_7d_in_currency") or 0))
    notes.append(f"Universe: {len(cg)} CoinGecko top coins -> {len(out)} liquid Coinbase USD pairs (vol >= $2M, no stables/wrapped)")
    return out
def main():
    t0 = time.time(); p = load(); t = now(); today = t.strftime("%Y-%m-%d"); notes = []
    if p["day"].get("date") != today: p["day"] = dict(date=today, entries=0, losses=0)
    # 1) manage open positions
    still = []
    for pos in p["open"]:
        try:
            fee = pos.get("cost_rate", 0.001); hit = None
            for c in candles(pos["coin"]):
                if c["t"] < pos["entry_candle"]: continue
                if c["l"] <= pos["stop"]: hit = ("stop", pos["stop"], c["t"] + 3600); break
                if c["h"] >= pos["target"]: hit = ("target", pos["target"], c["t"] + 3600); break
            if not hit:
                lp = price(pos["coin"]); pos["last_price"] = lp
                if lp <= pos["stop"]: hit = ("stop", lp, int(t.timestamp()))
                elif lp >= pos["target"]: hit = ("target", lp, int(t.timestamp()))
            if hit:
                why, px, ts = hit; xf = px * pos["qty"] * fee
                pnl = (px - pos["entry"]) * pos["qty"] - xf - pos["entry_fee"]
                p["sleeves"][pos.get("sleeve", "core")]["cash"] += px * pos["qty"] - xf
                pos.update(exit=r2(px), exit_time=iso(ts), exit_reason=why, pnl=round(pnl, 2), R=round(pnl / pos["risk"], 2))
                p["closed"].append(pos)
                if pnl < 0:
                    if pos.get("sleeve") == "meme": p["day"]["meme_losses"] = p["day"].get("meme_losses", 0) + 1
                    else: p["day"]["losses"] += 1
                notes.append(f"CLOSED {pos['coin']} at {r2(px)} ({why}), P&L ${pnl:.2f} ({pos['R']}R)")
            else: still.append(pos)
        except Exception as e:
            still.append(pos); notes.append(f"{pos['coin']}: could not update ({e})")
    p["open"] = still
    sync_cash(p)
    def equity():
        eq = p["cash"]
        for o in p["open"]: eq += (o.get("last_price") or o["entry"]) * o["qty"]
        return eq
    # 2) universe + momentum ranking
    scan = dict(t=iso(int(t.timestamp())), scanned=0, universe=0, leaders=[], signals=[], watch=[], core=[], errors=0)
    try:
        uni = universe(notes); scan["universe"] = len(uni)
    except Exception as e:
        uni = []; notes.append(f"Universe fetch failed ({e})")
    for u in uni: u["pre"] = u["p24"] + 0.5 * u["p7"]
    pre = sorted(uni, key=lambda u: -u["pre"])
    cand = [u for u in pre if u["sym"] not in CORE and not u["meme"]][:TOP_N] + [u for u in pre if u["meme"] and u["sym"] not in CORE][:20] + [u for u in uni if u["sym"] in CORE]
    ranked = []
    for u in cand:
        try:
            d = daily(u["sym"])
            if len(d) < MIN_DAYS: notes.append(f"{u['sym']}: skipped, only {len(d)} days listed"); continue
            u["sleeve"] = "core" if u["sym"] in CORE else ("meme" if (u["meme"] or u["rank"] > 150) else "alts")
            if u["sleeve"] != "meme" and u["vol"] < MIN_VOL: continue
            crash = min((b["c"] / a["c"] - 1) for a, b in zip(d[-31:-1], d[-30:]))
            if u["sleeve"] == "meme" and crash < -0.4: notes.append(f"{u['sym']}: excluded from meme sleeve, {crash*100:.0f}% one-day drop in last 30d (rug sign)"); continue
            avg = sum(x["v"] * x["c"] for x in d[-31:-1]) / 30 or 1
            u["surge"] = round(u["vol"] / avg, 2) if avg > 0 else 1
            u["score"] = round(u["p24"] + 0.5 * u["p7"] + 10 * math.log2(max(u["surge"], 0.1)), 2)
            ranked.append(u)
        except Exception as e:
            scan["errors"] += 1
    ranked.sort(key=lambda u: -u["score"])
    scan["scanned"] = len(ranked)
    scan["leaders"] = [dict(sym=u["sym"], sleeve=u["sleeve"], name=u["name"], p24=round(u["p24"], 2), p7=round(u["p7"], 2), surge=u["surge"], score=u["score"], image=u["image"]) for u in ranked[:10]]
    # 3) 1h trend rules on ranked candidates
    sigs, watch = [], []
    for u in ranked:
        s = u["sym"]
        try:
            c = candles(s)
            if len(c) < 80: continue
            cl = [x["c"] for x in c]; last, prev = c[-1], c[-2]
            e20, e50, e20p = ema(cl, 20), ema(cl, 50), ema(cl[:-1], 20)
            hi24 = max(x["h"] for x in c[-25:-1]); a = atr(c)
            trend = last["c"] > e20 and last["c"] > e50 and e20 > e50
            brk = last["c"] > hi24 and prev["c"] <= max(x["h"] for x in c[-26:-2])
            pb = prev["l"] <= e20p * 1.002 and last["c"] > e20 and last["c"] > prev["c"] and last["c"] > last["o"]
            ctx = dict(close=r2(last["c"]), ema20=r2(e20), ema50=r2(e50), hi24=r2(hi24), atr=r2(a))
            if s in CORE: scan["core"].append(dict(sym=s, trend=trend, trigger=bool(brk or pb), **ctx))
            if trend and (brk or pb): sigs.append(dict(u=u, setup="BREAKOUT" if brk else "PULLBACK", a=a, e20=e20, e50=e50, hi24=hi24, last=last, ctx=ctx))
            elif trend: watch.append(dict(sym=s, sleeve=u["sleeve"], name=u["name"], image=u["image"], score=u["score"], p24=round(u["p24"], 2), dist=round((hi24 / last["c"] - 1) * 100, 2), why=f"uptrend, {round((hi24/last['c']-1)*100,2)}% below 24h high {r2(hi24)}", **ctx))
        except Exception:
            scan["errors"] += 1
    # 4) take signals in momentum order
    for sg in sigs:
        u = sg["u"]; s = u["sym"]; sl = u["sleeve"]; cfg = SLEEVES[sl]; rec = dict(sym=s, sleeve=sl, name=u["name"], image=u["image"], setup=sg["setup"], score=u["score"], p24=round(u["p24"], 2), **sg["ctx"])
        why = None
        if any(o["coin"] == s for o in p["open"]): why = "already open"
        elif sl != "meme" and sum(o.get("sleeve") != "meme" for o in p["open"]) >= MAX_OPEN: why = f"max {MAX_OPEN} open (core+alts)"
        elif sl == "meme" and sum(o.get("sleeve") == "meme" for o in p["open"]) >= cfg["max_open"]: why = "meme sleeve max 2 open"
        elif p["day"]["entries"] >= MAX_DAY: why = f"{MAX_DAY} entries today"
        elif sl != "meme" and p["day"]["losses"] >= MAX_LOSS: why = f"paused, {MAX_LOSS} losses today"
        elif sl == "meme" and p["day"].get("meme_losses", 0) >= MAX_LOSS: why = "meme sleeve paused after 2 losses today"
        if why: rec["action"] = "skipped: " + why; scan["signals"].append(rec); continue
        try:
            rate = cfg.get("cost") or (0.001 if u["rank"] <= 20 else 0.002)
            entry = price(s); a = sg["a"]; stop = entry - 1.5 * a
            if stop <= 0 or entry <= stop: rec["action"] = "skipped: invalid stop"; scan["signals"].append(rec); continue
            target = entry + 2 * (entry - stop); eq = equity(); risk = eq * cfg["risk"]; scash = p["sleeves"][sl]["cash"]
            qty = min(risk / (entry - stop), (scash * 0.999) / (entry * (1 + rate)))
            if qty * entry < 10: rec["action"] = f"skipped: size < $10 ({sl} sleeve cash)"; scan["signals"].append(rec); continue
            fee = entry * qty * rate; p["sleeves"][sl]["cash"] -= entry * qty + fee; sync_cash(p)
            last = sg["last"]
            thesis = (f"[{sl.upper()} sleeve] {sg['setup']} (momentum rank score {u['score']}: 24h {u['p24']:+.2f}%, 7d {u['p7']:+.2f}%, vol surge {u['surge']}x): 1h close {r2(last['c'])} above EMA20 {r2(sg['e20'])} and EMA50 {r2(sg['e50'])}" +
                      (f", fresh close above prior 24h high {r2(sg['hi24'])}." if sg["setup"] == "BREAKOUT" else ", pulled back to EMA20 and bounced on a green candle.") +
                      f" Stop 1.5xATR({r2(a)}), target 2R, cost {rate*100:.1f}%/side. Invalid on a close back below EMA20.")
            pos = dict(coin=s, sleeve=sl, name=u["name"], image=u["image"], setup=sg["setup"], entry=r2(entry), entry_time=iso(int(t.timestamp())), entry_candle=last["t"] + 3600,
                       stop=r2(stop), target=r2(target), qty=qty, risk=round((entry - stop) * qty, 2), entry_fee=fee, cost_rate=rate, thesis=thesis, last_price=entry)
            p["open"].append(pos); p["day"]["entries"] += 1
            rec["action"] = f"OPENED at {r2(entry)}, stop {r2(stop)}, target {r2(target)}"
            notes.append(f"OPENED [{sl}] {s} {sg['setup']} at {r2(entry)}, stop {r2(stop)}, target {r2(target)}, risk ${pos['risk']:.2f}")
        except Exception as e:
            rec["action"] = f"error: {e}"
        scan["signals"].append(rec)
    watch.sort(key=lambda w: w["dist"]); scan["watch"] = watch[:5]
    # active coins for display (open + signals + near-signal), excluding core
    act = {}
    for o in p["open"]: act[o["coin"]] = dict(sym=o["coin"], name=o.get("name", o["coin"]), image=o.get("image"), tag="OPEN")
    for sgr in scan["signals"]: act.setdefault(sgr["sym"], dict(sym=sgr["sym"], name=sgr["name"], image=sgr["image"], tag="SIGNAL"))
    for w in scan["watch"]: act.setdefault(w["sym"], dict(sym=w["sym"], name=w["name"], image=w["image"], tag="WATCH"))
    by = {u["sym"]: u for u in uni}
    scan["active"] = [dict(a, price=by.get(a["sym"], {}).get("price"), p24=round(by.get(a["sym"], {}).get("p24", 0), 2)) for a in act.values() if a["sym"] not in CORE][:5]
    scan["secs"] = round(time.time() - t0, 1)
    lead = ", ".join(f"{l['sym']} {l['p24']:+.1f}%/7d {l['p7']:+.1f}%" for l in scan["leaders"])
    notes.insert(0, f"SCAN: {scan['scanned']} coins ranked (universe {scan['universe']}), {len(sigs)} signal(s), {scan['secs']}s. Leaders: {lead}")
    if not sigs: notes.append("No trade signals: no ranked coin had trend + fresh 24h-high breakout or EMA20 pullback bounce on the last closed 1h candle.")
    for w in scan["watch"]: notes.append(f"WATCH {w['sym']}: {w['why']}")
    for cc in scan["core"]: notes.append(f"{cc['sym']}: {'uptrend' if cc['trend'] else 'trend filter fails'}{', trigger' if cc['trigger'] else ', no trigger'} (close {cc['close']}, EMA20 {cc['ema20']}, EMA50 {cc['ema50']}, 24h high {cc['hi24']})")
    sync_cash(p)
    for k, v in p["sleeves"].items():
        mv = sum((o.get("last_price") or o["entry"]) * o["qty"] for o in p["open"] if o.get("sleeve", "core") == k)
        v["equity"] = round(v["cash"] + mv, 2); v["pnl"] = round(v["equity"] - v["alloc"], 2)
        v["open"] = sum(o.get("sleeve", "core") == k for o in p["open"]); v["closed"] = sum(o.get("sleeve", "core") == k for o in p["closed"])
    notes.append("SLEEVES: " + ", ".join(f"{k} ${v['equity']:,.2f} ({v['pnl']:+.2f})" for k, v in p["sleeves"].items()))
    eq = equity(); p["equity"] = round(eq, 2); p["updated"] = scan["t"]; p["scan"] = scan
    p["equity_curve"].append(dict(t=p["updated"], equity=p["equity"])); p["equity_curve"] = p["equity_curve"][-3000:]
    p["runs"] = ([dict(t=p["updated"], notes=notes)] + p["runs"])[:50]
    json.dump(p, open(PF, "w"), indent=1)
    with open(JR, "w", newline="") as f:
        w = csv.writer(f); w.writerow(COLS)
        for x in p["closed"] + p["open"]:
            w.writerow([x["entry_time"][:10], x["coin"], f"{x.get('sleeve','core')}:{x['setup']}", "long", x["thesis"], x["entry_time"], x["entry"], x["stop"], x["target"], round(x["qty"], 6), x["risk"],
                        x.get("exit_time", ""), x.get("exit", ""), x.get("exit_reason", "OPEN"), x.get("pnl", ""), x.get("R", ""), "Y", "", ""])
    print("\n".join(notes)); print(f"Equity ${eq:,.2f}  cash ${p['cash']:,.2f}  open {len(p['open'])}  ({scan['secs']}s)")
if __name__ == "__main__": main()
