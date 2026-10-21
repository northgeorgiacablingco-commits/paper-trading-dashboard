"""Accounting/session regression tests for rules/stock_bot.py (no network; uses a temp dir, never touches data/). Run: python tests/test_stock_accounting.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'rules'))
import stock_bot as b
from datetime import datetime, date, timezone, timedelta
R = []
def chk(n, c): R.append(c); print("PASS" if c else "FAIL", n)
tmp = tempfile.mkdtemp(); b.PF = os.path.join(tmp, "stock_portfolio.json"); b.JOURNAL = os.path.join(tmp, "stock_journal.csv"); b.SIGNALS = os.path.join(tmp, "stock_signals.csv")
def acct(): return dict(cash=10000.0, open=[], closed=[], day=dict(date="2026-10-21", realized=0.0, losses=0, entries=0, traded=[], start_eq=10000.0))
def pos(side, entry=100.0, qty=100, rps=1.0):
    sd = 1 if side == "long" else -1
    return dict(sym="X", side=side, strategy="t", qty=qty, qty0=qty, entry=entry, stop=entry - sd * rps, stop0=entry - sd * rps, risk_ps=rps,
                target=entry + sd * 2 * rps, risk0=rps * qty, atr=0.5, best=entry, seen=0, opened="t", reason="t", entry_data={})
INFO = dict(price=100.0, source="test", as_of="t", delay="test")
# 1) realized P&L signs and cash delta == pnl (long/short, win/loss)
for side in ("long", "short"):
    for px, lab in [(103.0, "up"), (97.0, "down")]:
        p = acct(); x = pos(side); p["open"].append(x); b.realize(p, x, 100, px, "t", "t", INFO, [])
        win = (lab == "up") == (side == "long"); c = p["closed"][-1]
        chk(f"{side} {lab}: cash delta {p['cash']-10000:.2f} == pnl {c['pnl']}, {'win' if win else 'loss'}, losses={p['day']['losses']}",
            abs(p["cash"] - 10000 - c["pnl"]) < 1e-6 and (c["pnl"] > 0) == win and p["day"]["losses"] == (0 if win else 1) and not p["open"])
# 2) slippage is always adverse
chk("slippage adverse: long open pays more, long close gets less", b.fill_price(100, "long", "open") > 100 > b.fill_price(100, "long", "close"))
chk("slippage adverse: short open sells lower, short close buys higher", b.fill_price(100, "short", "open") < 100 < b.fill_price(100, "short", "close"))
chk("2 bps exactly", abs(b.fill_price(100, "long", "open") - 100.02) < 1e-9)
# 3) round trip at unchanged price loses exactly the slippage, commission $0
p = acct(); e = b.fill_price(100, "long", "open"); x = pos("long", entry=e); p["open"].append(x)
b.realize(p, x, 100, b.fill_price(100, "long", "close"), "t", "t", INFO, [])
chk(f"flat round trip costs only slippage ({p['closed'][-1]['pnl']})", abs(p["closed"][-1]["pnl"] - round((100 * 0.9998 - 100 * 1.0002) * 100, 2)) < 0.011)
# 4) equity = cash + unrealized, short unrealized sign
p = acct(); p["open"] = [pos("long"), dict(pos("short"), sym="Y")]
chk("equity marks long +2/short -2 correctly", abs(b.equity(p, {"X": 102.0, "Y": 102.0}) - (10000 + 200 - 200)) < 1e-9)
# 5) sizing caps
chk("day-trade margin caps: 4x per position, 4x total", b.POS_NOTIONAL_MAX == 4.0 and b.TOTAL_NOTIONAL_MAX == 4.0)
q = b.size_position(10000, 100, 1.0, 0.02, 0, 0); chk(f"2% risk on $10k with $1 stop -> 200 shares ($20k = 2x, under 4x cap) = {q}", q == 200)
q = b.size_position(10000, 100, 0.2, 0.02, 0, 0); chk(f"tight $0.20 stop: risk wants 1000 shares, 4x per-position cap -> 400 = {q}", q == 400)
q = b.size_position(10000, 100, 0.2, 0.02, 30000, 0); chk(f"4x total cap with $30k already open leaves $10k -> 100 = {q}", q == 100)
q = b.size_position(10000, 100, 0.2, 0.02, 40000, 0); chk(f"4x total cap fully used -> 0 = {q}", q == 0)
q = b.size_position(10000, 100, 0.2, 0.02, 0, 0); chk(f"risk at 4x cap = {q*0.2:.0f} <= 2% ($200)", q * 0.2 <= 200)
q = b.size_position(10000, 100, 5.0, 0.02, 0, 0); chk(f"risk-limited size 40 = {q}", q == 40)
q = b.size_position(10000, 100, 1.0, 0.02, 35000, 0); chk(f"4x total notional cap leaves $5k -> 50 = {q}", q == 50)
q = b.size_position(10000, 100, 1.0, 0.02, 0, 750); chk(f"8% open risk cap leaves $50 -> 50 = {q}", q == 50)
q = b.size_position(10000, 100, 5.0, 0.01, 0, 0); chk(f"half size 1% -> 20 = {q}", q == 20)
# 6) manage: 1R partial then breakeven stop; total pnl = half at +1R + 0 (minus slippage)
p = acct(); x = pos("long", qty=100); p["open"].append(x)
bars = [dict(t=1000, o=100, h=101.2, l=99.8, c=101), dict(t=1300, o=101, h=101.1, l=99.9, c=100)]
b.manage(p, x, bars, dict(INFO, price=100.0), datetime(2026, 10, 21, 15, tzinfo=timezone.utc), [])
c = p["closed"][-1] if p["closed"] else {}
chk(f"1R partial (+50), stop -> BE then trails 1xATR to 100.70, rest stopped (+35): pnl {c.get('pnl')} ~ +85 minus slippage, R {c.get('R')}", c and 82.5 < c["pnl"] < 85 and not p["open"] and p["day"]["losses"] == 0)
# 7) short hits 2R target
p = acct(); x = pos("short", qty=10); p["open"].append(x)
b.manage(p, x, [dict(t=1000, o=100, h=100.2, l=97.5, c=98)], dict(INFO, price=98), datetime(2026, 10, 21, 15, tzinfo=timezone.utc), [])
c = p["closed"][-1]; chk(f"short: 5 off at 1R + 5 at 2R target -> ~+15 ({c['pnl']}), R {c['R']}", 14.7 < c["pnl"] < 15 and abs(p["cash"] - 10000 - c["pnl"]) < 1e-6)
# 8) stop before target in same bar (conservative) and gap-through fill at open
p = acct(); x = pos("long"); p["open"].append(x)
b.manage(p, x, [dict(t=1000, o=98.5, h=103, l=98, c=102)], dict(INFO, price=102), datetime(2026, 10, 21, 15, tzinfo=timezone.utc), [])
c = p["closed"][-1]; chk(f"gap below stop fills at open 98.5 -> pnl {c['pnl']} (-150 minus 2bps slippage)", -152.5 < c["pnl"] < -150 and p["day"]["losses"] == 1)
# 9) session calendar
NY = b.NY
chk("2026-10-20 (Tue) is a trading day", b.session(date(2026, 10, 20)) is not None)
chk("weekend closed", b.session(date(2026, 10, 24)) is None)
chk("Thanksgiving 2026-11-26 closed", b.session(date(2026, 11, 26)) is None)
s = b.session(date(2026, 11, 27)); chk(f"2026-11-27 early close 13:00, last entry 12:30, flatten 12:55", s and s[1].hour == 13 and s[2].strftime("%H:%M") == "12:30" and s[3].strftime("%H:%M") == "12:55")
s = b.session(date(2026, 10, 21)); chk("normal day: last entry 15:30, flatten 15:55", s[2].strftime("%H:%M") == "15:30" and s[3].strftime("%H:%M") == "15:55")
# 10) init guard: missing file refuses without BOT_INIT_OK
os.environ.pop("BOT_INIT_OK", None)
try: b.load(); ok = False
except SystemExit: ok = True
chk("missing portfolio refuses to start without BOT_INIT_OK=1", ok)
# 11) full simulated session with synthetic bars: closed market does nothing; open market ORB long enters; 15:55 flattens
json.dump(b.new_account(), open(b.PF, "w"))
d = date(2026, 10, 21); o = datetime(2026, 10, 21, 9, 30, tzinfo=NY)
def mkbars(n, upto):
    out = []
    for i in range(78):  # prior day
        t = int((o - timedelta(days=1)).timestamp()) + i * 300; out.append(dict(t=t, o=100, h=100.3, l=99.7, c=100, v=1000))
    for i in range(n):
        t = int(o.timestamp()) + i * 300; base = 100 + (0 if i < 3 else 0.12 * (i - 2))
        if t + 300 > upto: break
        out.append(dict(t=t, o=base, h=base + (0.4 if i < 3 else 0.15), l=base - (0.2 if i < 3 else 0.05), c=base + 0.05, v=1000))
    return out
def fake(now, n=10, stale=False, sym_ok=("SPY",)):
    def f(sym):
        bars = mkbars(n, now.timestamp()) if sym in sym_ok else mkbars(3, now.timestamp())
        last = bars[-1]["c"]; t = int(now.timestamp()) - (3600 if stale else 30)
        return dict(meta=dict(price=last, time=t, prev_close=100, delayed_by=None, exchange="test"), bars=bars, url="test")
    return f
p = b.run(now=datetime(2026, 10, 20, 21, 47, tzinfo=timezone.utc), fetch=lambda s: (_ for _ in ()).throw(AssertionError("fetched while closed")))
chk(f"closed market: state={p['status']['state']}, no positions, cash 10000", p["status"]["state"] == "market_closed" and not p["open"] and p["cash"] == 10000)
now = (o + timedelta(minutes=30)).astimezone(timezone.utc)
p = b.run(now=now, fetch=fake(now, stale=True))
chk(f"stale data -> no trade ({p['status']['signals'].get('SPY')})", not p["open"] and "stale" in p["status"]["signals"].get("SPY", ""))
p = b.run(now=now, fetch=fake(now))
chk(f"ORB long entered on SPY: {p['status']['signals'].get('SPY')}", len(p["open"]) == 1 and p["open"][0]["side"] == "long" and p["day"]["entries"] == 1)
x = p["open"][0]; chk(f"entry logged with source+timestamp+delay ({x['entry_data']})", all(x["entry_data"].get(k) for k in ("source", "as_of", "delay")))
chk(f"entry within caps: notional {x['entry']*x['qty']:.0f} <= 4x equity, risk {x['risk0']} <= 2%", x["entry"] * x["qty"] <= 40000 and x["risk0"] <= 200.01)
# 11b) orb15_v1 stop_mode 'or_far' (Joey 2026-10-21 16:52Z): stop at the far side of the 15-min OR, $ risk unchanged so qty shrinks, caps still apply
chk(f"ORB long stop = OR low 99.80 exactly (stop {x['stop']}, stop0 {x['stop0']})", abs(x["stop"] - 99.8) < 1e-9 and abs(x["stop0"] - 99.8) < 1e-9)
chk(f"ORB position tagged stop_mode=or_far ({x.get('stop_mode')}) and reason tagged", x.get("stop_mode") == "or_far" and "[stop_mode=or_far]" in x["reason"])
chk(f"ORB risk_ps = entry - OR low ({x['risk_ps']:.4f}), target = entry + 2R ({x['target']:.4f})", abs(x["risk_ps"] - (x["entry"] - 99.8)) < 1e-9 and abs(x["target"] - (x["entry"] + 2 * x["risk_ps"])) < 1e-9)
qexp = b.size_position(10000, x["entry"], x["risk_ps"], b.RISK_PCT, 0, 0)
chk(f"ORB qty {x['qty']} == size_position with the wide stop ({qexp}); $ risk {x['risk0']} <= 2%, notional {x['entry']*x['qty']:.0f} <= 4x", x["qty"] == qexp and x["risk0"] <= 200.01 and x["entry"] * x["qty"] <= 40000)
jr = open(b.JOURNAL).read() if os.path.exists(b.JOURNAL) else ""
chk("journal OPEN row reason tagged [stop_mode=or_far]; signals ENTER row too", "[stop_mode=or_far]" in jr and "[stop_mode=or_far]" in open(b.SIGNALS).read())
sig, why = b.orb15_v1("SPY", mkbars(10, now.timestamp()), now, d)
chk(f"orb15_v1 long signal returns stop=OR low, no stop_dist ({sig and sig.get('stop')})", sig and sig["side"] == "long" and abs(sig["stop"] - 99.8) < 1e-9 and "stop_dist" not in sig and sig["stop_mode"] == "or_far")
mir = [dict(t=k["t"], o=200 - k["o"], h=200 - k["l"], l=200 - k["h"], c=200 - k["c"], v=k["v"]) for k in mkbars(10, now.timestamp())]
sig, why = b.orb15_v1("SPY", mir, now, d)
chk(f"orb15_v1 short signal: stop = OR high 100.20 ({sig and sig.get('stop')}, {why})", sig and sig["side"] == "short" and abs(sig["stop"] - 100.2) < 1e-9 and sig["stop_mode"] == "or_far")
p = b.run(now=now, fetch=fake(now)); chk("no duplicate entry same symbol/side same day", len(p["open"]) + len(p["closed"]) == 1 and p["day"]["entries"] == 1)
late = (o + timedelta(hours=6, minutes=26)).astimezone(timezone.utc)
p = b.run(now=late, fetch=fake(late, n=78))
chk(f"15:56 ET: everything flat, cash moved by realized only (cash {p['cash']}, realized {p['day']['realized']})", not p["open"] and abs(p["cash"] - 10000 - p["day"]["realized"]) < 0.01)
chk("journal written", os.path.exists(b.JOURNAL) and open(b.JOURNAL).read().count("\n") >= 2)

# ===== multi-strategy tests (tjr_exact / liquidity_reversal / conflict rule / caps) =====
import csv, stock_strategies as SS
def chkp(name): return os.path.exists(b.SIGNALS) and list(csv.DictReader(open(b.SIGNALS)))
# swings / BOS
bb = [dict(t=i, o=10, h=h, l=l, c=c, reg=True) for i, (h, l, c) in enumerate([(10, 9, 9.5), (11, 9.5, 10.5), (12, 10, 11), (11, 10, 10.5), (10.5, 9.8, 10), (10.2, 8.5, 9.0), (10, 8.8, 9.5)])]
hs, ls = SS.swings(bb); chk(f"swing high at idx 2 (12) found: {hs}", hs and hs[0] == (2, 12))
chk("cost rule: stop 0.10 on $100 < 3x4bps (0.12) fails, 0.13 passes", not SS.cost_ok(100, 100.10) and SS.cost_ok(100, 100.13))
D21 = date(2026, 10, 21); O21 = datetime(2026, 10, 21, 9, 30, tzinfo=b.NY).timestamp()
def scen(sc=1.0, qqq_bull=False):
    """Prior day flat 100 +/-0.5; overnight high 100.8; 09:35 sweeps ONH to 101.6; 09:40 rejects (close 100.3); 09:45 pulls back to 100.9 and closes 100.25."""
    P = lambda x: 100 + (x - 100) * sc
    out = []; pd0 = datetime(2026, 10, 20, 9, 30, tzinfo=b.NY).timestamp()
    for i in range(78): out.append(dict(t=pd0 + i * 300, o=100, h=P(100.5), l=P(99.5), c=100, v=1000))
    for i in range(48): out.append(dict(t=pd0 + 6.5 * 3600 + i * 300, o=100, h=P(100.8) if i == 5 else P(100.2), l=P(99.6) if i == 7 else P(99.9), c=100, v=10))
    pm0 = datetime(2026, 10, 21, 4, 0, tzinfo=b.NY).timestamp()
    for i in range(66): out.append(dict(t=pm0 + i * 300, o=100, h=P(100.3), l=P(99.9), c=100, v=10))
    day = [(100.5, 100.7, 100.3, 100.6), (100.6, 101.6, 100.5, 100.9), (100.9, 101.0, 100.2, 100.3), (100.3, 100.9, 100.2, 100.25), (100.25, 100.3, 100.1, 100.15)]
    for i, (o_, h_, l_, c_) in enumerate(day): out.append(dict(t=O21 + i * 300, o=P(o_), h=P(h_), l=P(l_), c=P(c_), v=1000))
    for x in out: x["t"] = int(x["t"]); x["reg"] = b.is_reg(x["t"])
    return out
def ctx_at(now_ts, sc=1.0, qqq_bull=False):
    D = {}
    for s_ in SS.PAIR:
        bars = scen(sc)
        if s_ == "QQQ" and qqq_bull:
            for x in bars:
                if x["t"] == O21 + 600: x["o"], x["c"] = x["c"], x["o"] + 0.0   # QQQ 09:40 candle bullish instead
                if x["t"] == O21 + 600: x["o"], x["c"] = 100.2, 100.8
        D[s_] = dict(bars=[x for x in bars if x["t"] + 300 <= now_ts])
    return dict(now=now_ts, d=D21, data=D)
sig, sk = SS.liquidity_reversal(ctx_at(O21 + 1200))
spy = [x for x in sig if x["sym"] == "SPY"]
chk(f"LR: SPY short after ONH sweep+reject+pullback ({spy[0]['reason'][:90] if spy else sk})", spy and spy[0]["side"] == "short")
if spy:
    x = spy[0]; chk(f"LR: stop {x['stop']:.2f} just above sweep extreme 101.6, TP1 {x['tp1']:.2f} = 1.5R (no opposing level 1-1.5R away), trail on, flat 11:30",
                    101.6 < x["stop"] < 101.7 and abs((100.25 - x["tp1"]) - 1.5 * (x["stop"] - 100.25)) < 1e-6 and x["trail"] and x["target"] is None
                    and datetime.fromtimestamp(x["flat_by"], b.NY).strftime("%H:%M") == "11:30")
sig, sk = SS.liquidity_reversal(ctx_at(O21 + 1200, qqq_bull=True))
chk(f"LR: QQQ candle disagrees -> SPY skipped with reason ({[k['reason'][:60] for k in sk if k['sym']=='SPY']})", not [x for x in sig if x["sym"] == "SPY"] and any("disagree" in k["reason"] for k in sk if k["sym"] == "SPY"))
sig, sk = SS.liquidity_reversal(ctx_at(O21 + 1200, sc=0.02))
chk(f"LR: tiny sweep -> stop < 3x round-trip cost -> skipped ({[k['reason'][:70] for k in sk][:1]})", not sig and any("round-trip cost" in k["reason"] for k in sk))
sig, sk = SS.liquidity_reversal(ctx_at(O21 + 2400))
chk("LR: entry bar no longer fresh 20 min later -> no new signal (stale skip logged)", not sig and any("stale" in k["reason"] for k in sk))
# TJR: levels and SMT gate pieces
lv = SS.unswept(SS.base_levels(scen(), D21), scen(), O21); names = sorted(l[0] for l in lv)
chk(f"levels: PDH 100.5 was swept overnight (100.8) so it is dropped; ONH/ONL/PDL kept: {names}", names == ["ONH", "ONL", "PDL"])
chk("TJR rules as written: sweep 09:30-09:50, entry 09:50-10:10, flat 11:30, 1 trade, stop after 1 loss, TP1 >= 1.5R",
    SS.TJR["sweep"] == ("09:30", "09:50") and SS.TJR["entry"] == ("09:50", "10:10") and SS.TJR["flat"] == "11:30" and SS.TJR["max_trades"] == 1 and SS.TJR["tp1_min_R"] == 1.5)
sig, sk = SS.tjr_exact(ctx_at(O21 + 1500))
chk(f"TJR on the LR scenario: no entry before 09:50 window/alignment ({len(sig)} signals, skips {[k['reason'][:50] for k in sk]})", not sig)
# run(): LR live path, conflict rule, per-strategy caps (temp account)
json.dump(b.new_account(), open(b.PF, "w")); open(b.SIGNALS, "w").close(); os.remove(b.SIGNALS)
U0 = b.UNIVERSE; b.UNIVERSE = ["SPY", "QQQ"]
def fk(now_ts, sc=1.0):
    def f(s_):
        bars = [x for x in scen(sc) if x["t"] + 300 <= now_ts]
        return dict(meta=dict(price=bars[-1]["c"], time=int(now_ts) - 20, prev_close=100, delayed_by=None, exchange="t"), bars=bars, url="t")
    return f
nowdt = datetime.fromtimestamp(O21 + 1200, timezone.utc)
p = b.run(now=nowdt, fetch=fk(O21 + 1200), strategies=["liquidity_reversal"])
chk(f"run LR: entered SPY+QQQ shorts tagged liquidity_reversal ({[(x['sym'], x['strategy']) for x in p['open']]})", len(p["open"]) == 2 and all(x["strategy"] == "liquidity_reversal" and x["side"] == "short" for x in p["open"]))
chk(f"run LR: tp1 set, no fixed target, flat_by 11:30, risk <= 2% ({[x['risk0'] for x in p['open']]})", all(x["tp1"] and x["target"] is None and x["flat_by"] and x["risk0"] <= 200.01 for x in p["open"]))
rows = list(csv.DictReader(open(b.SIGNALS))); chk(f"signals csv logs ENTER rows with strategy ({len(rows)} rows)", sum(r["event"] == "ENTER" and r["strategy"] == "liquidity_reversal" for r in rows) == 2)
late = datetime.fromtimestamp(O21 + 2 * 3600 + 5 * 60, timezone.utc)   # 11:35 ET: time exit
p = b.run(now=late, fetch=fk(O21 + 1500), strategies=["liquidity_reversal"])
chk(f"LR time exit by 11:30: flat, exit reason {[c['exit_reason'] for c in p['closed']]}", not p["open"] and all("time exit" in c["exit_reason"] or "stop" in c["exit_reason"] or "TP1" in c["exit_reason"] for c in p["closed"]))
st = p["stats"].get("liquidity_reversal", {}); chk(f"per-strategy stats present {st}", st.get("trades") == 2 and "avg_R" in st and "win_rate" in st)
# conflict: two stub strategies signal SPY in the same run
json.dump(b.new_account(), open(b.PF, "w")); os.remove(b.SIGNALS)
stub = lambda ctx: ([dict(sym="SPY", side="short", stop=101.65, tp1=99.0, target=None, trail=True, flat_by=O21 + 7200, key="stub:" + str(id(ctx)) , signal_bar=0, reason="stub")], [])
b.STRATEGIES["A"] = stub; b.STRATEGIES["B"] = lambda ctx: ([dict(stub(ctx)[0][0], key="stubB")], [])
b.STRAT_CAPS["A"] = b.STRAT_CAPS["B"] = dict(entries=2, loss_stop=1)
p = b.run(now=nowdt, fetch=fk(O21 + 1200), strategies=["A", "B"])
rows = list(csv.DictReader(open(b.SIGNALS)))
chk(f"conflict: first strategy (A) takes SPY, B skipped with logged reason", len(p["open"]) == 1 and p["open"][0]["strategy"] == "A" and any(r["strategy"] == "B" and "conflict" in r["reason"] for r in rows))
# per-strategy loss stop
json.dump(b.new_account(), open(b.PF, "w")); os.remove(b.SIGNALS)
q = json.load(open(b.PF)); q["day"] = dict(date="2026-10-21", realized=-20.0, losses=1, entries=1, traded=[], start_eq=10000.0, strat={"liquidity_reversal": dict(entries=1, losses=1)}, logged=[]); json.dump(q, open(b.PF, "w"))
p = b.run(now=nowdt, fetch=fk(O21 + 1200), strategies=["liquidity_reversal"])
rows = list(csv.DictReader(open(b.SIGNALS)))
chk("LR done for the day after its first loss (signal logged as skipped)", not p["open"] and any("done for the day" in r["reason"] for r in rows))
b.UNIVERSE = U0
print(f"\n{sum(R)}/{len(R)} passed")
sys.exit(0 if all(R) else 1)
