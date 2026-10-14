/* Live layer: refreshes every 60s.
   - data/market.json + data/headlines.json come from a GitHub Actions workflow (~every 15 min); fetched cache-busted.
   - Crypto prices are fetched straight from CoinGecko in the browser (CORS allowed). */
(function(){
const BASE=document.currentScript.dataset.base||"./";
const SYMS=["^GSPC","^DJI","^IXIC","^RUT","BTC","ETH","SOL","XRP","ADA","DOGE","AAPL","MSFT","NVDA","TSLA","GOOGL","AMZN"];
const LABEL={"^GSPC":"S&P 500","^DJI":"DOW","^IXIC":"NASDAQ","^RUT":"RUSSELL 2000"};
const CG={bitcoin:"BTC",ethereum:"ETH",solana:"SOL",ripple:"XRP",cardano:"ADA",dogecoin:"DOGE"};
let data={},heads=[],genAt=null,last={};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=n=>n==null?"n/a":n.toLocaleString("en-US",{maximumFractionDigits:n<1?4:2});
// bars
const bars=document.createElement("div");bars.id="liveBars";
bars.innerHTML='<div class="lb" id="breaking"><span class="tag">BREAKING</span><div style="overflow:hidden;flex:1"><span class="track" id="brTrack">Loading headlines…</span></div></div><div class="lb" id="ticker"><span class="tag"><span class="dot"></span>LIVE</span><div style="overflow:hidden;flex:1"><span class="track" id="tkTrack">Loading prices…</span></div></div>';
document.body.appendChild(bars);
// header stamp
const hdr=document.querySelector("header");let stamp=document.getElementById("liveStamp");
if(hdr&&!stamp){stamp=document.createElement("span");stamp.id="liveStamp";stamp.className="note";stamp.style.marginLeft="8px";hdr.appendChild(stamp);}
function ago(t){if(!t)return"n/a";const s=Math.max(0,(Date.now()-new Date(t))/1000|0);return s<60?s+"s ago":s<3600?(s/60|0)+"m ago":(s/3600|0)+"h ago";}
function tickStamp(){if(stamp)stamp.innerHTML='<span class="dot"></span>data updated '+ago(genAt);}
function renderTicker(){
  const items=SYMS.map(s=>data[s]).filter(Boolean);
  if(!items.length)return;
  const html=items.map(m=>{const up=(m.change_pct||0)>=0;return `<span class="tk" data-s="${esc(m.symbol)}"><b>${esc(LABEL[m.symbol]||m.symbol)}</b>${fmt(m.price)} <span class="${up?"up":"dn"}">${up?"▲":"▼"} ${m.change_pct==null?"":Math.abs(m.change_pct).toFixed(2)+"%"}</span></span>`;}).join("");
  const tr=document.getElementById("tkTrack");tr.innerHTML=html+html;tr.style.setProperty("--dur",Math.max(30,items.length*4)+"s");
  items.forEach(m=>{const p=last[m.symbol];if(p!=null&&m.price!=null&&p!==m.price){const el=tr.querySelector(`[data-s="${CSS.escape(m.symbol)}"]`);el&&el.classList.add(m.price>p?"flash-up":"flash-down");}last[m.symbol]=m.price;});
}
function renderBreaking(){if(!heads.length)return;const tr=document.getElementById("brTrack");
  tr.innerHTML=heads.slice(0,12).map(h=>`<a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.title)} <span style="color:#8b949e">(${esc(h.source)})</span></a>`).join("");tr.innerHTML+=tr.innerHTML;tr.style.setProperty("--dur",Math.max(40,heads.length*9)+"s");}
async function j(u){const r=await fetch(u+(u.includes("?")?"&":"?")+"t="+Date.now(),{cache:"no-store"});if(!r.ok)throw r.status;return r.json();}
async function refresh(){
  try{const m=await j(BASE+"data/market.json");genAt=m.generated_at;m.markets.forEach(x=>data[x.symbol]=x);}catch(e){}
  try{const h=await j(BASE+(location.pathname.includes("/crypto/")?"data/crypto_headlines.json":"data/headlines.json"));heads=h.headlines||[];}catch(e){}
  try{const c=await j("https://api.coingecko.com/api/v3/simple/price?ids="+Object.keys(CG).join(",")+"&vs_currencies=usd&include_24hr_change=true");
    for(const k in c){const s=CG[k];data[s]=Object.assign({},data[s]||{},{symbol:s,price:c[k].usd,change_pct:c[k].usd_24h_change});}}catch(e){}
  renderTicker();renderBreaking();tickStamp();
  document.dispatchEvent(new CustomEvent("livedata",{detail:{data,heads,genAt}}));
}
// count-up animation for stat values like "$10,000.00"
function countUp(el){const t=el.textContent.trim(),m=t.match(/^(-?\$?)([\d,]+(?:\.\d+)?)(.*)$/);if(!m)return;
  if(document.hidden||navigator.webdriver||/Headless/i.test(navigator.userAgent)){el.textContent=t;return;}
  const target=parseFloat(m[2].replace(/,/g,"")),dec=(m[2].split(".")[1]||"").length,t0=performance.now(),D=800;
  (function step(now){const k=Math.min(1,(now-t0)/D),e=1-Math.pow(1-k,3);el.textContent=k>=1?t:m[1]+(target*e).toLocaleString("en-US",{minimumFractionDigits:dec,maximumFractionDigits:dec})+m[3];if(k<1&&!document.hidden)requestAnimationFrame(step);else el.textContent=t;})(t0);
  setTimeout(()=>el.textContent=t,D+50);document.addEventListener("visibilitychange",()=>el.textContent=t,{once:true});}
setTimeout(()=>document.querySelectorAll(".val").forEach(countUp),50);
if(window.Chart){Chart.defaults.animation={duration:1200,easing:"easeOutQuart"};}
refresh();setInterval(refresh,60000);setInterval(tickStamp,1000);
})();
