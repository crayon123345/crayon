// Render only explicit answer numbers, never infer or fetch private data.
(()=>{
const log=document.querySelector('#log');
const render=()=>log.querySelectorAll('.msg.ai .bubble').forEach(b=>{
 if(b.dataset.cardChecked)return;b.dataset.cardChecked='yes';
 const text=b.textContent;
 if(!/Source:/.test(text))return;
 const matches=[...text.matchAll(/(?:₹|Rs\s?)[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s?°\s?C/g)].slice(0,2);
 if(!matches.length)return;
 const box=document.createElement('div');box.className='verified-data-card';
 const label=document.createElement('span');label.textContent='From the sourced answer';box.append(label);
 for(const m of matches){const n=document.createElement('strong');n.textContent=m[0];box.append(n);}
 const note=document.createElement('small');note.textContent='Forecast or advertised price where stated. Details and source below.';box.append(note);b.prepend(box);
});
new MutationObserver(render).observe(log,{childList:true,subtree:true});
})();
