#!/usr/bin/env python3
"""Stock PAPER day-trading bot. Simulated only: no real money, no broker, no API keys, no orders sent anywhere.
Joey approved 2026-10-20 in chat ("Trade both", "Agresivo"): paper stock bot alongside the crypto paper bot.
Account: separate $10,000 paper account in data/stock_portfolio.json (never reset; created only with BOT_INIT_OK=1).
Data: public Yahoo Finance chart API (query1.finance.yahoo.com/v8/finance/chart), 5m bars, regular hours only.
Every price used is stored with its source and timestamp. Stale data (>15 min old during market hours) = no new trade.
Session: NYSE regular hours 9:30-16:00 America/New_York on trading days (weekends + NYSE holidays skipped, early closes handled).
No new entries after 15:30 (12:30 on 1pm early-close days); everything flattened at 15:55 (12:55). Day trading only.
Strategy v1 (orb15_v1): 15-min opening range breakout on closed 5m bars + trend filter (VWAP and prior close).
AGGRESSIVE paper risk profile (matches crypto): 2% risk/trade, max 8% open risk, -$400 daily stop, no new trades at +$600/day,
half size after 2 losses, stop after 3, max 3 open, max 4 entries/day, 1x notional per position, total notional <= 2x equity.
Fills: simulated at last price +/- 2 bps slippage, $0 commission. Shorts are simulated (no borrow fee / locate modeled)."""
import csv, json, math, os, sys, urllib.request, urllib.parse
from datetime import datetime, date, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PF = os.path.join(ROOT, "data", "stock_portfolio.json")
JOURNAL = os.path.join(ROOT, "data", "stock_journal.csv")
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (paper-trading-dashboard stock paper bot; read-only public chart data)"}
SOURCE = "Yahoo Finance chart API v8 (query1.finance.yahoo.com), 5m bars, regular hours"
VERSION = "stock_bot v1 (orb15_v1)"

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
MAX_ENTRIES_DAY = 4
POS_NOTIONAL_MAX = 1.0     # each position <= 1x equity notional (no leverage per position)
TOTAL_NOTIONAL_MAX = 2.0   # all open positions <= 2x equity (Reg-T-like intraday buying power)
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
def fetch_chart(sym, rng="5d", interval="5m"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range={rng}&interval={interval}&includePrePost=false"
    r = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read())["chart"]["result"][0]
    q = r["indicators"]["quote"][0]; bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c): continue
        bars.append(dict(t=t, o=o, h=h, l=l, c=c, v=v or 0))
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

STRATEGIES = {"orb15_v1": orb15_v1}
ACTIVE_STRATEGY = "orb15_v1"

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
    """Shares limited by $ risk, 1x equity per position, 2x equity total notional, 8% open risk."""
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
        if tot < 0: p["day"]["losses"] += 1
        p["open"] = [x for x in p["open"] if x is not pos]
        p["closed"].append(dict(sym=pos["sym"], side=pos["side"], strategy=pos.get("strategy"), qty0=pos["qty0"], entry=r2(pos["entry"]),
                                stop0=r2(pos["stop0"]), target=r2(pos["target"]), opened=pos["opened"], closed=when, exit=r2(px_fill),
                                exit_reason=reason, pnl=tot, R=R, entry_reason=pos.get("reason"), entry_data=pos.get("entry_data"),
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
        if not pos.get("half") and (hi_fav - (pos["entry"] + sd * R)) * sd >= 0:
            q = pos["qty"] // 2
            if q > 0: realize(p, pos, q, fill_price(pos["entry"] + sd * R, pos["side"], "close"), "1R partial: half off", when, info, log)
            pos["half"] = True; pos["stop"] = pos["entry"]
        if (hi_fav - pos["target"]) * sd >= 0:
            realize(p, pos, pos["qty"], fill_price(pos["target"], pos["side"], "close"), "2R target", when, info, log); return
        pos["best"] = max(pos.get("best", pos["entry"]) * sd, hi_fav * sd) * sd
        if pos.get("half") and pos.get("atr"):
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
        try: d = json.loads(s); d.get("status", {}).pop("checked_at", None); return json.dumps(d, sort_keys=True)
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

RULES = dict(profile="AGGRESSIVE paper (Joey 2026-10-20 'Trade both' / 'Agresivo')", universe=UNIVERSE, strategy=ACTIVE_STRATEGY,
             risk_per_trade=RISK_PCT, half_size_after_losses=HALF_AFTER_LOSSES, stop_after_losses=STOP_AFTER_LOSSES, open_risk_max=OPEN_RISK_MAX,
             daily_stop=DAILY_STOP, profit_lock=PROFIT_LOCK, max_open=MAX_OPEN, max_entries_day=MAX_ENTRIES_DAY,
             position_notional_max=POS_NOTIONAL_MAX, total_notional_max=TOTAL_NOTIONAL_MAX, slippage_bps=SLIPPAGE_BPS, commission=COMMISSION,
             stale_after_sec=STALE_SEC, session="NYSE 9:30-16:00 ET trading days; no entries after 15:30; flatten 15:55 (early close: 12:30 / 12:55)",
             data_source=SOURCE)

# ---- Main ----
def run(now=None, fetch=fetch_chart):
    now = now or datetime.now(timezone.utc); nyt = now.astimezone(NY); d = nyt.date()
    p = load(); log = []; notes = []
    if d > CAL_MAX: notes.append("WARNING: holiday calendar ends 2027-12-31; update HOLIDAYS")
    if p["day"].get("date") != d.isoformat():
        p["day"] = dict(date=d.isoformat(), realized=0.0, losses=0, entries=0, traded=[], start_eq=None)
    p["rules"] = RULES; p["version"] = VERSION
    sess = session(d)
    st = dict(checked_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), ny_time=nyt.strftime("%Y-%m-%d %H:%M %Z"))
    if not sess or not (sess[0] <= nyt < sess[1]):
        why = "weekend" if d.weekday() >= 5 else "NYSE holiday" if d in HOLIDAYS else ("before 9:30 ET" if sess and nyt < sess[0] else "after the close")
        st.update(state="market_closed", message=f"Market closed ({why}). No trades. Open positions: {len(p['open'])}.")
        if p["open"]: st["message"] += " WARNING: positions open while market closed (should have been flattened at 15:55); they will be flattened at the next open."
        p["status"] = st; p["equity"] = r2(equity(p, {})); wrote = save(p, force=False)
        print(st["message"], "(state file", "updated)" if wrote else "unchanged)"); return p
    o, c, last_entry, flat_t = sess
    data, infos = {}, {}
    for s in sorted(set(UNIVERSE) | {x["sym"] for x in p["open"]}):
        try:
            data[s] = fetch(s); infos[s] = quote_info(data[s], now)
        except Exception as e:
            notes.append(f"{s}: data error {e}")
    prices = {s: i["price"] for s, i in infos.items() if i["price"]}
    if p["day"].get("start_eq") is None: p["day"]["start_eq"] = r2(equity(p, prices))
    # 1) manage / flatten open positions
    flatten_all = nyt >= flat_t
    for pos in list(p["open"]):
        s = pos["sym"]
        if s not in infos: notes.append(f"{s}: no data, cannot manage this run"); continue
        if infos[s]["stale"] and not flatten_all: notes.append(f"{s}: stale data ({infos[s]['delay']}), managing skipped"); continue
        manage(p, pos, [b for b in data[s]["bars"] if b["t"] + 300 <= now.timestamp()], infos[s], now, log, flatten=flatten_all)
        if pos in p["open"]: pos["last"] = infos[s]["price"]; pos["last_data"] = dict(source=SOURCE, as_of=infos[s]["as_of"], delay=infos[s]["delay"])
    eq = equity(p, prices); day_pnl = round(eq - p["day"]["start_eq"], 2)
    if day_pnl <= -DAILY_STOP and p["open"]:
        for pos in list(p["open"]):
            if pos["sym"] in infos: manage(p, pos, [], infos[pos["sym"]], now, log, flatten=True, flatten_reason=f"daily loss stop (day P&L {day_pnl:+.2f} <= -${DAILY_STOP:.0f}): flatten")
        eq = equity(p, prices); day_pnl = round(eq - p["day"]["start_eq"], 2)
    # 2) entry gates
    blocks = []
    if flatten_all: blocks.append("after 15:55 flatten time")
    elif nyt >= last_entry: blocks.append("no new entries after 15:30 ET")
    if day_pnl <= -DAILY_STOP: blocks.append(f"daily loss stop hit ({day_pnl:+.2f})")
    if day_pnl >= PROFIT_LOCK: blocks.append(f"profit lock hit ({day_pnl:+.2f} >= +${PROFIT_LOCK:.0f})")
    if p["day"]["losses"] >= STOP_AFTER_LOSSES: blocks.append(f"{p['day']['losses']} losses today (stop after {STOP_AFTER_LOSSES})")
    if p["day"]["entries"] >= MAX_ENTRIES_DAY: blocks.append(f"max {MAX_ENTRIES_DAY} entries/day reached")
    risk_pct = RISK_PCT / 2 if p["day"]["losses"] >= HALF_AFTER_LOSSES else RISK_PCT
    signals = {}
    if not blocks:
        strat = STRATEGIES[ACTIVE_STRATEGY]
        for s in UNIVERSE:
            if len(p["open"]) >= MAX_OPEN or p["day"]["entries"] >= MAX_ENTRIES_DAY: break
            if s not in infos: continue
            if infos[s]["stale"]: signals[s] = f"stale data ({infos[s]['delay']}): no trade"; continue
            if any(x["sym"] == s for x in p["open"]): signals[s] = "already open"; continue
            sig, why = strat(s, data[s]["bars"], now, d)
            if not sig: signals[s] = why; continue
            if f"{s}:{sig['side']}" in p["day"]["traded"]: signals[s] = f"already traded {sig['side']} today"; continue
            info = infos[s]; px = fill_price(info["price"], sig["side"], "open"); sd = 1 if sig["side"] == "long" else -1
            stop = px - sd * sig["stop_dist"]; risk_ps = abs(px - stop)
            o_not = sum(x["entry"] * x["qty"] for x in p["open"]); o_risk = sum(abs(x["entry"] - x["stop"]) * x["qty"] for x in p["open"] if (x["entry"] - x["stop"]) * (1 if x["side"] == "long" else -1) > 0)
            q = size_position(eq, px, risk_ps, risk_pct, o_not, o_risk)
            if q < 1: signals[s] = "size 0 after risk/notional caps"; continue
            pos = dict(sym=s, side=sig["side"], strategy=ACTIVE_STRATEGY, qty=q, qty0=q, entry=px, stop=stop, stop0=stop, risk_ps=risk_ps,
                       target=px + sd * 2 * risk_ps, risk0=round(risk_ps * q, 2), atr=atr(data[s]["bars"]), best=px, seen=int(now.timestamp()) // 300 * 300 + 300,  # only bars that start after entry
                       opened=now.strftime("%Y-%m-%dT%H:%M:%SZ"), reason=sig["reason"] + (" [HALF SIZE: 2+ losses today]" if risk_pct < RISK_PCT else ""),
                       entry_data=dict(source=info["source"], as_of=info["as_of"], delay=info["delay"], signal_bar=sig["signal_bar"]), last=info["price"])
            p["open"].append(pos); p["day"]["entries"] += 1; p["day"]["traded"].append(f"{s}:{sig['side']}")
            log.append(dict(time=pos["opened"], sym=s, action="OPEN", side=pos["side"], qty=q, price=r2(px), entry=r2(px), stop=r2(stop), target=r2(pos["target"]),
                            pnl=0.0, reason=pos["reason"], source=info["source"], data_time=info["as_of"], delay=info["delay"]))
            signals[s] = f"ENTERED {sig['side']} {q} @ {px:.2f}"
    eq = equity(p, prices)
    p["equity"] = r2(eq)
    st.update(state="market_open", message=("Blocked: " + "; ".join(blocks)) if blocks else f"Scanned {len(UNIVERSE)} symbols.",
              day_pnl=r2(eq - p["day"]["start_eq"]), risk_pct_now=risk_pct, blocks=blocks, signals=signals, notes=notes,
              data={s: dict(price=r2(i["price"]), as_of=i["as_of"], delay=i["delay"], stale=i["stale"]) for s, i in infos.items()})
    p["status"] = st
    save(p); write_journal(log)
    print(st["message"], "| equity", r2(eq), "| actions", len(log))
    return p

if __name__ == "__main__":
    run()
