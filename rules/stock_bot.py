#!/usr/bin/env python3
"""Stock PAPER day-trading bot. Simulated only: no real money, no broker, no API keys, no orders sent anywhere.
Joey approved 2026-10-20 in chat ("Trade both", "Agresivo"): paper stock bot alongside the crypto paper bot.
Account: separate $10,000 paper account in data/stock_portfolio.json (never reset; created only with BOT_INIT_OK=1).
Data: public Yahoo Finance chart API (query1.finance.yahoo.com/v8/finance/chart), 5m bars, regular hours only.
Every price used is stored with its source and timestamp. Stale data (>15 min old during market hours) = no new trade.
Session: NYSE regular hours 9:30-16:00 America/New_York on trading days (weekends + NYSE holidays skipped, early closes handled).
No new entries after 15:30 (12:30 on 1pm early-close days); everything flattened at 15:55 (12:55). Day trading only.
Strategies (each tagged on every trade, with its own daily caps; all share the account's risk limits):
  orb15_v1           15-min opening range breakout on closed 5m bars + trend filter (VWAP and prior close). Up to 4 entries/day.
  tjr_exact          faithful mechanical TJR 2026 model on SPY/QQQ (rules/stock_strategies.py). 1 trade/day, done after a loss.
  liquidity_reversal our TJR-inspired emulation on SPY/QQQ (rules/stock_strategies.py). 2 trades/day, done after a loss.
  If two strategies signal the same ticker in one run, the first in STRATEGY_ORDER wins and the other is logged as skipped.
AGGRESSIVE paper risk profile (matches crypto): 2% risk/trade, max 8% open risk, -$400 daily stop, no new trades at +$600/day,
half size after 2 account losses, stop after 3, max 3 open, per-strategy entry caps (ORB 4/day, TJR 1, LR 2), day-trade margin (Joey 2026-10-20 22:15Z): position notional <= 4x equity, total notional <= 4x equity (was 1x / 2x); flat by 15:55 so no overnight margin; no margin interest simulated intraday.
Fills: simulated at last price +/- 2 bps slippage, $0 commission. Shorts are simulated (no borrow fee / locate modeled)."""
import csv, json, math, os, sys, urllib.request, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_strategies as SS
from datetime import datetime, date, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PF = os.path.join(ROOT, "data", "stock_portfolio.json")
JOURNAL = os.path.join(ROOT, "data", "stock_journal.csv")
SIGNALS = os.path.join(ROOT, "data", "stock_signals.csv")   # every strategy signal, incl. skipped ones with the reason
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (paper-trading-dashboard stock paper bot; read-only public chart data)"}
SOURCE = "Yahoo Finance chart API v8 (query1.finance.yahoo.com), 5m bars (+pre/post for levels; 1m for SPY/QQQ TJR entries)"
VERSION = "stock_bot v2 (orb15_v1 + tjr_exact + liquidity_reversal)"

# ---- Universe: liquid US stocks/ETFs ----
UNIVERSE = ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD"]

# ---- AGGRESSIVE paper risk profile (Joey 2026-10-20 "Agresivo"; mirrors crypto AGGRESSIVE_PAPER_PROFILE) ----
START = 10000.0
RISK_PCT = 0.02            # 2% of equity per trade
HALF_AFTER_LOSSES = 2      # half size (1%) after 2 losing trades today
STOP_AFTER_LOSSES = 3      # no new trades after 3 losing trades today
OPEN_RISK_MAX = 0.08       # sum of open $ risk <= 8% of equity
DAILY_STOP = 400.0         # no new trades once day P&L <= -$400 (and open positions are flattened)
PROFIT_LOCK = 600.0        # no new trades once day P&L >= +$600
MAX_OPEN = 3
MAX_ENTRIES_DAY = 4       # orb15_v1 cap (account cap from the base spec); TJR/LR have their own caps below
# Day-trade margin approved by Joey 2026-10-20 22:15Z ("Yes, allow day-trade margin on the paper stock bot"), paper only, 1-week paper evaluation.
POS_NOTIONAL_MAX = 4.0     # each position <= 4x equity notional (was 1x)
TOTAL_NOTIONAL_MAX = 4.0   # all open positions <= 4x equity, Reg-T-style day-trade buying power (was 2x). No intraday margin interest.
SLIPPAGE_BPS = 2.0         # adverse slippage on every fill
COMMISSION = 0.0
STALE_SEC = 15 * 60        # data older than 15 min during market hours = no trade

# ---- Session calendar (NYSE, from nyse.com/trade/hours-calendars) ----
HOLIDAYS = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
            date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
            date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
            date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24)}
EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}  # 1:00 pm ET close
OPEN_T = dtime(9, 30)
CAL_MAX = date(2027, 12, 31)

def session(d):
    """(open_dt, close_dt, last_entry_dt, flatten_dt) in NY time, or None if not a trading day."""
    if d.weekday() >= 5 or d in HOLIDAYS: return None
    close_t = dtime(13, 0) if d in EARLY_CLOSE else dtime(16, 0)
    o = datetime.combine(d, OPEN_T, NY); c = datetime.combine(d, close_t, NY)
    return o, c, c - timedelta(minutes=30), c - timedelta(minutes=5)

def r2(x): return None if x is None else round(x, 2)
def iso(ts): return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---- Data ----
def is_reg(t):
    d = datetime.fromtimestamp(t, NY).date(); s = session(d)
    return bool(s) and s[0].timestamp() <= t < s[1].timestamp()

def fetch_chart(sym, rng="5d", interval="5m", prepost=True):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range={rng}&interval={interval}&includePrePost={'true' if prepost else 'false'}"
    r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read())["chart"]["result"][0]
    q = r["indicators"]["quote"][0]; bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c): continue
        bars.append(dict(t=t, o=o, h=h, l=l, c=c, v=v or 0, reg=is_reg(t)))
    m = r["meta"]
    return dict(meta=dict(price=m.get("regularMarketPrice"), time=m.get("regularMarketTime"), prev_close=m.get("previousClose") or m.get("chartPreviousClose"),
                          delayed_by=m.get("exchangeDataDelayedBy"), exchange=m.get("fullExchangeName") or m.get("exchangeName")), bars=bars, url=url)

def quote_info(d, now):
    """Price + provenance. Delay label: Yahoo's chart meta has no explicit delay field for US listings (exchangeDataDelayedBy is
    absent/0), so we label by measured lag of regularMarketTime vs now."""
    m = d["meta"]; lag = int(now.timestamp() - (m["time"] or 0))
    if m.get("delayed_by"): label = f"delayed {m['delayed_by']} min (Yahoo exchangeDataDelayedBy)"
    elif lag <= 120: label = f"near real-time (lag {lag}s)"
    else: label = f"possibly delayed (lag {lag // 60} min)"
    return dict(price=m["price"], source=SOURCE, as_of=iso(m["time"]) if m["time"] else None, lag_sec=lag, delay=label, stale=lag > STALE_SEC)

# ---- Indicators ----
def day_bars(bars, d):
    return [b for b in bars if datetime.fromtimestamp(b["t"], NY).date() == d]

def vwap(bars):
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars); v = sum(b["v"] for b in bars)
    return pv / v if v else None

def atr(bars, n=14):
    if len(bars) < 2: return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    trs = trs[-n:]; return sum(trs) / len(trs)

def prior_close(bars, d):
    prev = [b for b in bars if datetime.fromtimestamp(b["t"], NY).date() < d]
    return prev[-1]["c"] if prev else None

# ---- Strategies (modular: add e.g. a TJR-style strategy here and select it in ACTIVE_STRATEGY) ----
def orb15_v1(sym, bars, now, d):
    """15-min opening range breakout. Long: last CLOSED 5m bar closes above OR high, above session VWAP, above prior close.
    Short mirror. Stop = tighter of opposite OR side or 1x ATR(14, 5m) from entry (min 0.10% of price). Target 2R.
    Skips: OR width outside 0.15%-3% of price; price already > 1R past the OR edge (no chasing)."""
    bars = [b for b in bars if b.get("reg", True)]
    today = day_bars(bars, d)
    closed = [b for b in today if b["t"] + 300 <= now.timestamp()]
    o_ts = datetime.combine(d, OPEN_T, NY).timestamp()
    orb = [b for b in closed if b["t"] < o_ts + 900]
    if len(orb) < 3: return None, "opening range not complete (needs 3 closed 5m bars 9:30-9:45)"
    after = [b for b in closed if b["t"] >= o_ts + 900]
    if not after: return None, "no closed bar after 9:45 yet"
    hi, lo = max(b["h"] for b in orb), min(b["l"] for b in orb); last = after[-1]; px = last["c"]
    w = (hi - lo) / px
    if not 0.0015 <= w <= 0.03: return None, f"OR width {w*100:.2f}% outside 0.15-3%"
    vw, pc, a = vwap(closed), prior_close(bars, d), atr([b for b in bars if b["t"] + 300 <= now.timestamp()])
    if None in (vw, pc, a): return None, "missing VWAP / prior close / ATR"
    if px > hi and px > vw and px > pc: side, edge, other = "long", hi, lo
    elif px < lo and px < vw and px < pc: side, edge, other = "short", lo, hi
    else: return None, f"no breakout+trend (close {px:.2f}, OR {lo:.2f}-{hi:.2f}, VWAP {vw:.2f}, prior close {pc:.2f})"
    sd = 1 if side == "long" else -1
    dist = max(min(abs(px - other), a), px * 0.001)
    if abs(px - edge) > dist: return None, f"{side} breakout already > 1R past OR edge (no chase)"
    return dict(side=side, stop_dist=dist, signal_bar=iso(last["t"]),
                reason=f"ORB15 {side}: 5m close {px:.2f} {'>' if sd > 0 else '<'} OR {'high' if sd > 0 else 'low'} {edge:.2f} (OR {lo:.2f}-{hi:.2f}, width {w*100:.2f}%), "
                       f"{'above' if sd > 0 else 'below'} VWAP {vw:.2f} and prior close {pc:.2f}; stop dist {dist:.2f} = min(OR other side {abs(px-other):.2f}, ATR14 {a:.2f})"), None

def orb_wrap(ctx):
    sig, skips = [], []
    for s in UNIVERSE:
        if s not in ctx["data"]: continue
        x, why = orb15_v1(s, ctx["data"][s]["bars"], ctx["now_dt"], ctx["d"])
        if x: sig.append(dict(x, sym=s, key=f"orb:{s}:{x['side']}", tp1=None, target=None, trail=True, flat_by=None))
        else: ctx["status"][s] = why
    return sig, skips

STRATEGIES = {"tjr_exact": SS.tjr_exact, "liquidity_reversal": SS.liquidity_reversal, "orb15_v1": orb_wrap}
STRATEGY_ORDER = ["tjr_exact", "liquidity_reversal", "orb15_v1"]   # conflict rule: first signal on a ticker wins
STRAT_CAPS = {"orb15_v1": dict(entries=MAX_ENTRIES_DAY, loss_stop=None), "tjr_exact": dict(entries=1, loss_stop=1), "liquidity_reversal": dict(entries=2, loss_stop=1)}
ACTIVE_STRATEGY = "orb15_v1"   # kept for older records

# ---- Accounting (pure functions; tested in tests/test_stock_accounting.py) ----
# Model: cash changes ONLY by realized P&L (commission $0; slippage is inside fill prices). Equity = cash + unrealized P&L.
def fill_price(px, side, action):
    """action 'open' or 'close'. Buys pay up, sells receive less (adverse SLIPPAGE_BPS)."""
    buy = (side == "long") == (action == "open")
    return px * (1 + SLIPPAGE_BPS / 1e4) if buy else px * (1 - SLIPPAGE_BPS / 1e4)

def unrealized(pos, px):
    return (px - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "long" else -1)

def equity(p, prices):
    return p["cash"] + sum(unrealized(x, prices.get(x["sym"], x.get("last", x["entry"]))) for x in p["open"])

def size_position(eq, entry, stop_dist, risk_pct, open_notional, open_risk):
    """Shares limited by $ risk (2%), 8% open risk, POS_NOTIONAL_MAX x equity per position, TOTAL_NOTIONAL_MAX x equity in total."""
    risk_d = eq * risk_pct
    room_risk = max(0.0, eq * OPEN_RISK_MAX - open_risk)
    risk_d = min(risk_d, room_risk)
    q = math.floor(risk_d / stop_dist) if stop_dist > 0 else 0
    q = min(q, math.floor(eq * POS_NOTIONAL_MAX / entry), math.floor(max(0.0, eq * TOTAL_NOTIONAL_MAX - open_notional) / entry))
    return max(0, q)

def realize(p, pos, qty, px_fill, reason, when, info, log):
    sd = 1 if pos["side"] == "long" else -1
    pnl = round((px_fill - pos["entry"]) * qty * sd - COMMISSION, 2)
    p["cash"] = round(p["cash"] + pnl, 2); pos["qty"] -= qty; pos["realized"] = round(pos.get("realized", 0) + pnl, 2)
    p["day"]["realized"] = round(p["day"]["realized"] + pnl, 2)
    log.append(dict(time=when, sym=pos["sym"], action="PARTIAL" if pos["qty"] > 0 else "CLOSE", side=pos["side"], qty=qty, price=r2(px_fill),
                    entry=r2(pos["entry"]), stop=r2(pos["stop"]), target=r2(pos["target"]), pnl=pnl, reason=reason,
                    source=info.get("source", SOURCE), data_time=info.get("as_of"), delay=info.get("delay")))
    if pos["qty"] <= 0:
        tot = pos["realized"]; R = round(tot / pos["risk0"], 2) if pos.get("risk0") else None
        if tot < 0:
            p["day"]["losses"] += 1
            ds = p["day"].setdefault("strat", {}).setdefault(pos.get("strategy") or "orb15_v1", dict(entries=0, losses=0)); ds["losses"] += 1
        p["open"] = [x for x in p["open"] if x is not pos]
        p["closed"].append(dict(sym=pos["sym"], side=pos["side"], strategy=pos.get("strategy"), qty0=pos["qty0"], entry=r2(pos["entry"]),
                                stop0=r2(pos["stop0"]), target=r2(pos["target"]), opened=pos["opened"], closed=when, exit=r2(px_fill),
                                exit_reason=reason, pnl=tot, R=R, risk0=pos.get("risk0"), entry_reason=pos.get("reason"), entry_data=pos.get("entry_data"),
                                exit_data=dict(source=info.get("source", SOURCE), as_of=info.get("as_of"), delay=info.get("delay"))))
    return pnl

def manage(p, pos, bars, info, now, log, flatten=False, flatten_reason="EOD flatten (15:55 ET rule)"):
    """Walk 5m bars since last check: stop first (conservative), then 1R partial (half off, stop -> breakeven), then 2R target;
    after the partial the stop trails 1x ATR behind the best price. flatten=True closes at last price."""
    when = now.strftime("%Y-%m-%dT%H:%M:%SZ") if now.tzinfo == timezone.utc else now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sd = 1 if pos["side"] == "long" else -1; R = pos["risk_ps"]
    for b in [b for b in bars if b["t"] >= pos.get("seen", 0)]:
        lo_adv, hi_fav = (b["l"], b["h"]) if sd > 0 else (b["h"], b["l"])
        if (lo_adv - pos["stop"]) * sd <= 0:
            px = pos["stop"] if (b["o"] - pos["stop"]) * sd > 0 else b["o"]   # gap through stop fills at the open
            realize(p, pos, pos["qty"], fill_price(px, pos["side"], "close"), "stop" if not pos.get("half") else "stop (breakeven/trail)", when, info, log); return
        tp1 = pos.get("tp1") or pos["entry"] + sd * R
        if not pos.get("half") and (hi_fav - tp1) * sd >= 0:
            q = pos["qty"] // 2
            if q > 0: realize(p, pos, q, fill_price(tp1, pos["side"], "close"), "TP1 partial: half off" if pos.get("tp1") else "1R partial: half off", when, info, log)
            pos["half"] = True; pos["stop"] = pos["entry"]
        if pos.get("target") is not None and (hi_fav - pos["target"]) * sd >= 0:
            realize(p, pos, pos["qty"], fill_price(pos["target"], pos["side"], "close"), "2R target" if not pos.get("tp1") else "TP2 target", when, info, log); return
        pos["best"] = max(pos.get("best", pos["entry"]) * sd, hi_fav * sd) * sd
        if pos.get("half") and pos.get("atr") and pos.get("trail", True):
            trail = pos["best"] - sd * pos["atr"]
            if (trail - pos["stop"]) * sd > 0: pos["stop"] = trail
        pos["seen"] = b["t"] + 300
    px = info.get("price")
    if px and pos in p["open"] and not flatten and (px - pos["stop"]) * sd <= 0:   # live price already through the stop
        realize(p, pos, pos["qty"], fill_price(px, pos["side"], "close"), "stop (live price)", when, info, log); return
    if flatten and pos in p["open"]:
        realize(p, pos, pos["qty"], fill_price(px, pos["side"], "close"), flatten_reason, when, info, log)

# ---- Persistence ----
def new_account():
    return dict(account="stock_paper", start=START, cash=START, open=[], closed=[], day={}, created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                note="Simulated only. No broker, no real money. Never reset (NO-RESET RULE).")

def load():
    if not os.path.exists(PF):
        if os.environ.get("BOT_INIT_OK") != "1":
            raise SystemExit(f"REFUSING TO START: {PF} is missing. Will not create a new ${START:,.0f} stock paper account. Restore it from git history (or set BOT_INIT_OK=1 with Joey's one-time approval).")
        return new_account()
    p = json.load(open(PF))
    for k in ("cash", "open", "closed"):
        if k not in p: raise SystemExit(f"REFUSING TO START: {PF} has no '{k}'. Will not rebuild it. Restore from git.")
    return p

def save(p, force=True):
    new = json.dumps(p, indent=1, sort_keys=True)
    old = open(PF).read() if os.path.exists(PF) else ""
    def strip(s):
        try: d = json.loads(s); d.get("status", {}).pop("checked_at", None); d.get("status", {}).pop("ny_time", None); return json.dumps(d, sort_keys=True)
        except Exception: return s
    if force or strip(old) != strip(new):
        open(PF, "w").write(new); return True
    return False

def write_journal(log):
    if not log: return
    new = not os.path.exists(JOURNAL)
    with open(JOURNAL, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(log[0].keys()))
        if new: w.writeheader()
        for row in log: w.writerow(row)

def write_signals(p, rows):
    """Append strategy signals/skips once per key per day (data/stock_signals.csv)."""
    seen = set(p["day"].setdefault("logged", [])); new = [r for r in rows if r["key"] not in seen]
    if not new: return
    p["day"]["logged"] = sorted(seen | {r["key"] for r in new})
    hdr = not os.path.exists(SIGNALS)
    with open(SIGNALS, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "strategy", "sym", "event", "key", "reason"])
        if hdr: w.writeheader()
        for r in new: w.writerow({k: r.get(k) for k in ["time", "strategy", "sym", "event", "key", "reason"]})

def strat_stats(closed):
    out = {}
    for c in closed:
        k = c.get("strategy") or "orb15_v1"; o = out.setdefault(k, dict(trades=0, wins=0, pnl=0.0, sumR=0.0, gross_win=0.0, gross_loss=0.0))
        o["trades"] += 1; o["wins"] += c["pnl"] > 0; o["pnl"] = round(o["pnl"] + c["pnl"], 2); o["sumR"] += c.get("R") or 0
        if c["pnl"] > 0: o["gross_win"] += c["pnl"]
        else: o["gross_loss"] -= c["pnl"]
    for k, o in out.items():
        o["win_rate"] = round(o["wins"] / o["trades"], 3); o["avg_R"] = round(o.pop("sumR") / o["trades"], 3)
        gw, gl = o.pop("gross_win"), o.pop("gross_loss"); o["profit_factor"] = round(gw / gl, 2) if gl else None
    return out

RULES = dict(profile="AGGRESSIVE paper (Joey 2026-10-20 'Trade both' / 'Agresivo')", universe=UNIVERSE, strategies=STRATEGY_ORDER,
             strategy_caps=STRAT_CAPS, conflict_rule="one position per ticker; first strategy in order wins, the other is logged as skipped",
             risk_per_trade=RISK_PCT, half_size_after_losses=HALF_AFTER_LOSSES, stop_after_losses=STOP_AFTER_LOSSES, open_risk_max=OPEN_RISK_MAX,
             daily_stop=DAILY_STOP, profit_lock=PROFIT_LOCK, max_open=MAX_OPEN, max_entries_day=MAX_ENTRIES_DAY,
             position_notional_max=POS_NOTIONAL_MAX, total_notional_max=TOTAL_NOTIONAL_MAX, margin="day-trade buying power 4x equity, approved Joey 2026-10-20 22:15Z, paper only; flat by 15:55 (no overnight margin); no margin interest", slippage_bps=SLIPPAGE_BPS, commission=COMMISSION,
             min_stop_cost_mult=SS.MIN_STOP_MULT, stale_after_sec=STALE_SEC,
             session="NYSE 9:30-16:00 ET trading days; ORB: no entries after 15:30, flatten 15:55 (early close: 12:30 / 12:55); TJR/LR: entries by 10:10 / 10:30, flat 11:30",
             tjr_exact=SS.TJR, liquidity_reversal=SS.LR, data_source=SOURCE)

# ---- Main ----
def run(now=None, fetch=fetch_chart, fetch1=None, strategies=None):
    """fetch(sym) -> 5m chart (5d, with pre/post). fetch1(sym) -> 1m chart for SPY/QQQ (TJR entries); None in backtests (5m stand-in)."""
    now = now or datetime.now(timezone.utc); nyt = now.astimezone(NY); d = nyt.date()
    if fetch1 is None and fetch is fetch_chart: fetch1 = lambda s: fetch_chart(s, "1d", "1m", prepost=False)
    order = strategies or STRATEGY_ORDER
    p = load(); log = []; notes = []; sigrows = []
    if d > CAL_MAX: notes.append("WARNING: holiday calendar ends 2027-12-31; update HOLIDAYS")
    if p["day"].get("date") != d.isoformat():
        p["day"] = dict(date=d.isoformat(), realized=0.0, losses=0, entries=0, traded=[], start_eq=None, strat={}, logged=[])
    p["day"].setdefault("strat", {}); p["rules"] = RULES; p["version"] = VERSION
    sess = session(d); stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    st = dict(checked_at=stamp, ny_time=nyt.strftime("%Y-%m-%d %H:%M %Z"))
    if not sess or not (sess[0] <= nyt < sess[1]):
        why = "weekend" if d.weekday() >= 5 else "NYSE holiday" if d in HOLIDAYS else ("before 9:30 ET" if sess and nyt < sess[0] else "after the close")
        st.update(state="market_closed", message=f"Market closed ({why}). No trades. Open positions: {len(p['open'])}.")
        if p["open"]: st["message"] += " WARNING: positions open while market closed (should have been flattened at 15:55); they will be flattened at the next open."
        p["status"] = st; p["equity"] = r2(equity(p, {})); p["stats"] = strat_stats(p["closed"]); wrote = save(p, force=False)
        print(st["message"], "(state file", "updated)" if wrote else "unchanged)"); return p
    o, c, last_entry, flat_t = sess
    data, infos = {}, {}
    for s in sorted(set(UNIVERSE) | {x["sym"] for x in p["open"]}):
        try:
            data[s] = fetch(s); infos[s] = quote_info(data[s], now)
        except Exception as e:
            notes.append(f"{s}: data error {e}")
    if fetch1:
        for s in SS.PAIR:
            try:
                if s in data: data[s]["bars1"] = fetch1(s)["bars"]
            except Exception as e: notes.append(f"{s}: 1m data error {e}")
    prices = {s: i["price"] for s, i in infos.items() if i["price"]}
    if p["day"].get("start_eq") is None: p["day"]["start_eq"] = r2(equity(p, prices))
    # 1) manage / flatten open positions
    flatten_all = nyt >= flat_t
    for pos in list(p["open"]):
        s = pos["sym"]
        if s not in infos: notes.append(f"{s}: no data, cannot manage this run"); continue
        tflat = pos.get("flat_by") and now.timestamp() >= pos["flat_by"]
        if infos[s]["stale"] and not (flatten_all or tflat): notes.append(f"{s}: stale data ({infos[s]['delay']}), managing skipped"); continue
        manage(p, pos, [b for b in data[s]["bars"] if b["t"] + 300 <= now.timestamp() and b.get("reg", True)], infos[s], now, log,
               flatten=bool(flatten_all or tflat), flatten_reason="EOD flatten (15:55 ET rule)" if flatten_all else f"time exit ({pos.get('strategy')} flat by {datetime.fromtimestamp(pos['flat_by'], NY).strftime('%H:%M')} ET)" if tflat else "")
        if pos in p["open"]: pos["last"] = infos[s]["price"]; pos["last_data"] = dict(source=SOURCE, as_of=infos[s]["as_of"], delay=infos[s]["delay"])
    eq = equity(p, prices); day_pnl = round(eq - p["day"]["start_eq"], 2)
    if day_pnl <= -DAILY_STOP and p["open"]:
        for pos in list(p["open"]):
            if pos["sym"] in infos: manage(p, pos, [], infos[pos["sym"]], now, log, flatten=True, flatten_reason=f"daily loss stop (day P&L {day_pnl:+.2f} <= -${DAILY_STOP:.0f}): flatten")
        eq = equity(p, prices); day_pnl = round(eq - p["day"]["start_eq"], 2)
    # 2) account-level entry gates (shared by all strategies)
    blocks = []
    if flatten_all: blocks.append("after 15:55 flatten time")
    if day_pnl <= -DAILY_STOP: blocks.append(f"daily loss stop hit ({day_pnl:+.2f})")
    if day_pnl >= PROFIT_LOCK: blocks.append(f"profit lock hit ({day_pnl:+.2f} >= +${PROFIT_LOCK:.0f})")
    if p["day"]["losses"] >= STOP_AFTER_LOSSES: blocks.append(f"{p['day']['losses']} losses today (stop after {STOP_AFTER_LOSSES})")
    risk_pct = RISK_PCT / 2 if p["day"]["losses"] >= HALF_AFTER_LOSSES else RISK_PCT
    signals = {}; ctx_status = {}
    ctx = dict(now=now.timestamp(), now_dt=now, d=d, data={s: v for s, v in data.items() if not infos[s]["stale"]}, status=ctx_status)
    for s, i in infos.items():
        if i["stale"]: signals[s] = f"stale data ({i['delay']}): no trade"
    taken = {}
    for name in ([] if blocks else order):
        cap = STRAT_CAPS[name]; ds = p["day"]["strat"].setdefault(name, dict(entries=0, losses=0))
        try: sigs, skips = STRATEGIES[name](ctx)
        except Exception as e: notes.append(f"{name}: strategy error {e!r}"); continue
        for k in skips: sigrows.append(dict(time=stamp, strategy=name, sym=k["sym"], event="SKIP", key=k["key"], reason=k["reason"]))
        for sg in sigs:
            s = sg["sym"]; key = sg["key"]
            def skip(why):
                sigrows.append(dict(time=stamp, strategy=name, sym=s, event="SKIP", key=key + ":" + why[:24], reason=f"signal skipped: {why} | {sg['reason']}")); signals[s] = f"{name}: skipped ({why})"
            if name == "orb15_v1" and nyt >= last_entry: skip("no new ORB entries after 15:30 ET"); continue
            if ds["entries"] >= cap["entries"]: skip(f"{name} daily cap {cap['entries']} reached"); continue
            if cap["loss_stop"] and ds["losses"] >= cap["loss_stop"]: skip(f"{name} done for the day after {ds['losses']} loss"); continue
            if s in taken: skip(f"conflict: {s} already taken this run by {taken[s]}"); continue
            if any(x["sym"] == s for x in p["open"]): skip(f"{s} already has an open position"); continue
            if f"{s}:{sg['side']}" in p["day"]["traded"] and name == "orb15_v1": skip(f"already traded {sg['side']} today"); continue
            if key in p["day"]["traded"]: continue
            if len(p["open"]) >= MAX_OPEN: skip(f"max {MAX_OPEN} open positions"); continue
            info = infos[s]; px = fill_price(info["price"], sg["side"], "open"); sd = 1 if sg["side"] == "long" else -1
            stop = sg["stop"] if "stop" in sg else px - sd * sg["stop_dist"]
            if (px - stop) * sd <= 0: skip(f"price {px:.2f} already through stop {stop:.2f}"); continue
            if "stop" in sg and not SS.cost_ok(px, stop): skip("stop < 3x round-trip cost at fill"); continue
            risk_ps = abs(px - stop)
            if sg.get("tp1") is not None and (sg["tp1"] - px) * sd <= 0: skip("price already past TP1"); continue
            o_not = sum(x["entry"] * x["qty"] for x in p["open"]); o_risk = sum(abs(x["entry"] - x["stop"]) * x["qty"] for x in p["open"] if (x["entry"] - x["stop"]) * (1 if x["side"] == "long" else -1) > 0)
            q = size_position(eq, px, risk_ps, risk_pct, o_not, o_risk)
            if q < 1: skip("size 0 after risk/notional caps"); continue
            target = sg["target"] if sg.get("tp1") is not None else (None if name != "orb15_v1" else px + sd * 2 * risk_ps)
            if name == "orb15_v1": target = px + sd * 2 * risk_ps
            pos = dict(sym=s, side=sg["side"], strategy=name, qty=q, qty0=q, entry=px, stop=stop, stop0=stop, risk_ps=risk_ps, tp1=sg.get("tp1"),
                       target=target, trail=sg.get("trail", True), flat_by=sg.get("flat_by"), risk0=round(risk_ps * q, 2), atr=atr([b for b in data[s]["bars"] if b.get("reg", True)]),
                       best=px, seen=int(now.timestamp()) // 300 * 300 + 300,  # only bars that start after entry
                       opened=stamp, reason=sg["reason"] + (" [HALF SIZE: 2+ losses today]" if risk_pct < RISK_PCT else ""),
                       entry_data=dict(source=info["source"], as_of=info["as_of"], delay=info["delay"], signal_bar=sg.get("signal_bar")), last=info["price"])
            p["open"].append(pos); p["day"]["entries"] += 1; ds["entries"] += 1; taken[s] = name
            p["day"]["traded"] += [f"{s}:{sg['side']}", key]
            log.append(dict(time=stamp, sym=s, action="OPEN", side=pos["side"], qty=q, price=r2(px), entry=r2(px), stop=r2(stop), target=r2(target) if target else "",
                            pnl=0.0, reason=f"[{name}] " + pos["reason"], source=info["source"], data_time=info["as_of"], delay=info["delay"]))
            sigrows.append(dict(time=stamp, strategy=name, sym=s, event="ENTER", key=key, reason=pos["reason"]))
            signals[s] = f"{name}: ENTERED {sg['side']} {q} @ {px:.2f}"
            eq = equity(p, prices)
    for s, why in ctx_status.items(): signals.setdefault(s, why)
    if nyt >= last_entry and not flatten_all: blocks.append("no new ORB entries after 15:30 ET")
    eq = equity(p, prices)
    p["equity"] = r2(eq); p["stats"] = strat_stats(p["closed"])
    st.update(state="market_open", message=("Blocked: " + "; ".join(blocks)) if blocks else f"Scanned {len(UNIVERSE)} symbols with {', '.join(order)}.",
              day_pnl=r2(eq - p["day"]["start_eq"]), risk_pct_now=risk_pct, blocks=blocks, signals=signals, notes=notes,
              data={s: dict(price=r2(i["price"]), as_of=i["as_of"], delay=i["delay"], stale=i["stale"]) for s, i in infos.items()})
    p["status"] = st
    for r in log: r.setdefault("strategy", "")
    write_signals(p, sigrows); save(p); write_journal(log)
    print(st["message"], "| equity", r2(eq), "| actions", len(log), "| signals logged", len(sigrows))
    return p

if __name__ == "__main__":
    run()
