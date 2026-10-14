/* Detail sheets for stat cards. Config: window.SHEETS={title(key), render(key)->html}. Deep link: #open-trades etc. */
(function(){
const S=window.SHEETS;if(!S)return;
let bd,sh,cur=null,lastFocus=null;
function build(){bd=document.createElement("div");bd.className="sh-bd";bd.onclick=()=>close(true);
 sh=document.createElement("div");sh.className="sh";sh.setAttribute("role","dialog");sh.setAttribute("aria-modal","true");
 sh.innerHTML='<div class="sh-grab"></div><div class="sh-hd"><h3 id="shT"></h3><button class="sh-x" aria-label="Close">&times;</button></div><div class="sh-bdy" id="shB"></div>';
 sh.querySelector(".sh-x").onclick=()=>close(true);}
function open(k){if(!bd)build();const n=S.keys[k];if(!n)return;cur=k;lastFocus=document.activeElement;
 sh.querySelector("#shT").textContent=n;sh.querySelector("#shB").innerHTML=S.render(k);
 if(!bd.isConnected){document.body.append(bd,sh);requestAnimationFrame(()=>{bd.classList.add("on");sh.classList.add("on")});}
 document.body.classList.add("sh-lock");sh.querySelector(".sh-x").focus({preventScroll:true});}
function hide(){cur=null;if(!bd||!bd.isConnected)return;bd.classList.remove("on");sh.classList.remove("on");document.body.classList.remove("sh-lock");
 setTimeout(()=>{if(!cur){bd.remove();sh.remove();}},200);if(lastFocus&&lastFocus.focus)lastFocus.focus({preventScroll:true});}
function close(viaUI){if(viaUI&&location.hash.slice(1)===cur){history.back();}else hide();}
function route(){const h=location.hash.slice(1);if(S.keys[h])open(h);else hide();}
window.addEventListener("hashchange",route);
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&cur)close(true);});
document.querySelectorAll("[data-sheet]").forEach(c=>{c.classList.add("tap");c.tabIndex=0;c.setAttribute("role","button");
 c.setAttribute("aria-label",(c.querySelector(".label")||c).textContent.trim()+": tap for details");
 const q=document.createElement("div");q.className="tapcue";q.innerHTML='Details <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4.5 2.5 8 6l-3.5 3.5"/></svg>';c.append(q);
 const go=()=>{const k=c.dataset.sheet;if(location.hash.slice(1)===k)open(k);else location.hash=k;};
 c.addEventListener("click",go);c.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();go();}});});
S.refresh=()=>{if(cur&&sh)sh.querySelector("#shB").innerHTML=S.render(cur);};
if(document.readyState==="complete")route();else window.addEventListener("load",route);setTimeout(route,1500);
})();
