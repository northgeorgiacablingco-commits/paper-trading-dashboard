/* Labels table cells from their headers so tables can stack into cards on mobile. */
(function(){function label(){document.querySelectorAll("table").forEach(t=>{const hs=[...t.querySelectorAll("thead th")].map(th=>th.textContent.trim());
 if(!hs.length)return;t.querySelectorAll("tbody tr").forEach(tr=>[...tr.children].forEach((td,i)=>{if(!td.dataset.label&&hs[i])td.dataset.label=hs[i];}));});}
label();new MutationObserver(label).observe(document.body,{childList:true,subtree:true});})();
