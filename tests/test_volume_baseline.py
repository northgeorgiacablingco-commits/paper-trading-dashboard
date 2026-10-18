"""Market-volume baseline data-fix tests (no network, no data files touched). Run: python tests/test_volume_baseline.py"""
import os, sys, math
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'rules'))
import crypto_bot as b
R = []
def chk(n, c): R.append(c); print("PASS" if c else "FAIL", n)
B = 1e9
# ~7 days of hourly-ish readings around $60-75B plus three bogus ~$2T prints (Oct 14 03:38-05:57 UTC incident)
normal = [60*B, 62*B, 64*B, 66*B, 68*B, 70*B, 72*B, 74*B, 61*B, 65*B, 69*B, 73*B, 63*B, 67*B, 71*B]
bogus = [2.0e12, 2.05e12, 1.98e12]
window = normal[:7] + bogus + normal[7:]
old_mean = sum(window) / len(window)
base, nrej = b.vol_baseline(window)
chk(f"3 bogus ~$2T readings rejected (rejected={nrej})", nrej == 3)
med = sorted(normal)[len(normal)//2]
chk(f"baseline is median of cleaned readings (${base/B:.1f}B == ${med/B:.1f}B)", abs(base - med) < 1)
chk(f"old mean inflated (${old_mean/B:.1f}B) vs cleaned median (${base/B:.1f}B)", old_mean > 3 * base and 64*B <= base <= 70*B)
chk("even count uses mean of middle two", b.vol_baseline([10*B, 20*B, 30*B, 40*B]) == (25*B, 0))
chk("non-positive / NaN / inf readings rejected", b.vol_baseline([60*B, 0, -5*B, float('nan'), float('inf'), 64*B, 62*B]) == (62*B, 4))
chk("no valid readings -> baseline 0 (caller blocks)", b.vol_baseline([0, float('nan')])[0] == 0)
chk("reading exactly 3x median is kept (strictly >3x rejected)", b.vol_baseline([10*B, 10*B, 30*B])[1] == 0)
# 70% gate unchanged
chk(f"today ~$55B passes vs cleaned ~${base/B:.0f}B (70% = ${0.7*base/B:.1f}B)", b.vol_gate_ok(55*B, base))
chk(f"$55B would have BLOCKED under old mean ${old_mean/B:.1f}B", not (55*B >= 0.7 * old_mean))
chk("genuinely low $30B still blocked", not b.vol_gate_ok(30*B, base))
for lo, hi in [(64*B, 70*B)]:
    chk("$55B passes across cleaned $64-70B range", b.vol_gate_ok(55*B, lo) and b.vol_gate_ok(55*B, hi))
    chk("$30B blocks across cleaned $64-70B range", not b.vol_gate_ok(30*B, lo) and not b.vol_gate_ok(30*B, hi))
chk("gate threshold still exactly 70%", b.vol_gate_ok(70*B, 100*B) and not b.vol_gate_ok(69.99*B, 100*B))
inp = list(window); b.vol_baseline(inp); chk("input history list not modified", inp == window)
print(f"{sum(R)}/{len(R)} passed"); sys.exit(0 if all(R) else 1)
