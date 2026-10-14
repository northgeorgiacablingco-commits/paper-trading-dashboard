#!/usr/bin/env python3
"""PAPER crypto trend-following bot over the liquid USD spot market. Simulated only: no real money, wallets or exchanges.
Universe (each run): CoinGecko top 250 by market cap ∩ Coinbase Exchange online USD pairs.
Filters: 24h volume >= $5M, >= 30 days of daily candles on Coinbase, no stablecoins / wrapped / staked / pegged tokens.
Ranking: momentum score = 24h% + 0.5 x 7d% + 10 x log2(volume surge), surge = 24h $vol / 30d avg daily $vol.
Rules on the top candidates (1h, closed candles only): close above EMA20 and EMA50 with EMA20 > EMA50; trigger = fresh
close above the prior 24h high (BREAKOUT) or an EMA20 pullback bounce (PULLBACK). Stop = entry - 1.5 x ATR(14), target 2R.
Risk 1% of equity; spot longs 1x; BTC/ETH/SOL/XRP via Coinbase Derivatives US perps (long+short, default 2x, max 3x, isolated), max 3 open, max 5 entries/UTC day, pause after 2 losses/day.
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
# ---- Shorts & leverage (approved by Joey 2026-10-14), REAL-MONEY-REALISTIC for a US trader ----
# Only BTC/ETH/SOL/XRP, via Coinbase Derivatives (CFTC-regulated DCM) US perpetual-style futures, cleared by Coinbase
# Financial Markets (FCM). Whole contracts only (contract sizes from the live Coinbase product API).
PERP = {"BTC": dict(pid="BIP-20DEC30-CDE", cs=0.01, mmr=0.10), "ETH": dict(pid="ETP-20DEC30-CDE", cs=0.1, mmr=0.12),
        "SOL": dict(pid="SLP-20DEC30-CDE", cs=5.0, mmr=0.15), "XRP": dict(pid="XPP-20DEC30-CDE", cs=500.0, mmr=0.15)}
# mmr = ASSUMED maintenance margin per isolated position (conservative; real CFM margin is set by the clearinghouse and
# can change; overnight margin is higher than intraday). Fees = Coinbase published perp fee structure:
# 0.02% of notional + $0.15/contract per side; liquidation trades 0.10% + $0.15/contract.
LEV_DEFAULT, LEV_MAX = 2.0, 3.0
PERP_FEE, PERP_LIQ_FEE, PER_CONTRACT = 0.0002, 0.0010, 0.15
SLIP = 0.0003                                  # assumed slippage per side on perp fills
FUND_LONG_MAX, FUND_SHORT_MIN = 0.0005, -0.0005  # 8h-equivalent funding filter
EXPO_MAX, MAX_SHORTS, OPEN_RISK_MAX, LIQ_BUFFER, PAUSE_DAYS = 1.5, 2, 0.04, 3.0, 7
def derivs(notes):
    """Funding (hourly) + open interest. Primary: Coinbase Derivatives public API (the venue we would actually use).
    Fallback: OKX (8h rate / 8, labeled PROXY), then Kraken Futures (PROXY)."""
    out = {}
    try:
        d = get("https://api.coinbase.com/api/v3/brokerage/market/products?product_type=FUTURE&limit=500", pace=0)
        byp = {x["product_id"]: x for x in d.get("products", [])}
        for c, m in PERP.items():
            x = byp.get(m["pid"]);
            if not x: continue
            fd = x.get("future_product_details") or {}
            out[c] = dict(src="Coinbase Derivatives", proxy=False, pid=m["pid"], perp_px=float(x.get("price") or 0), fr_1h=float(fd.get("funding_rate") or 0),
                          oi=float(fd.get("open_interest") or 0), oi_unit="contracts", cs=float(fd.get("contract_size") or m["cs"]))
    except Exception as e: notes.append(f"DERIVS: Coinbase Derivatives API failed ({e}), using proxy")
    for c in PERP:
        if c in out: continue
        try:
            r = get(f"https://www.okx.com/api/v5/public/funding-rate?instId={c}-USDT-SWAP", pace=0)["data"][0]
            oi = get(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={c}-USDT-SWAP", pace=0)["data"][0]
            out[c] = dict(src="OKX (PROXY, not US-accessible)", proxy=True, pid=PERP[c]["pid"], perp_px=None, fr_1h=float(r["fundingRate"]) / 8, oi=float(oi.get("oiCcy") or 0), oi_unit=c, cs=PERP[c]["cs"]); continue
        except Exception: pass
        try:
            k = {"BTC": "PF_XBTUSD"}.get(c, f"PF_{c}USD")
            tk = next(x for x in get("https://futures.kraken.com/derivatives/api/v3/tickers", pace=0)["tickers"] if x.get("symbol") == k)
            mk = float(tk.get("markPrice") or 1)
            out[c] = dict(src="Kraken Futures (PROXY)", proxy=True, pid=PERP[c]["pid"], perp_px=mk, fr_1h=float(tk.get("fundingRate") or 0) / mk, oi=float(tk.get("openInterest") or 0), oi_unit=c, cs=PERP[c]["cs"])
        except Exception as e: notes.append(f"DERIVS: no funding data for {c} ({e}); perp entries on {c} blocked")
    return out
def liq_price(side, entry, lev, mmr):
    """Isolated-margin liquidation: equity (margin + PnL) falls to maintenance margin (mmr x notional)."""
    return entry * (1 - 1 / lev + mmr) if side == "long" else entry * (1 + 1 / lev - mmr)
def pick_lev(side, entry, stop, mmr):
    """Default 2x; must keep liquidation distance >= 3x stop distance (on mark). Else step down; 1x or skip."""
    sd = abs(entry - stop)
    for lev in (LEV_DEFAULT, 1.5, 1.0):
        if abs(entry - liq_price(side, entry, lev, mmr)) >= LIQ_BUFFER * sd: return lev
    return None
def pos_value(o, px=None):
    px = px or o.get("last_price") or o["entry"]
    if o.get("inst") == "perp": return o["margin"] + o["sd"] * (px - o["entry"]) * o["qty"] - o.get("funding_paid", 0)
    return px * o["qty"]
def notional(o): return (o.get("last_price") or o["entry"]) * o["qty"]
def open_risk(o): return max(0.0, o.get("sd", 1) * ((o.get("last_price") or o["entry"]) - o["stop"]) * o["qty"])
def perp_fee(px, qty, cs, liq=False): return px * qty * ((PERP_LIQ_FEE if liq else PERP_FEE) + (0 if liq else SLIP)) + PER_CONTRACT * round(qty / cs)
def close_perp(p, pos, px, ts, why, notes, liq=False):
    sl = pos.get("sleeve", "core"); xf = perp_fee(px, pos["qty"], pos["cs"], liq)
    gross = pos["sd"] * (px - pos["entry"]) * pos["qty"]
    back = max(0.0, pos["margin"] + gross - pos.get("funding_paid", 0) - xf)   # isolated: cannot lose more than margin
    p["sleeves"][sl]["cash"] += back
    pnl = back - pos["margin"] - pos["entry_fee"] * (pos["qty"] / pos["qty0"]) + pos.get("realized", 0)
    pos.update(exit=r2(px), exit_time=iso(ts), exit_reason=why, pnl=round(pnl, 2), R=round(pnl / pos["risk"], 2), exit_fee=round(xf, 2))
    p["closed"].append(pos); p["day"]["realized"] = round(p["day"].get("realized", 0) + pnl, 2)
    if pnl < 0: p["day"]["losses"] += 1
    if liq:
        until = iso(ts + PAUSE_DAYS * 86400); p.setdefault("risk", {})["perp_pause_until"] = until
        p.setdefault("alerts", []).append(dict(t=iso(ts), type="LIQUIDATION", big=True, coin=pos["coin"], side=pos["side"], lev=pos["lev"], pnl=round(pnl, 2),
                                               msg=f"SIMULATED LIQUIDATION {pos['side'].upper()} {pos['coin']} {pos['lev']}x at mark {r2(px)}; perps paused until {until}"))
        p["alerts"] = p["alerts"][-50:]
    notes.append(f"{'LIQUIDATED' if liq else 'CLOSED'} [perp {pos['side']} {pos['lev']}x] {pos['coin']} at {r2(px)} ({why}), P&L ${pnl:.2f} ({pos['R']}R), funding ${pos.get('funding_paid',0):.2f}")
def accrue_funding(pos, dv, t):
    """Hourly funding on full notional at each top of hour since last accrual. Positive rate: longs pay shorts."""
    d = dv.get(pos["coin"]); last = pos.get("fund_t", pos["entry_candle"] - 3600)
    hrs = int(t.timestamp()) // 3600 * 3600; n = max(0, (hrs - last) // 3600)
    if d and n:
        amt = n * d["fr_1h"] * notional(pos) * (1 if pos["side"] == "long" else -1)
        pos["funding_paid"] = round(pos.get("funding_paid", 0) + amt, 4); pos["fund_src"] = d["src"]
    if n: pos["fund_t"] = hrs
def manage_perp(p, pos, t, notes, dv):
    sd = pos["sd"]; R1 = pos["risk_ps"]; cs = candles(pos["coin"]); new = [c for c in cs if c["t"] >= pos["seen"]]
    a = atr(cs) if len(cs) > 15 else R1 / 1.5
    accrue_funding(pos, dv, t)
    for c in new:
        pos["seen"] = c["t"] + 3600
        lo_side = c["l"] if sd > 0 else c["h"]                     # worst mark in candle (Coinbase spot = mark/index proxy)
        hit = lambda lvl, x: (x <= lvl) if sd > 0 else (x >= lvl)
        if hit(pos["liq"], c["o"]):                                 # gapped through stop straight to liquidation
            close_perp(p, pos, pos["liq"], c["t"], "LIQUIDATED: candle opened beyond liquidation price", notes, liq=True); return False
        if hit(pos["stop"], lo_side):
            close_perp(p, pos, pos["stop"], c["t"] + 3600, "breakeven/trail stop" if pos.get("half") else "stop (mark)", notes); return False
        fav = c["h"] if sd > 0 else c["l"]
        pos["hi"] = max(pos["hi"], fav) if sd > 0 else min(pos["hi"], fav); pos["maxR"] = max(pos["maxR"], sd * (fav - pos["entry"]) / R1)
        tgt = pos["entry"] + sd * 2 * R1
        if not pos.get("half") and sd * (fav - tgt) >= 0:
            n = int(round(pos["qty"] / pos["cs"])); h = n // 2
            if h == 0: close_perp(p, pos, tgt, c["t"] + 3600, "target 2R (1 contract, cannot split)", notes); return False
            q = h * pos["cs"]; xf = perp_fee(tgt, q, pos["cs"]); share = q / pos["qty"]
            part = sd * (tgt - pos["entry"]) * q - xf - pos["entry_fee"] * (q / pos["qty0"]) - pos.get("funding_paid", 0) * share
            p["sleeves"][pos["sleeve"]]["cash"] += pos["margin"] * share + sd * (tgt - pos["entry"]) * q - xf - pos.get("funding_paid", 0) * share
            pos["margin"] *= (1 - share); pos["funding_paid"] = pos.get("funding_paid", 0) * (1 - share); pos["qty"] -= q
            pos["realized"] = round(pos.get("realized", 0) + part, 2); pos["half"] = True
            pos["stop"] = r2(max(pos["stop"], pos["entry"]) if sd > 0 else min(pos["stop"], pos["entry"]))
            notes.append(f"PARTIAL [perp {pos['side']}] {pos['coin']}: closed {h} of {n} contracts at +2R {r2(tgt)} (${part:.2f}), stop to breakeven")
        if pos.get("half"):
            tr = pos["hi"] - sd * 2 * a
            if sd * (tr - pos["stop"]) > 0: pos["stop"] = r2(tr)
    if new and len(cs) > 50:
        last = cs[-1]; e20 = ema([x["c"] for x in cs], 20)
        if sd * (last["c"] - e20) < 0:
            close_perp(p, pos, price(pos["coin"]), int(t.timestamp()), f"early cut: 1h close {r2(last['c'])} {'below' if sd>0 else 'above'} EMA20 {r2(e20)}", notes); return False
    lp = price(pos["coin"]); pos["last_price"] = lp; pos["maxR"] = max(pos["maxR"], sd * (lp - pos["entry"]) / R1)
    d = dv.get(pos["coin"]) or {}; pos["perp_px"] = d.get("perp_px")
    if sd * (lp - pos["liq"]) <= 0: close_perp(p, pos, pos["liq"], int(t.timestamp()), "LIQUIDATED (live mark)", notes, liq=True); return False
    if sd * (lp - pos["stop"]) <= 0: close_perp(p, pos, lp, int(t.timestamp()), "stop (live mark)", notes); return False
    if pos.get("funding_paid", 0) > 0.5 * pos["risk"]: close_perp(p, pos, lp, int(t.timestamp()), f"funding exit: paid ${pos['funding_paid']:.2f} > 0.5R", notes); return False
    hrs = (t - datetime.fromisoformat(pos["entry_time"].replace("Z", "+00:00"))).total_seconds() / 3600
    if hrs >= TIME_STOP["core"] and pos["maxR"] < 1 and not pos.get("half"):
        close_perp(p, pos, lp, int(t.timestamp()), f"time stop: not +1R within {TIME_STOP['core']}h", notes); return False
    return True
def perp_precheck(side, entry, stop, qty=None, contracts=None, cs=None, tol=1e-9):
    """Pre-trade guard: stop on the correct side of entry, and qty == contracts x contract size. Returns a block reason or None."""
    if stop <= 0 or (side == "long" and not stop < entry) or (side == "short" and not stop > entry):
        return f"BLOCKED invalid stop: {side} entry {r2(entry)} stop {r2(stop)} (wrong side)"
    if qty is not None and abs(qty - contracts * cs) > tol * max(1.0, abs(qty)):
        return f"BLOCKED size mismatch: qty {qty} != {contracts} x {cs}"
    return None
def perp_entry(p, sg, u, s, short, regime, half_all, dv, equity, t, rec, notes):
    """Returns a skip reason, or True when a perp position was opened."""
    side = "short" if short else "long"; sd = -1 if short else 1; m = PERP[s]; d = dv.get(s); g = p.get("risk", {})
    if g.get("perp_pause_until") and iso(int(t.timestamp())) < g["perp_pause_until"]: return f"perps paused after a liquidation until {g['perp_pause_until']}"
    if not d: return "no funding data, perp entry blocked"
    if short and regime != "risk-off": return "shorts only when BTC 1h close < EMA50"
    f8 = d["fr_1h"] * 8
    if not short and f8 >= FUND_LONG_MAX: return f"funding {f8*100:+.3f}%/8h >= +0.05% (crowded longs)"
    if short and f8 <= FUND_SHORT_MIN: return f"funding {f8*100:+.3f}%/8h <= -0.05% (crowded shorts)"
    if short and sum(o.get("side") == "short" for o in p["open"]) >= MAX_SHORTS: return f"max {MAX_SHORTS} shorts open"
    entry = price(s) * (1 + sd * SLIP); a = sg["a"]; stop = entry - sd * 1.5 * a
    bad = perp_precheck(side, entry, stop)
    if bad: notes.append(f"FLAG [perp {side}] {s}: {bad}"); return bad
    lev = pick_lev(side, entry, stop, m["mmr"])
    if not lev: return "liquidation buffer: cannot keep liq >= 3x stop distance even at 1x"
    eq = equity(); mult = (0.5 if half_all else 1) * (0.5 if (not short and regime == "risk-off") else 1)
    risk = eq * SLEEVES["core"]["risk"] * mult
    expo = sum(notional(o) for o in p["open"]); room = EXPO_MAX * eq - expo
    orisk = sum(open_risk(o) for o in p["open"]); rroom = OPEN_RISK_MAX * eq - orisk
    held = sum(notional(o) for o in p["open"] if o["coin"] == s)
    capn = min(CAP["core"] * lev, COIN_MAX * eq * lev - held, room)
    q = min(risk, rroom) / abs(entry - stop); q = min(q, capn / entry)
    n = int(q // m["cs"])
    cash = p["sleeves"]["core"]["cash"]
    while n > 0 and (n * m["cs"] * entry / lev + perp_fee(entry, n * m["cs"], m["cs"])) > cash * 0.999: n -= 1
    if n < 1:
        one = m["cs"] * abs(entry - stop)
        return f"1 contract ({m['cs']} {s}) risks ${one:.2f} / ${m['cs']*entry:,.0f} notional; exceeds risk ${min(risk, rroom):.2f}, exposure or cash limits"
    qty = n * m["cs"]; notl = qty * entry; margin = notl / lev; fee = perp_fee(entry, qty, m["cs"])
    bad = perp_precheck(side, entry, stop, qty, n, m["cs"])
    if bad: notes.append(f"FLAG [perp {side}] {s}: {bad}"); return bad
    p["sleeves"]["core"]["cash"] -= margin + fee; sync_cash(p)
    liq = liq_price(side, entry, lev, m["mmr"]); target = entry + sd * 2 * abs(entry - stop); last = sg["last"]
    thesis = (f"[CORE perp {side.upper()} {lev}x isolated, {m['pid']} Coinbase Derivatives] {sg['setup']}: 1h close {r2(last['c'])} "
              + (f"below EMA20 {r2(sg['e20'])} and EMA50 {r2(sg['e50'])}, BTC regime {regime}" if short else f"above EMA20 {r2(sg['e20'])} and EMA50 {r2(sg['e50'])}")
              + f". {n} contract(s) x {m['cs']} {s} = ${notl:,.0f} notional, margin ${margin:,.0f}. Stop 1.5xATR {r2(stop)}, liq {r2(liq)} (>= 3x stop distance, assumed MM {m['mmr']*100:.0f}%),"
              + f" funding {f8*100:+.4f}%/8h [{d['src']}]. Fees 0.02%+$0.15/contract/side + {SLIP*100:.2f}% slippage.")
    pos = dict(coin=s, sleeve="core", inst="perp", side=side, sd=sd, pid=m["pid"], cs=m["cs"], contracts=n, lev=lev, margin=margin, liq=r2(liq), mmr=m["mmr"], funding_paid=0.0,
               fund_t=int(t.timestamp()) // 3600 * 3600, fund_src=d["src"], name=u["name"], image=u["image"], setup=sg["setup"], entry=r2(entry), entry_time=iso(int(t.timestamp())),
               entry_candle=last["t"] + 3600, stop=r2(stop), target=r2(target), qty=qty, risk=round(abs(entry - stop) * qty, 2), entry_fee=fee, cost_rate=PERP_FEE + SLIP,
               thesis=thesis, last_price=entry, qty0=qty, risk_ps=abs(entry - stop), hi=entry, maxR=0.0, seen=last["t"] + 3600, level=r2(entry), perp_px=d.get("perp_px"))
    p["open"].append(pos); p["day"]["entries"] += 1
    rec["action"] = f"OPENED {side.upper()} {lev}x {n} contract(s) at {r2(entry)}, stop {r2(stop)}, liq {r2(liq)}, target {r2(target)}"
    notes.append(f"OPENED [perp {side} {lev}x] {s} {sg['setup']} at {r2(entry)}, stop {r2(stop)}, liq {r2(liq)}, risk ${pos['risk']:.2f}")
    return True
def main():
    t0 = time.time(); p = load(); t = now(); today = t.strftime("%Y-%m-%d"); notes = []
    if p["day"].get("date") != today: p["day"] = dict(date=today, entries=0, losses=0, meme_losses=0, realized=0)
    dv = derivs(notes)
    for c, d in dv.items(): notes.append(f"DERIVS {c}: funding {d['fr_1h']*100:+.4f}%/h ({d['fr_1h']*800:+.4f}%/8h), OI {d['oi']:,.0f} {d['oi_unit']} [{d['src']}]")
    # 1) manage open positions (stops, 50% at 2R, breakeven + trail, early cut, time stop)
    still = []
    for pos in p["open"]:
        try:
            if (manage_perp(p, pos, t, notes, dv) if pos.get("inst") == "perp" else manage(p, pos, t, notes)): still.append(pos)
        except Exception as e:
            still.append(pos); notes.append(f"{pos['coin']}: could not update ({e})")
    p["open"] = still
    try:   # real-money feasibility: every spot holding must be an online, tradable Coinbase US (Coinbase Exchange) USD pair
        live = {x["base_currency"]: x for x in get(f"{CB}/products") if x["quote_currency"] == "USD"}
        for o in p["open"]:
            if o.get("inst") == "perp": o["us_feasible"] = f"Coinbase Derivatives {o['pid']}"; continue
            x = live.get(o["coin"]); ok = bool(x) and x["status"] == "online" and not x.get("trading_disabled") and not x.get("cancel_only") and not x.get("post_only")
            o["us_feasible"] = f"Coinbase US spot {o['coin']}-USD" if ok else "FLAG: not tradable on Coinbase US spot"
            if not ok: notes.append(f"FEASIBILITY FLAG: {o['coin']} not tradable on Coinbase US spot (kept, not deleted)")
    except Exception as e: notes.append(f"feasibility check failed ({e})")
    sync_cash(p)
    def equity():
        eq = p["cash"]
        for o in p["open"]: eq += pos_value(o)
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
            if sl == "core" and s in PERP and not setup:
                lo24 = min(x["l"] for x in c[-25:-1])
                dn = last["c"] < e20 and last["c"] < e50 and e20 < e50
                bdn = last["c"] < lo24 and prev["c"] >= min(x["l"] for x in c[-26:-2])
                frr = prev["h"] >= e20p * 0.998 and last["c"] < e20 and last["c"] < prev["c"] and last["c"] < last["o"]
                if dn and (bdn or frr):
                    setup = "SHORT-BREAKDOWN" if bdn else "SHORT-FAILED-RALLY"; ctx["lo24"] = r2(lo24)
                    fail = None if vx >= 1.2 else f"signal-candle volume {vx:.2f}x < 1.2x"
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
        short = sg["setup"].startswith("SHORT")
        if not why and sl == "core" and s in PERP:
            why = perp_entry(p, sg, u, s, short, regime, half_all, dv, equity, t, rec, notes)
            if why is True: scan["signals"].append(rec); continue
        elif not why and short: why = "shorts only on BTC/ETH/SOL/XRP (US-regulated perps)"
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
            held = sum(notional(o) for o in p["open"] if o["coin"] == s)
            cap = min(CAP[sl], COIN_MAX * eq - held)
            qty = min(risk / (entry - stop), cap / entry, (scash * 0.999) / (entry * (1 + rate)))
            room = EXPO_MAX * eq - sum(notional(o) for o in p["open"]); rroom = OPEN_RISK_MAX * eq - sum(open_risk(o) for o in p["open"])
            qty = max(0.0, min(qty, room / entry, rroom / (entry - stop)))
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
    eqx = equity(); g = p.setdefault("risk", {})
    scan["derivs"] = [dict(coin=c, **d) for c, d in dv.items()]
    scan["exposure"] = dict(notional=round(sum(notional(o) for o in p["open"]), 2), cap=round(EXPO_MAX * eqx, 2), open_risk=round(sum(open_risk(o) for o in p["open"]), 2),
                            risk_cap=round(OPEN_RISK_MAX * eqx, 2), shorts=sum(o.get("side") == "short" for o in p["open"]), max_shorts=MAX_SHORTS, perp_pause_until=g.get("perp_pause_until"))
    for o in p["open"]: o.setdefault("side", "long"); o.setdefault("inst", "spot"); o.setdefault("lev", 1.0)
    scan["regime"] = regime; scan["blocks"] = blocks; scan["half_size"] = half_all; scan["meme_off"] = meme_off_why
    if blocks: notes.insert(1, "NO NEW ENTRIES: " + "; ".join(blocks))
    if not sigs: notes.append("No trade signals: no ranked coin had trend + fresh 24h-high breakout or EMA20 pullback bounce on the last closed 1h candle.")
    for w in scan["watch"]: notes.append(f"WATCH {w['sym']}: {w['why']}")
    for cc in scan["core"]: notes.append(f"{cc['sym']}: {'uptrend' if cc['trend'] else 'trend filter fails'}{', trigger' if cc['trigger'] else ', no trigger'} (close {cc['close']}, EMA20 {cc['ema20']}, EMA50 {cc['ema50']}, 24h high {cc['hi24']})")
    sync_cash(p)
    for k, v in p["sleeves"].items():
        mv = sum(pos_value(o) for o in p["open"] if o.get("sleeve", "core") == k)
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
            w.writerow([x["entry_time"][:10], x["coin"], f"{x.get('sleeve','core')}:{x.get('inst','spot')}:{x['setup']}", x.get("side", "long") + (f" {x['lev']}x" if x.get("inst") == "perp" else ""), x["thesis"], x["entry_time"], x["entry"], x["stop"], x["target"], round(x["qty"], 6), x["risk"],
                        x.get("exit_time", ""), x.get("exit", ""), x.get("exit_reason", "OPEN"), x.get("pnl", ""), x.get("R", ""), "Y", "", ""])
    print("\n".join(notes)); print(f"Equity ${eq:,.2f}  cash ${p['cash']:,.2f}  open {len(p['open'])}  ({scan['secs']}s)")
if __name__ == "__main__": main()
