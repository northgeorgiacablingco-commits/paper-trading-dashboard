#!/usr/bin/env python3
"""Paper-trading stats. Reads journal.csv and skips PLANNED/CANCELLED or unfilled rows."""
import csv, json, os, sys
D = os.path.dirname(os.path.abspath(__file__))
J = os.path.join(D, "journal.csv")
def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None
rows = [r for r in csv.DictReader(open(J)) if r["exit_reason"].strip().upper() not in ("PLANNED","CANCELLED","") and f(r["pnl_$"]) is not None]
start = json.load(open(os.path.join(D,"state.json")))["starting_balance"]
if not rows:
    print("No closed trades yet."); sys.exit(0)
R = []
for r in rows:
    rm = f(r["R_multiple"])
    if rm is None and f(r["risk_$"]): rm = f(r["pnl_$"]) / f(r["risk_$"])
    R.append(rm or 0.0)
pnl = [f(r["pnl_$"]) for r in rows]
wins = [x for x in R if x > 0]; losses = [x for x in R if x <= 0]
gw = sum(p for p in pnl if p > 0); gl = -sum(p for p in pnl if p < 0)
eq = [start]; [eq.append(eq[-1] + p) for p in pnl]
peak, mdd = eq[0], 0.0
for v in eq:
    peak = max(peak, v); mdd = max(mdd, (peak - v) / peak)
adh = sum(1 for r in rows if r["followed_rules(Y/N)"].strip().upper() == "Y") / len(rows)
n = len(rows)
print(f"Closed trades: {n}" + ("  (sample < 50: too early to judge)" if n < 50 else ""))
print(f"Win rate: {len(wins)/n:.1%}")
print(f"Avg win: {sum(wins)/len(wins):.2f}R" if wins else "Avg win: n/a")
print(f"Avg loss: {sum(losses)/len(losses):.2f}R" if losses else "Avg loss: n/a")
print(f"Expectancy: {sum(R)/n:.2f}R per trade")
print(f"Profit factor: {gw/gl:.2f}" if gl else "Profit factor: inf (no losses)")
print(f"Net P&L: ${sum(pnl):,.2f}  Ending equity: ${eq[-1]:,.2f}")
print(f"Max drawdown: {mdd:.2%}")
print(f"Rule adherence: {adh:.0%}" + ("  (below 90% target)" if adh < 0.9 else ""))
try:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    plt.figure(figsize=(8,4)); plt.plot(range(len(eq)), eq, marker="o")
    plt.title("Paper equity curve"); plt.xlabel("Trade #"); plt.ylabel("Equity ($)"); plt.grid(alpha=.3)
    out = os.path.join(D, "equity_curve.png"); plt.tight_layout(); plt.savefig(out); print("Equity curve:", out)
except ImportError:
    print("matplotlib not installed; skipped PNG (pip install matplotlib)")
