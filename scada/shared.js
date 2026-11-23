// The small helpers both pages use (scada/home.html and scada/dashboard.html). This file is the one source:
// tools/sync_pages.py copies it into the SHARED block of each page, so the pages stay single files.
// LANG ("en" / "da") is the page's own variable.
const LOC=()=>LANG==="da"?"da-DK":"en-GB";
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
// a number as display text with d decimals (decimal comma in Danish); "—" when there is none
const fmt=(v,d)=>{ if(v==null||isNaN(v)) return "—"; const s=Number(v).toFixed(d).replace(/^-(0(\.0+)?)$/,"$1"); return LANG==="da"?s.replace(".",","):s; };
// the middle value (the mean of the two middle ones for an even count); null for an empty list
const median=a=>{ if(!a.length) return null; const s=a.slice().sort((x,y)=>x-y), m=s.length>>1; return s.length%2?s[m]:(s[m-1]+s[m])/2; };
// a clock time "17:15" (sec: "17:15:01"), the same style in both languages - the style the time fields use
const clock=(ep,sec)=>{ if(!ep) return "—"; const d=new Date(ep*1000), p=n=>String(n).padStart(2,"0");
  return p(d.getHours())+":"+p(d.getMinutes())+(sec?":"+p(d.getSeconds()):""); };
// Chart axes with round steps (1, 2, 5 x 10^n), so every tick label is exact and no half steps.
const niceStep=v=>{ if(!(v>0)) return 1; const p=Math.pow(10,Math.floor(Math.log10(v))), f=v/p; return (f<=1?1:f<=2?2:f<=5?5:10)*p; };
// [from, to, step] with 4 steps that cover lo..hi; pos = never below 0 (all values are positive)
const niceAxis=(lo,hi,pos)=>{ if(pos) lo=0; if(hi-lo<1e-9){ lo-=1; hi+=1; } let st=niceStep((hi-lo)/4), a=0;
  for(let k=0;k<20;k++){ a=Math.floor(lo/st)*st; if(pos&&a<0) a=0; if(a+4*st>=hi) break; st=niceStep(st*1.01); } return [a,a+4*st,st]; };
// decimals a tick label needs for that step
const axisDec=st=>st<0.1?2:(Math.abs(st-Math.round(st))>1e-9?1:0);
// GET (no body) or POST (JSON body) to the monitor -> {ok, j}
const api=(path,body)=>fetch(path, body===undefined?{cache:"no-cache"}:
  {method:"POST",headers:{"Content-Type":"application/json","X-Burner":"1"},body:JSON.stringify(body)})
  .then(r=>r.json().then(j=>({ok:r.ok,j})));
