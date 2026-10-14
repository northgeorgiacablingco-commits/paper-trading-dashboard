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
# Meme safety: honeypot/mint/freeze/holder checks do not apply to or are not available for Coinbase-listed spot assets;
# Coinbase listing + >=30d history + order-book depth (+/-2%) >= $250K + no >40% one-day crash are used instead.
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
from zoneinfo import ZoneInfo
# ---- Approved playbook (PLAYBOOK.md, approved by Joey 2026-10-14) ----
CAP = {"core": 2500.0, "alts": 800.0, "meme": 400.0}      # max $ per coin per sleeve
COIN_MAX, DAILY_STOP, WEEK_DD, HALT_DD = 0.25, 200.0, 0.06, 0.15
TIME_STOP = {"core": 24, "alts": 24, "meme": 6}             # hours to reach +1R
NEWS_WORDS = ("fomc", "cpi", "fed decision", "rate decision", "powell", "jobs report", "nonfarm")
def c15(sym):
    rows = sorted(get(f"{CB}/products/{sym}-USD/candles?granularity=900"), key=lambda r: r[0])
    cut = int(now().timestamp()) // 900 * 900
    return [dict(t=r[0], l=r[1], h=r[2], o=r[3], c=r[4], v=r[5]) for r in rows if r[0] < cut]
def depth2(sym, px):
    b = get(f"{CB}/products/{sym}-USD/book?level=2")
    return sum(float(q) * float(pz) for pz, q, _ in b["bids"] if float(pz) >= px * 0.98) + sum(float(q) * float(pz) for pz, q, _ in b["asks"] if float(pz) <= px * 1.02)
def news_soon():
    for fn in ("headlines.json", "crypto_headlines.json"):
        try:
            for h in json.load(open(os.path.join(ROOT, "data", fn))).get("headlines", []):
                ts = h.get("published") or h.get("time") or h.get("date")
                if not ts: continue
                try: age = (now() - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds()
                except Exception: continue
                if -1800 <= age <= 1800 and any(w in h.get("title", "").lower() for w in NEWS_WORDS): return h["title"]
        except Exception: pass
    return None
def close_pos(p, pos, px, ts, why, notes, still_open=False):
    fee = pos.get("cost_rate", 0.001); xf = px * pos["qty"] * fee
    p["sleeves"][pos.get("sleeve", "core")]["cash"] += px * pos["qty"] - xf
    pnl = (px - pos["entry"]) * pos["qty"] - xf - pos["entry_fee"] * (pos["qty"] / pos.get("qty0", pos["qty"])) + pos.get("realized", 0)
    pos.update(exit=r2(px), exit_time=iso(ts), exit_reason=why, pnl=round(pnl, 2), R=round(pnl / pos["risk"], 2))
    p["closed"].append(pos)
    if pnl < 0:
        if pos.get("sleeve") == "meme": p["day"]["meme_losses"] = p["day"].get("meme_losses", 0) + 1
        else: p["day"]["losses"] += 1
    p["day"]["realized"] = round(p["day"].get("realized", 0) + pnl, 2)
    notes.append(f"CLOSED [{pos.get('sleeve','core')}] {pos['coin']} at {r2(px)} ({why}), P&L ${pnl:.2f} ({pos['R']}R)")
def manage(p, pos, t, notes):
    """Returns True if still open. Processes each new closed 1h candle once (pos['seen'])."""
    sl = pos.get("sleeve", "core"); fee = pos.get("cost_rate", 0.001)
    pos.setdefault("qty0", pos["qty"]); pos.setdefault("risk_ps", pos["entry"] - pos["stop"]); pos.setdefault("hi", pos["entry"]); pos.setdefault("maxR", 0.0)
    pos.setdefault("seen", pos["entry_candle"]); pos.setdefault("level", pos["entry"]); R1 = pos["risk_ps"]
    cs = candles(pos["coin"]); new = [c for c in cs if c["t"] >= pos["seen"]]
    a = atr(cs) if len(cs) > 15 else R1 / 1.5
    for c in new:
        pos["seen"] = c["t"] + 3600
        if c["l"] <= pos["stop"]:
            close_pos(p, pos, pos["stop"], c["t"] + 3600, "breakeven/trail stop" if pos.get("half") else "stop", notes); return False
        pos["hi"] = max(pos["hi"], c["h"]); pos["maxR"] = max(pos["maxR"], (c["h"] - pos["entry"]) / R1)
        if not pos.get("half") and c["h"] >= pos["entry"] + 2 * R1:
            px = pos["entry"] + 2 * R1; q = pos["qty"] / 2; xf = px * q * fee
            part = (px - pos["entry"]) * q - xf - pos["entry_fee"] * (q / pos["qty0"])
            p["sleeves"][sl]["cash"] += px * q - xf; pos["qty"] -= q; pos["realized"] = round(pos.get("realized", 0) + part, 2)
            pos["half"] = True; pos["stop"] = r2(max(pos["stop"], pos["entry"]))
            notes.append(f"PARTIAL [{sl}] {pos['coin']}: sold 50% at +2R {r2(px)} (${part:.2f}), stop to breakeven {pos['stop']}")
        if pos.get("half"):
            trail = pos["hi"] * 0.85 if sl == "meme" else pos["hi"] - 2 * a
            if trail > pos["stop"]: pos["stop"] = r2(trail)     # never widen
    if new and len(cs) > 50:
        last = cs[-1]; e20 = ema([x["c"] for x in cs], 20)
        if sl != "meme" and last["c"] < e20:
            close_pos(p, pos, price(pos["coin"]), int(t.timestamp()), f"early cut: 1h close {r2(last['c'])} below EMA20 {r2(e20)}", notes); return False
        if sl == "meme" and last["c"] < pos["level"]:
            close_pos(p, pos, price(pos["coin"]), int(t.timestamp()), f"early cut: 1h close back below breakout level {pos['level']}", notes); return False
    hrs = (t - datetime.fromisoformat(pos["entry_time"].replace("Z", "+00:00"))).total_seconds() / 3600
    lp = price(pos["coin"]); pos["last_price"] = lp; pos["maxR"] = max(pos["maxR"], (lp - pos["entry"]) / R1)
    if lp <= pos["stop"]: close_pos(p, pos, lp, int(t.timestamp()), "stop (live price)", notes); return False
    if hrs >= TIME_STOP[sl] and pos["maxR"] < 1 and not pos.get("half"):
        close_pos(p, pos, lp, int(t.timestamp()), f"time stop: not +1R within {TIME_STOP[sl]}h", notes); return False
    return True
def main():
    t0 = time.time(); p = load(); t = now(); today = t.strftime("%Y-%m-%d"); notes = []
    if p["day"].get("date") != today: p["day"] = dict(date=today, entries=0, losses=0, meme_losses=0, realized=0)
    # 1) manage open positions (stops, 50% at 2R, breakeven + trail, early cut, time stop)
    still = []
    for pos in p["open"]:
        try:
            if manage(p, pos, t, notes): still.append(pos)
        except Exception as e:
            still.append(pos); notes.append(f"{pos['coin']}: could not update ({e})")
    p["open"] = still
    sync_cash(p)
    def equity():
        eq = p["cash"]
        for o in p["open"]: eq += (o.get("last_price") or o["entry"]) * o["qty"]
        return eq
    # 1b) account-level risk state
    eq0 = equity(); g = p.setdefault("risk", {})
    if g.get("day") != today: g["day"] = today; g["day_start_eq"] = round(eq0, 2)
    wk = t.strftime("%G-W%V")
    if g.get("week") != wk:
        if g.get("week") and g.get("week_low", 1e18) <= g.get("week_start_eq", 0) * (1 - WEEK_DD): g["half_week"] = wk
        g["week"] = wk; g["week_start_eq"] = round(eq0, 2); g["week_low"] = eq0
    g["week_low"] = min(g.get("week_low", eq0), eq0); g["peak"] = max(g.get("peak", START), eq0)
    blocks = []
    if eq0 - g["day_start_eq"] <= -DAILY_STOP: blocks.append(f"daily loss stop hit ({eq0 - g['day_start_eq']:+.2f} <= -${DAILY_STOP:.0f})")
    if eq0 <= g["peak"] * (1 - HALT_DD): blocks.append(f"HALT: account -15% from peak ${g['peak']:,.2f}, stop and review")
    half_all = g.get("half_week") == wk or g["week_low"] <= g["week_start_eq"] * (1 - WEEK_DD)
    regime, meme_off_why = "normal", None
    try:
        bc = candles("BTC"); be50 = ema([x["c"] for x in bc], 50); r24 = bc[-24:]
        rng = (max(x["h"] for x in r24) - min(x["l"] for x in r24)) / bc[-1]["c"]
        if bc[-1]["c"] < be50: regime = "risk-off"; meme_off_why = "BTC below 1h EMA50"
        if rng < 0.015: blocks.append(f"chop: BTC 24h range {rng*100:.2f}% < 1.5%")
        notes.append(f"REGIME: {regime} (BTC 1h close {r2(bc[-1]['c'])} vs EMA50 {r2(be50)}, 24h range {rng*100:.2f}%)")
    except Exception as e:
        regime = "unknown"; blocks.append(f"BTC regime unavailable ({e})")
    nw = news_soon()
    if nw: blocks.append(f"major news within 30 min: {nw}")
    et = t.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() < 5 and (et.hour, et.minute) >= (9, 30) and et.hour < 10: meme_off_why = meme_off_why or "no meme entries 9:30-10:00 ET"
    # 2) universe + momentum ranking
    scan = dict(t=iso(int(t.timestamp())), scanned=0, universe=0, leaders=[], signals=[], watch=[], core=[], errors=0)
    try:
        uni = universe(notes); scan["universe"] = len(uni)
    except Exception as e:
        uni = []; notes.append(f"Universe fetch failed ({e})")
    if uni:
        tv = sum(u["vol"] for u in uni); vh = [x for x in p.get("vol_hist", []) if x[0] >= t.timestamp() - 7 * 86400]
        if len(vh) >= 12 and (vh[-1][0] - vh[0][0]) >= 86400:
            av = sum(x[1] for x in vh) / len(vh)
            if tv < 0.7 * av: blocks.append(f"low market volume: ${tv/1e9:.1f}B < 70% of 7-day avg ${av/1e9:.1f}B")
        else: notes.append("Market-volume gate: collecting history (needs >= 1 day of runs)")
        if not vh or t.timestamp() - vh[-1][0] >= 3600: vh.append([int(t.timestamp()), round(tv)])
        p["vol_hist"] = vh[-200:]
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
    for i, u in enumerate([u for u in ranked if u["sleeve"] == "alts"]): u["mrank"] = i
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
            v20 = sum(x["v"] for x in c[-21:-1]) / 20 or 1; vx = last["v"] / v20
            ctx = dict(close=r2(last["c"]), ema20=r2(e20), ema50=r2(e50), hi24=r2(hi24), atr=r2(a), volx=round(vx, 2))
            sl = u["sleeve"]; setup = None; fail = None
            if s in CORE: scan["core"].append(dict(sym=s, trend=trend, trigger=bool(brk or pb), **ctx))
            if sl == "core":
                if trend and (brk or pb): setup = "BREAKOUT" if brk else "PULLBACK"; fail = None if vx >= 1.2 else f"signal-candle volume {vx:.2f}x < 1.2x"
            elif sl == "alts":
                if trend and brk:
                    setup = "BREAKOUT"
                    fail = (None if u.get("mrank", 99) < 20 else "not top-20 momentum") or (None if u["surge"] >= 2 else f"volume surge {u['surge']}x < 2x") or (None if u["p24"] <= 25 else f"up {u['p24']:.1f}% in 24h (>25%, chasing)")
            else:
                m = c15(s)
                if len(m) >= 40:
                    l15 = m[-1]; h4 = max(x["h"] for x in m[-17:-1]); v15 = sum(x["v"] for x in m[-21:-1]) / 20 or 1
                    a15 = sum(x["h"] - x["l"] for x in m[-15:-1]) / 14 or 1
                    ctx.update(hi4h=r2(h4), vol15x=round(l15["v"] / v15, 2))
                    if trend and l15["c"] > h4:
                        setup = "MEME-BREAKOUT"
                        fail = (None if l15["v"] >= 3 * v15 else f"15m volume {l15['v']/v15:.1f}x < 3x") or (None if (l15["h"] - l15["l"]) <= 3 * a15 else "vertical 15m candle, no chase")
                        sg_extra = dict(level=h4, bl=l15["l"])
            if setup: sigs.append(dict(u=u, setup=setup, a=a, e20=e20, e50=e50, hi24=hi24, last=last, ctx=ctx, fail=fail, **(sg_extra if setup == "MEME-BREAKOUT" else {})))
            elif trend: watch.append(dict(sym=s, sleeve=sl, name=u["name"], image=u["image"], score=u["score"], p24=round(u["p24"], 2), dist=round((hi24 / last["c"] - 1) * 100, 2), why=f"uptrend, {round((hi24/last['c']-1)*100,2)}% below 24h high {r2(hi24)}", **ctx))
        except Exception:
            scan["errors"] += 1
    # 4) take signals in momentum order
    for sg in sigs:
        u = sg["u"]; s = u["sym"]; sl = u["sleeve"]; cfg = SLEEVES[sl]; rec = dict(sym=s, sleeve=sl, name=u["name"], image=u["image"], setup=sg["setup"], score=u["score"], p24=round(u["p24"], 2), **sg["ctx"])
        why = sg.get("fail")
        if why: pass
        elif blocks: why = "stand aside: " + "; ".join(blocks)
        elif sl == "meme" and meme_off_why: why = "meme sleeve off: " + meme_off_why
        elif any(o["coin"] == s for o in p["open"]): why = "already open"
        elif sl != "meme" and sum(o.get("sleeve") != "meme" for o in p["open"]) >= MAX_OPEN: why = f"max {MAX_OPEN} open (core+alts)"
        elif sl == "meme" and sum(o.get("sleeve") == "meme" for o in p["open"]) >= cfg["max_open"]: why = "meme sleeve max 2 open"
        elif p["day"]["entries"] >= MAX_DAY: why = f"{MAX_DAY} entries today"
        elif sl != "meme" and p["day"]["losses"] >= MAX_LOSS: why = f"paused, {MAX_LOSS} losses today"
        elif sl == "meme" and p["day"].get("meme_losses", 0) >= MAX_LOSS: why = "meme sleeve paused after 2 losses today"
        if why: rec["action"] = "skipped: " + why; scan["signals"].append(rec); continue
        try:
            rate = cfg.get("cost") or (0.001 if u["rank"] <= 20 else 0.002)
            entry = price(s); a = sg["a"]; stop = entry - 1.5 * a
            if sl == "meme":
                stop = max(entry * 0.88, sg["bl"])
                liq = depth2(s, entry)
                if liq < 250000: rec["action"] = f"skipped: order-book depth +/-2% ${liq:,.0f} < $250K"; scan["signals"].append(rec); continue
            if stop <= 0 or entry <= stop: rec["action"] = "skipped: invalid stop"; scan["signals"].append(rec); continue
            target = entry + 2 * (entry - stop); eq = equity(); mult = (0.5 if half_all else 1) * (0.5 if sl == "core" and regime == "risk-off" else 1)
            risk = eq * cfg["risk"] * mult; scash = p["sleeves"][sl]["cash"]
            held = sum((o.get("last_price") or o["entry"]) * o["qty"] for o in p["open"] if o["coin"] == s)
            cap = min(CAP[sl], COIN_MAX * eq - held)
            qty = min(risk / (entry - stop), cap / entry, (scash * 0.999) / (entry * (1 + rate)))
            if qty * entry < 10: rec["action"] = f"skipped: size < $10 ({sl} sleeve cash)"; scan["signals"].append(rec); continue
            fee = entry * qty * rate; p["sleeves"][sl]["cash"] -= entry * qty + fee; sync_cash(p)
            last = sg["last"]
            thesis = (f"[{sl.upper()} sleeve] {sg['setup']} (momentum rank score {u['score']}: 24h {u['p24']:+.2f}%, 7d {u['p7']:+.2f}%, vol surge {u['surge']}x): 1h close {r2(last['c'])} above EMA20 {r2(sg['e20'])} and EMA50 {r2(sg['e50'])}" +
                      (f", fresh close above prior 24h high {r2(sg['hi24'])}." if sg["setup"] == "BREAKOUT" else ", pulled back to EMA20 and bounced on a green candle.") +
                      (f" Stop {'min(12%, breakout-candle low)' if sl=='meme' else '1.5xATR('+str(r2(a))+')'}, sell 50% at 2R then breakeven + trail, time stop {TIME_STOP[sl]}h, cost {rate*100:.1f}%/side, size x{mult}. Invalid on a 1h close back below {'the breakout level' if sl=='meme' else 'EMA20'}."))
            pos = dict(coin=s, sleeve=sl, name=u["name"], image=u["image"], setup=sg["setup"], entry=r2(entry), entry_time=iso(int(t.timestamp())), entry_candle=last["t"] + 3600,
                       stop=r2(stop), target=r2(target), qty=qty, risk=round((entry - stop) * qty, 2), entry_fee=fee, cost_rate=rate, thesis=thesis, last_price=entry, qty0=qty, risk_ps=entry - stop, hi=entry, maxR=0.0, seen=last["t"] + 3600, level=r2(sg.get("level", entry)))
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
    scan["regime"] = regime; scan["blocks"] = blocks; scan["half_size"] = half_all; scan["meme_off"] = meme_off_why
    if blocks: notes.insert(1, "NO NEW ENTRIES: " + "; ".join(blocks))
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
