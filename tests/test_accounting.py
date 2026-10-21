"""Balance/sign regression tests for rules/crypto_bot.py (no network, no data files touched). Run: python tests/test_accounting.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'rules'))
import copy
import crypto_bot as b
from datetime import datetime,timezone
R=[]
def chk(n,c): R.append(c); print("PASS" if c else "FAIL",n)
def acct(): return dict(sleeves={"core":dict(cash=7000.0),"alts":dict(cash=2000.0),"meme":dict(cash=1000.0)},closed=[],day=dict(losses=0,meme_losses=0,realized=0),cash=10000.0)
def spot(entry,qty,rate=0.001):
    p=acct(); fee=entry*qty*rate; p["sleeves"]["core"]["cash"]-=entry*qty+fee
    pos=dict(coin="X",sleeve="core",entry=entry,qty=qty,qty0=qty,entry_fee=fee,cost_rate=rate,risk=10.0,stop=entry*0.95)
    return p,pos
def perp(side,entry,contracts,cs=0.1,lev=2,fund=0.0):
    p=acct(); sd=1 if side=="long" else -1; qty=contracts*cs; m=qty*entry/lev; fee=b.perp_fee(entry,qty,cs)
    p["sleeves"]["core"]["cash"]-=m+fee
    pos=dict(coin="ETH",sleeve="core",inst="perp",side=side,sd=sd,cs=cs,qty=qty,qty0=qty,margin=m,entry=entry,entry_fee=fee,funding_paid=fund,risk=50.0,lev=lev)
    return p,pos
def delta(p): return p["sleeves"]["core"]["cash"]-7000
for px,lab in [(110,"win"),(90,"loss")]:
    p,pos=spot(100,10); b.close_pos(p,pos,px,0,"t",[]); d=delta(p)
    chk(f"spot long {lab}: cash delta {d:.4f} == pnl {pos['pnl']} and sign ok", abs(d-pos['pnl'])<0.006 and (d>0)==(lab=="win"))
for side in ("long","short"):
    for px,lab in [(2600,"up"),(2400,"down")]:
        p,pos=perp(side,2500,20,fund=1.5); b.close_perp(p,pos,px,0,"t",[]); d=delta(p)
        win=(lab=="up")==(side=="long")
        chk(f"perp {side} {lab}: cash delta {d:.4f} == pnl {pos['pnl']}, {'win' if win else 'loss'}", abs(d-pos['pnl'])<0.006 and (d>0)==win)
# flat close: fees+funding must make it negative
p,pos=perp("short",2500,20,fund=0.5); b.close_perp(p,pos,2500,0,"t",[]); chk(f"perp flat close loses fees+funding ({pos['pnl']})", pos['pnl']<0)
p,pos=perp("short",2500,20,fund=-3.0); b.close_perp(p,pos,2500,0,"t",[]); fee_only=pos['pnl']
p2,pos2=perp("short",2500,20,fund=0.0); b.close_perp(p2,pos2,2500,0,"t",[]); chk(f"short receiving funding improves pnl ({fee_only} > {pos2['pnl']})", fee_only>pos2['pnl'])
# isolated liquidation caps loss at margin+entry fee
p,pos=perp("long",2500,20); b.close_perp(p,pos,1000,0,"liq",[],liq=True); chk(f"liq loss capped: pnl {pos['pnl']} >= -(margin+fee) {-(pos['margin']+pos['entry_fee']):.2f}", pos['pnl']>=-(pos['margin']+pos['entry_fee'])-0.01 and abs(delta(p)-pos['pnl'])<0.006)
# perp partial via manage_perp: candle hits 2R then later stop at breakeven
t=datetime(2026,10,15,12,0,tzinfo=timezone.utc); T=int(t.timestamp())//3600*3600
p,pos=perp("short",2500,20); pos.update(stop=2550,risk_ps=50,hi=2500,maxR=0,seen=T-7200,entry_candle=T-7200,fund_t=T,liq=4000,risk=100)
b.candles=lambda s:[dict(t=T-7200,o=2500,h=2505,l=2390,c=2400),dict(t=T-3600,o=2400,h=2600,l=2395,c=2590)]
b.price=lambda s:2590.0
p["open"]=[pos]; b.manage_perp(p,pos,t,[],{})
c=p["closed"][-1]; d=delta(p)
chk(f"perp short partial at 2R then BE stop: realized {c.get('realized')} >0, total pnl {c['pnl']} == cash delta {d:.4f}", c.get('realized',0)>0 and abs(d-c['pnl'])<0.006)
# spot partial via manage
p,pos=spot(100,10); pos.update(stop=95,risk_ps=5,hi=100,maxR=0,seen=T-7200,entry_candle=T-7200,setup="PULLBACK",level=100,entry_time="2026-10-15T09:00:00Z")
b.candles=lambda s:[dict(t=T-7200,o=100,h=111,l=99.5,c=110),dict(t=T-3600,o=110,h=110,l=99,c=99.5)]
b.price=lambda s:99.5; b.ema=lambda v,n:0
try:
    b.manage(p,pos,t,[]); c=p["closed"][-1]; d=delta(p)
    chk(f"spot partial at 2R then stop: realized {c.get('realized')} >0, total pnl {c['pnl']} == cash delta {d:.4f}", c.get('realized',0)>0 and abs(d-c['pnl'])<0.006)
except Exception as e: chk(f"spot partial ran ({e})",False)
# ---- FEE RULE (Joey 2026-10-21 00:54Z): new entries need stop >= 3x round-trip cost ----
c=b.round_trip_cost_pct("spot",4.09e-6,rate=0.005); r=b.fee_rule_block(4.09e-6,4.08e-6,c)
chk(f"fee rule SKIP: PEPE-like meme stop 0.24% vs RT cost {c*100:.2f}% -> {r}", r is not None and "FEE RULE" in r and "ratio" in r)
c=b.round_trip_cost_pct("spot",100,rate=0.001); r=b.fee_rule_block(100,98,c)
chk(f"fee rule ALLOW: core spot stop 2% vs RT cost {c*100:.2f}% (ratio 10x) -> {r}", r is None and abs(c-0.002)<1e-12)
r=b.fee_rule_block(100,99.41,c); chk(f"fee rule boundary (3x of 0.2% = 0.6%): 0.59% blocked, 0.61% allowed", r is not None and b.fee_rule_block(100,99.39,c) is None)
cp=b.round_trip_cost_pct("perp",2500,cs=0.1); exp=2*(b.PERP_FEE+b.SLIP)+2*b.PER_CONTRACT/(0.1*2500)
chk(f"futures RT cost = 2x(0.02%+0.03%) + 2x$0.15/contract notional = {cp*100:.4f}%", abs(cp-exp)<1e-12)
chk("futures: ETH stop 0.12% blocked, stop 2% allowed", b.fee_rule_block(2500,2497,cp) is not None and b.fee_rule_block(2500,2450,cp) is None)
b.price=lambda s:2500.0; nts=[]; p=acct(); p["open"]=[]
sg=dict(a=1.0,last=dict(t=0,c=2500),setup="SHORT-BREAKDOWN",e20=2510,e50=2520)
r=b.perp_entry(p,sg,{},"ETH",True,"risk-off",False,{"ETH":dict(fr_1h=0.0,src="t")},lambda:10000.0,t,{},nts)
chk(f"futures integration: perp_entry blocks tiny 1.5xATR stop via fee rule, nothing opened, logged ({r})", isinstance(r,str) and "FEE RULE" in r and not p["open"] and any("FEE RULE" in n for n in nts) and abs(p["sleeves"]["core"]["cash"]-7000)<1e-9)
print("ALL PASS" if all(R) else "FAILURES"); sys.exit(0 if all(R) else 1)
