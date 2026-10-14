#!/usr/bin/env python3
"""Pull Yahoo chart API (stocks/indices/futures), CoinGecko (crypto), and RSS headlines into data/*.json."""
import json, os, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timezone
UA = {"User-Agent": "Mozilla/5.0"}
def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()
YS = [("Indices","S&P 500","^GSPC"),("Indices","Dow","^DJI"),("Indices","Nasdaq","^IXIC"),("Indices","Russell 2000","^RUT"),
      ("Futures","S&P Fut","ES=F"),("Futures","Nasdaq Fut","NQ=F"),
      ("ETFs","SPY","SPY"),("ETFs","QQQ","QQQ"),("ETFs","DIA","DIA"),("ETFs","IWM","IWM")] + \
     [("Stocks", s, s) for s in ["AAPL","MSFT","NVDA","TSLA","GOOGL","AMZN"]]
CG = [("bitcoin","BTC"),("ethereum","ETH"),("solana","SOL"),("ripple","XRP"),("cardano","ADA"),("dogecoin","DOGE"),("binancecoin","BNB"),("avalanche-2","AVAX")]
def iso(ts): return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
markets = []
for g, n, s in YS:
    try:
        r = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(s)}?range=5d&interval=1d"))["chart"]["result"][0]
        m = r["meta"]; closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
        p = m["regularMarketPrice"]
        # prior close = last daily close before the current session's bar
        prev = closes[-2] if len(closes) >= 2 else m.get("chartPreviousClose")
        if closes and abs(closes[-1] - p) > 1e-9 and len(closes) >= 1: prev = closes[-1] if datetime.fromtimestamp(r["timestamp"][-1], timezone.utc).date() < datetime.fromtimestamp(m["regularMarketTime"], timezone.utc).date() else prev
        markets.append(dict(group=g, name=n, symbol=s, price=round(p, 2), change=round(p - prev, 2), change_pct=round((p - prev) / prev * 100, 2), as_of=iso(m["regularMarketTime"]), source="Yahoo Finance chart API"))
    except Exception as e:
        markets.append(dict(group=g, name=n, symbol=s, price=None, change=None, change_pct=None, as_of=None, source=f"Yahoo unavailable: {e}"))
try:
    cg = json.loads(get("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids=" + ",".join(i for i, _ in CG) + "&price_change_percentage=24h"))
    by = {c["id"]: c for c in cg}
    for i, sym in CG:
        c = by.get(i)
        if c: markets.append(dict(group="Crypto", name=c["name"], symbol=sym, price=c["current_price"], change=round(c.get("price_change_24h") or 0, 6), change_pct=round(c.get("price_change_percentage_24h") or 0, 2), market_cap=c.get("market_cap"), volume=c.get("total_volume"), as_of=c.get("last_updated"), source="CoinGecko"))
except Exception as e:
    print("CoinGecko failed:", e)
FEEDS = [("Yahoo Finance","https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,AAPL,NVDA,TSLA&region=US&lang=en-US"),("CNBC","https://www.cnbc.com/id/100003114/device/rss/rss.html"),
         ("CoinDesk","https://www.coindesk.com/arc/outboundfeeds/rss/"),("Cointelegraph","https://cointelegraph.com/rss")]
heads = []
for src, url in FEEDS:
    try:
        root = ET.fromstring(get(url))
        for it in root.iter("item"):
            t = (it.findtext("title") or "").strip(); l = (it.findtext("link") or "").strip()
            if t and l: heads.append(dict(title=t, source=src, url=l, time=(it.findtext("pubDate") or "").strip()))
            if sum(1 for h in heads if h["source"] == src) >= 5: break
    except Exception as e:
        print("RSS failed:", src, e)
os.makedirs("data", exist_ok=True)
def write(path, obj):
    new = json.dumps(obj, indent=1, sort_keys=True)
    old = open(path).read() if os.path.exists(path) else ""
    def strip(s):
        try: d = json.loads(s); d.pop("generated_at", None); return json.dumps(d, sort_keys=True)
        except Exception: return s
    if strip(old) != strip(new): open(path, "w").write(new); print("updated", path)
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
write("data/market.json", dict(generated_at=now, markets=markets))
write("data/headlines.json", dict(generated_at=now, headlines=heads))
