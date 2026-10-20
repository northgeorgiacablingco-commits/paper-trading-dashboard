"""Strategies for the stock PAPER bot (rules/stock_bot.py). Simulated only.
Every strategy is a pure function of (closed bars, time): run(ctx) -> (signals, skips). Same code runs live and in the backtest.
  signal = dict(sym, side, stop|stop_dist, tp1, target, trail, flat_by, reason, key, signal_bar)
  skip   = dict(sym, key, reason)   (logged once per key)
Bars: dict(t, o, h, l, c, v, reg) where reg=True for NYSE regular-session bars. 5m bars include pre/post (Yahoo includePrePost=true).

Two strategies built from TJR research (/workspace/trading/research/tjr.md), approved by Joey 2026-10-20:
  tjr_exact          - faithful mechanical version of the TJR 2026 four-step model (section 3 draft spec), on SPY/QQQ.
  liquidity_reversal - our own emulation of the principles that plausibly drive his wins (open window, sweep+reject,
                       index agreement, pullback entry, nearby targets, strict daily stop), 5m only so costs matter less.
Independent backtests (Telonics, Stockholm thesis) found little or no edge in exact TJR rules: these are experiments."""
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
NY = ZoneInfo("America/New_York")
PAIR = ("SPY", "QQQ")
ROUND_TRIP_BPS = 4.0          # 2 bps slippage per side; mirrors stock_bot.SLIPPAGE_BPS
MIN_STOP_MULT = 3.0           # skip if stop distance < 3x round-trip cost (PEPE fee lesson)

def at(d, hh, mm): return datetime.combine(d, dtime(hh, mm), NY).timestamp()
def nyhm(ts): return datetime.fromtimestamp(ts, NY).strftime("%H:%M")
def ndate(ts): return datetime.fromtimestamp(ts, NY).date()

def atr(bars, n=14):
    if len(bars) < 2: return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])][-n:]
    return sum(trs) / len(trs)

def swings(bars, k=2):
    """Fractal pivots. Returns lists of (pivot_idx, price) for highs and lows; a pivot at i is only known after bar i+k closes."""
    hs, ls = [], []
    for i in range(k, len(bars) - k):
        w = bars[i - k:i + k + 1]
        if bars[i]["h"] == max(b["h"] for b in w) and all(bars[i]["h"] > b["h"] for j, b in enumerate(w) if j != k): hs.append((i, bars[i]["h"]))
        if bars[i]["l"] == min(b["l"] for b in w) and all(bars[i]["l"] < b["l"] for j, b in enumerate(w) if j != k): ls.append((i, bars[i]["l"]))
    return hs, ls

def known(piv, i, k=2):
    """Pivots confirmed by the close of bar i."""
    return [p for p in piv if p[0] + k <= i]

def bos_at(bars, i, hs, ls, k=2):
    """'down' if bar i closes below the most recent confirmed swing low, 'up' if above the most recent swing high, else None."""
    kh, kl = known(hs, i, k), known(ls, i, k)
    if kl and bars[i]["c"] < kl[-1][1] and (i == 0 or bars[i - 1]["c"] >= kl[-1][1]): return "down"
    if kh and bars[i]["c"] > kh[-1][1] and (i == 0 or bars[i - 1]["c"] <= kh[-1][1]): return "up"
    return None

def last_bos_dir(bars, k=2):
    hs, ls = swings(bars, k); d = None
    for i in range(len(bars)):
        x = bos_at(bars, i, hs, ls, k)
        if x: d = x
    return d

def ifvg_at(bars, i, side, atr5):
    """Inverse FVG: bar i closes through the far edge of the most recent same-direction FVG (bull FVG for a short)."""
    for j in range(i - 1, 1, -1):
        a, c = bars[j - 2], bars[j]
        if side == "short" and c["l"] > a["h"] and c["l"] - a["h"] >= 0.1 * atr5:
            bot = a["h"]; return bars[i]["c"] < bot and all(b["c"] >= bot for b in bars[j + 1:i])
        if side == "long" and c["h"] < a["l"] and a["l"] - c["h"] >= 0.1 * atr5:
            top = a["l"]; return bars[i]["c"] > top and all(b["c"] <= top for b in bars[j + 1:i])
    return False

# ---------- liquidity levels ----------
def base_levels(bars, d):
    """PDH/PDL (prior regular session), ONH/ONL (prior post-market 16:00-20:00 + today's premarket 04:00-09:30, Yahoo pre/post)."""
    o = at(d, 9, 30); prev = [b for b in bars if b["reg"] and ndate(b["t"]) < d]
    out = []
    if prev:
        pd = ndate(prev[-1]["t"]); pdb = [b for b in prev if ndate(b["t"]) == pd]
        out += [("PDH", max(b["h"] for b in pdb), "high", pdb[-1]["t"] + 300), ("PDL", min(b["l"] for b in pdb), "low", pdb[-1]["t"] + 300)]
        on = [b for b in bars if not b["reg"] and pdb[-1]["t"] < b["t"] < o]
        if on: out += [("ONH", max(b["h"] for b in on), "high", o), ("ONL", min(b["l"] for b in on), "low", o)]
    return out

def hourly_levels(bars, d):
    """TJR draws: highs/lows of completed 1h bars in the last 24h and 4h bars in the last 48h (pre/post included)."""
    o = at(d, 9, 30); out = []
    for hrs, span, tag in ((1, 24, "1h"), (4, 48, "4h")):
        bk = {}
        for b in bars:
            if o - span * 3600 <= b["t"] < o:
                dt = datetime.fromtimestamp(b["t"], NY); key = (dt.date(), dt.hour // hrs)
                bk.setdefault(key, []).append(b)
        for key, bb in bk.items():
            end = datetime.combine(key[0], dtime(key[1] * hrs), NY).timestamp() + hrs * 3600
            if end > o: continue
            out += [(f"{tag}H {nyhm(bb[0]['t'])}", max(b["h"] for b in bb), "high", end), (f"{tag}L {nyhm(bb[0]['t'])}", min(b["l"] for b in bb), "low", end)]
    return out

def unswept(levels, bars, upto):
    """Keep levels not traded through between their formation and `upto`."""
    out = []
    for name, px, kind, formed in levels:
        later = [b for b in bars if formed <= b["t"] and b["t"] + 300 <= upto]
        if kind == "high" and all(b["h"] <= px for b in later): out.append((name, px, kind, formed))
        if kind == "low" and all(b["l"] >= px for b in later): out.append((name, px, kind, formed))
    return out

def cost_ok(entry, stop):
    return abs(entry - stop) >= MIN_STOP_MULT * ROUND_TRIP_BPS / 1e4 * entry

# ---------- tjr_exact ----------
TJR = dict(sweep=("09:30", "09:50"), entry=("09:50", "10:10"), no_new_after="10:30", flat="11:30", max_trades=1, stop_after_losses=1,
           tp1_min_R=1.5, sweep_buf_atr=0.05, k=2)

def tjr_exact(ctx):
    """Short shown, long mirrored. Steps (research/tjr.md section 3):
    1 SWEEP 09:30-09:50 (5m bar start): either index trades beyond an unswept draw (PDH/PDL, ONH/ONL, 1h/4h highs/lows) by >= 0.05*ATR14(5m).
    2 REVERSAL by 10:10 (5m bar close): 5m BOS (close through most recent confirmed swing, k=2) or IFVG close, on the index traded.
    3 SMT GATE at the reversal close: both SPY and QQQ last 5m BOS in the trade direction, else no trade.
    4 RETRACE: 1m BOS against the trade OR price retraces >= 50% of the reversal leg.
    5 ENTRY: first 1m BOS in the trade direction after the retrace, bar closing 09:50-10:10 ET (nothing after 10:30).
    STOP: beyond the 2nd most recent confirmed 1m swing (+$0.01), capped at sweep extreme + buffer. Skip if < 3x round-trip cost.
    TARGETS: TP1 (50%) = nearest unswept draw >= 1.5R away, then stop to breakeven; TP2 = next draw beyond (none -> flat 11:30).
    1 trade/day, done after the first loss. Flat by 11:30."""
    now, d, D = ctx["now"], ctx["d"], ctx["data"]; sig, skips = [], []
    if not all(s in D for s in PAIR): return sig, [dict(sym="SPY/QQQ", key="tjr:nodata", reason="SPY/QQQ data missing")]
    o = at(d, 9, 30)
    info = {}
    for s in PAIR:
        b5 = [b for b in D[s]["bars"] if b["t"] + 300 <= now]
        reg5 = [b for b in b5 if b["reg"] and ndate(b["t"]) == d]; day5 = [b for b in b5 if ndate(b["t"]) == d]
        src, blen = (D[s]["bars1"], 60) if D[s].get("bars1") else (D[s]["bars"], 300)   # live: 1m bars; backtest: 5m stand-in (no 60d 1m history)
        b1 = [dict(b, len=blen) for b in src if b["t"] + blen <= now and ndate(b["t"]) == d and b["reg"]]
        a5 = atr([b for b in b5 if b["reg"] and b["t"] < o])   # ATR frozen at 09:30 so the day's levels/buffers never shift later
        lv = unswept(base_levels(b5, d) + hourly_levels(b5, d), b5, o)
        info[s] = dict(reg5=reg5, day5=day5, b1=b1, atr=a5, levels=lv)
    for X in PAIR:
        Y = PAIR[1] if X == PAIR[0] else PAIR[0]; I = info[X]
        if not I["atr"] or not I["reg5"]: continue
        for side in ("short", "long"):
            sd = -1 if side == "short" else 1; key0 = f"tjr:{X}:{side}"
            # 1 sweep on either index
            sweep = None
            for s in PAIR:
                for b in info[s]["reg5"]:
                    if not (o <= b["t"] < at(d, 9, 50)) or not info[s]["atr"]: continue
                    buf = TJR["sweep_buf_atr"] * info[s]["atr"]
                    hit = [l for l in info[s]["levels"] if (side == "short" and l[2] == "high" and b["h"] >= l[1] + buf) or (side == "long" and l[2] == "low" and b["l"] <= l[1] - buf)]
                    if hit and (sweep is None or b["t"] < sweep[0]): sweep = (b["t"], s, hit[0][0], hit[0][1])
                    if hit: break
            if not sweep: continue
            # 2 reversal on X (5m, from sweep bar, closing by 10:10)
            bars = I["day5"]; hs, ls = swings(bars, TJR["k"]); rev = None
            for i, b in enumerate(bars):
                if b["t"] < sweep[0] or not b["reg"]: continue
                if b["t"] + 300 > at(d, 10, 10): break
                if bos_at(bars, i, hs, ls) == ("down" if sd < 0 else "up") or ifvg_at(bars, i, side, I["atr"]): rev = i; break
            if rev is None:
                if now >= at(d, 10, 10): skips.append(dict(sym=X, key=key0 + ":norev", reason=f"TJR {side}: {sweep[1]} swept {sweep[2]} {sweep[3]:.2f} at {nyhm(sweep[0])} but no 5m BOS/IFVG on {X} by 10:10"))
                continue
            rb = bars[rev]; rt = rb["t"] + 300
            ext = (max if sd < 0 else min)((b["h"] if sd < 0 else b["l"]) for b in bars[:rev + 1] if b["t"] >= sweep[0])
            # 3 SMT gate
            dX = last_bos_dir([b for b in bars if b["t"] + 300 <= rt]); dY = last_bos_dir([b for b in info[Y]["day5"] if b["t"] + 300 <= rt])
            want = "down" if sd < 0 else "up"
            if dX != want or dY != want:
                skips.append(dict(sym=X, key=key0 + ":smt", reason=f"TJR {side}: reversal {nyhm(rb['t'])} on {X} but indices not aligned ({X} {dX}, {Y} {dY})")); continue
            # 4 retrace + 5 entry on 1m (backtest falls back to 5m bars)
            if rt >= at(d, 10, 10):
                skips.append(dict(sym=X, key=key0 + ":late", reason=f"TJR {side}: aligned reversal on {X} closed {nyhm(rt)}, too late for the 09:50-10:10 entry window")); continue
            b1 = [b for b in I["b1"] if b["t"] >= rt]; full1 = I["b1"]
            if not b1: continue
            h1, l1 = swings(full1, TJR["k"]); off = len(full1) - len(b1)
            leg_lo = (min if sd < 0 else max)((b["l"] if sd < 0 else b["h"]) for b in bars[:rev + 1] if b["t"] >= sweep[0])
            half = leg_lo + (ext - leg_lo) * 0.5
            retr = None
            for j, b in enumerate(b1):
                i = j + off
                if bos_at(full1, i, h1, l1) == ("up" if sd < 0 else "down") or (b["h"] >= half if sd < 0 else b["l"] <= half): retr = j; break
            if retr is None:
                if now >= at(d, 10, 10): skips.append(dict(sym=X, key=key0 + ":noretrace", reason=f"TJR {side}: aligned reversal {nyhm(rb['t'])} on {X} but no retrace (1m BOS against / 50% of leg) before 10:10"))
                continue
            ent = None
            for j in range(retr + 1, len(b1)):
                i = j + off; ct = b1[j]["t"] + b1[j]["len"]
                if ct > at(d, 10, 10): break
                if bos_at(full1, i, h1, l1) == want and ct >= at(d, 9, 50): ent = (j, i, ct); break
            if ent is None:
                if now >= at(d, 10, 10): skips.append(dict(sym=X, key=key0 + ":noentry", reason=f"TJR {side}: aligned reversal {nyhm(rb['t'])} on {X} but no 1m entry BOS 09:50-10:10"))
                continue
            j, i, ct = ent; eb = b1[j]; key = f"{key0}:{eb['t']}"
            if now - ct > 360 or now > at(d, 10, 30): skips.append(dict(sym=X, key=key, reason=f"TJR {side}: entry bar {nyhm(eb['t'])} already stale at run time")); continue
            piv = [p for p in known(h1 if sd < 0 else l1, i)]
            if len(piv) < 2: skips.append(dict(sym=X, key=key, reason=f"TJR {side}: fewer than 2 confirmed 1m swings for the stop")); continue
            stop = piv[-2][1] - sd * 0.01
            cap = ext - sd * TJR["sweep_buf_atr"] * I["atr"]
            stop = min(stop, cap) if sd < 0 else max(stop, cap)
            entry = eb["c"]
            if (entry - stop) * sd <= 0: skips.append(dict(sym=X, key=key, reason=f"TJR {side}: stop {stop:.2f} on wrong side of entry {entry:.2f}")); continue
            if not cost_ok(entry, stop): skips.append(dict(sym=X, key=key, reason=f"TJR {side}: stop {abs(entry-stop):.2f} < 3x round-trip cost ({MIN_STOP_MULT*ROUND_TRIP_BPS/1e4*entry:.2f})")); continue
            R = abs(entry - stop)
            draws = sorted([l for l in unswept(I["levels"], [b for b in bars if b["t"] >= o], now) if l[2] == ("low" if sd < 0 else "high") and (l[1] - entry) * sd > 0],
                           key=lambda l: abs(l[1] - entry))
            t1 = [l for l in draws if abs(l[1] - entry) >= TJR["tp1_min_R"] * R]
            if not t1: skips.append(dict(sym=X, key=key, reason=f"TJR {side}: no unswept draw >= 1.5R (R {R:.2f}; draws {[f'{l[0]} {l[1]:.2f}' for l in draws[:3]]})")); continue
            tp2 = [l for l in draws if abs(l[1] - entry) > abs(t1[0][1] - entry)]
            sig.append(dict(sym=X, side=side, stop=stop, tp1=t1[0][1], target=tp2[0][1] if tp2 else None, trail=False, flat_by=at(d, 11, 30), key=key,
                            signal_bar=eb["t"], reason=f"TJR {side}: {sweep[1]} swept {sweep[2]} {sweep[3]:.2f} at {nyhm(sweep[0])}; 5m reversal {nyhm(rb['t'])} ({X} {dX}/{Y} {dY} aligned); "
                            f"retrace then 1m BOS {nyhm(eb['t'])}; stop {stop:.2f} (2nd 1m swing, cap sweep ext {ext:.2f}); TP1 {t1[0][0]} {t1[0][1]:.2f}"
                            + (f", TP2 {tp2[0][0]} {tp2[0][1]:.2f}" if tp2 else ", no TP2 (flat 11:30)")))
    return sig, skips

# ---------- liquidity_reversal (our emulation) ----------
LR = dict(window=("09:30", "10:30"), flat="11:30", max_trades=2, stop_after_losses=1, sweep_buf_atr=0.05, reject_bars=2, pullback_bars=3,
          pullback_frac=0.382, stop_buf_atr=0.05, tp1_R=1.5)

def liquidity_reversal(ctx):
    """Our rules (5m bars only, SPY/QQQ):
    LEVELS: PDH/PDL and overnight high/low (prior post + premarket via Yahoo pre/post), unswept as of 09:30. Obvious levels only.
    SWEEP: a 5m bar starting 09:30-10:30 trades beyond a level by >= 0.05*ATR14(5m).
    REJECT: that bar or one of the next 2 closes back inside the level (failed breakout).
    AGREE: at the rejection bar, SPY and QQQ 5m candles both point in the trade direction (close vs open).
    PULLBACK ENTRY: within the next 3 bars, a bar retraces >= 38.2% of the move from sweep extreme to the rejection close and then
      closes back in the trade direction (close vs open). Enter at that close. Never the first candle of the session.
    STOP: sweep extreme +/- 0.05*ATR. Skip if < 3x round-trip cost.
    TP1 (50%): nearest opposing level (PDL/PDH, ONL/ONH, today's session low/high so far) if it is 1R-1.5R away, else 1.5R.
      Then stop to breakeven, trail 1x ATR(5m). Flat 11:30. Max 2 trades/day; done after the first loss.
    Why: keeps the parts that plausibly carry his edge (open liquidity, failed breakouts of obvious levels, index agreement, not chasing,
      quick partials, hard daily stop) and drops the 1m discretion that independent tests could not make profitable after costs."""
    now, d, D = ctx["now"], ctx["d"], ctx["data"]; sig, skips = [], []
    if not all(s in D for s in PAIR): return sig, [dict(sym="SPY/QQQ", key="lr:nodata", reason="SPY/QQQ data missing")]
    o = at(d, 9, 30); P = {}
    for s in PAIR:
        b5 = [b for b in D[s]["bars"] if b["t"] + 300 <= now]
        P[s] = dict(b5=b5, reg=[b for b in b5 if b["reg"] and ndate(b["t"]) == d], atr=atr([b for b in b5 if b["reg"] and b["t"] < o]), lv=unswept(base_levels(b5, d), b5, o))
    for X in PAIR:
        Y = PAIR[1] if X == PAIR[0] else PAIR[0]; I = P[X]; bars = I["reg"]
        if not I["atr"] or len(bars) < 2: continue
        ybars = {b["t"]: b for b in P[Y]["reg"]}; buf = LR["sweep_buf_atr"] * I["atr"]
        for si, sb in enumerate(bars):
            if not (o <= sb["t"] < at(d, 10, 30)): continue
            for name, lvl, kind, _ in I["lv"]:
                side = "short" if kind == "high" else "long"; sd = -1 if side == "short" else 1
                if not ((kind == "high" and sb["h"] >= lvl + buf) or (kind == "low" and sb["l"] <= lvl - buf)): continue
                prior = bars[:si]
                if any((b["h"] >= lvl + buf) if kind == "high" else (b["l"] <= lvl - buf) for b in prior): continue   # only the first sweep of a level
                key0 = f"lr:{X}:{name}:{sb['t']}"
                rj = None
                for ri in range(si, min(si + 1 + LR["reject_bars"], len(bars))):
                    if (bars[ri]["c"] < lvl) if kind == "high" else (bars[ri]["c"] > lvl): rj = ri; break
                if rj is None:
                    if si + LR["reject_bars"] < len(bars): skips.append(dict(sym=X, key=key0 + ":accept", reason=f"LR {side}: {X} swept {name} {lvl:.2f} at {nyhm(sb['t'])} but held beyond it (no rejection close)"))
                    continue
                rb = bars[rj]; ext = (max if sd < 0 else min)((b["h"] if sd < 0 else b["l"]) for b in bars[si:rj + 1])
                yb = ybars.get(rb["t"])
                if not yb or (rb["c"] - rb["o"]) * sd <= 0 or (yb["c"] - yb["o"]) * sd <= 0:
                    skips.append(dict(sym=X, key=key0 + ":agree", reason=f"LR {side}: {X} rejected {name} at {nyhm(rb['t'])} but {X}/{Y} 5m candles disagree")); continue
                ent = None
                for ei in range(rj + 1, min(rj + 1 + LR["pullback_bars"], len(bars))):
                    eb = bars[ei]; thr = rb["c"] - sd * LR["pullback_frac"] * abs(ext - rb["c"])
                    if (eb["h"] if sd < 0 else -eb["l"]) >= (ext if sd < 0 else -ext): break     # pullback ran through the sweep extreme: invalid
                    if ((eb["h"] >= thr) if sd < 0 else (eb["l"] <= thr)) and (eb["c"] - eb["o"]) * sd > 0 and eb["t"] + 300 <= at(d, 10, 30):
                        ent = ei; break
                if ent is None:
                    if rj + LR["pullback_bars"] < len(bars): skips.append(dict(sym=X, key=key0 + ":nopb", reason=f"LR {side}: {X} rejected {name} at {nyhm(rb['t'])}, no valid pullback entry in 3 bars"))
                    continue
                eb = bars[ent]; key = f"{key0}:{eb['t']}"
                if now - (eb["t"] + 300) > 360: skips.append(dict(sym=X, key=key, reason=f"LR {side}: entry bar {nyhm(eb['t'])} stale at run time")); continue
                entry = eb["c"]; stop = ext - sd * LR["stop_buf_atr"] * I["atr"]; R = abs(entry - stop)
                if (entry - stop) * sd <= 0: skips.append(dict(sym=X, key=key, reason=f"LR {side}: stop wrong side")); continue
                if not cost_ok(entry, stop): skips.append(dict(sym=X, key=key, reason=f"LR {side}: stop {R:.2f} < 3x round-trip cost ({MIN_STOP_MULT*ROUND_TRIP_BPS/1e4*entry:.2f})")); continue
                opp = [l[1] for l in I["lv"] if l[2] == ("low" if sd < 0 else "high")]
                sess = (min if sd < 0 else max)((b["l"] if sd < 0 else b["h"]) for b in bars[:ent + 1])
                opp = sorted([x for x in opp + [sess] if (x - entry) * sd > 0], key=lambda x: abs(x - entry))
                near = [x for x in opp if R <= abs(x - entry) <= LR["tp1_R"] * R]
                tp1 = near[0] if near else entry + sd * LR["tp1_R"] * R
                sig.append(dict(sym=X, side=side, stop=stop, tp1=tp1, target=None, trail=True, flat_by=at(d, 11, 30), key=key, signal_bar=eb["t"],
                                reason=f"LR {side}: {X} swept {name} {lvl:.2f} at {nyhm(sb['t'])} (ext {ext:.2f}), rejected {nyhm(rb['t'])}, {X}+{Y} agree; "
                                       f"pullback entry {nyhm(eb['t'])}; stop {stop:.2f}; TP1 {tp1:.2f} ({'opposing level' if near else '1.5R'}), then BE + 1xATR trail"))
    return sig, skips
