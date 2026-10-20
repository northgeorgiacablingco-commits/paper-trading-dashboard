"""Accounting/session regression tests for rules/stock_bot.py (no network; uses a temp dir, never touches data/). Run: python tests/test_stock_accounting.py"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'rules'))
import stock_bot as b
from datetime import datetime, date, timezone, timedelta
R = []
def chk(n, c): R.append(c); print("PASS" if c else "FAIL", n)
tmp = tempfile.mkdtemp(); b.PF = os.path.join(tmp, "stock_portfolio.json"); b.JOURNAL = os.path.join(tmp, "stock_journal.csv")
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
q = b.size_position(10000, 100, 1.0, 0.02, 0, 0); chk(f"2% risk on $10k with $1 stop -> min(200 risk shares, 100 by 1x notional) = {q}", q == 100)
q = b.size_position(10000, 100, 5.0, 0.02, 0, 0); chk(f"risk-limited size 40 = {q}", q == 40)
q = b.size_position(10000, 100, 1.0, 0.02, 15000, 0); chk(f"2x total notional cap leaves $5k -> 50 = {q}", q == 50)
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
chk(f"entry within caps: notional {x['entry']*x['qty']:.0f} <= equity, risk {x['risk0']} <= 2%", x["entry"] * x["qty"] <= 10000 and x["risk0"] <= 200.01)
p = b.run(now=now, fetch=fake(now)); chk("no duplicate entry same symbol/side same day", len(p["open"]) + len(p["closed"]) == 1 and p["day"]["entries"] == 1)
late = (o + timedelta(hours=6, minutes=26)).astimezone(timezone.utc)
p = b.run(now=late, fetch=fake(late, n=78))
chk(f"15:56 ET: everything flat, cash moved by realized only (cash {p['cash']}, realized {p['day']['realized']})", not p["open"] and abs(p["cash"] - 10000 - p["day"]["realized"]) < 0.01)
chk("journal written", os.path.exists(b.JOURNAL) and open(b.JOURNAL).read().count("\n") >= 2)
print(f"\n{sum(R)}/{len(R)} passed")
sys.exit(0 if all(R) else 1)
