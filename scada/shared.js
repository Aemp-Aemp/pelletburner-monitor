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
// a day and month, "28 Nov" / "28. nov" (Danish without the month's own full stop, so a sentence that ends with a
// date gets one full stop); "—" when there is none
const dmon=ep=>ep?new Date(ep*1000).toLocaleDateString(LOC(),{day:"numeric",month:"short"}).replace(/\.$/,""):"—";
// a chart's width: the element's own, at least 260 px (def = before it is laid out)
const grW=(el,def)=>Math.max(260,Math.round((el&&el.clientWidth)||def||600));
// a typed number: plain digits with one . or , only (not "0x10", "1e3", "+5"); anything else -> NaN
const numTyped=s=>{ s=String(s==null?"":s).trim().replace(",","."); return /^-?\d+(\.\d+)?$/.test(s)?Number(s):NaN; };
// why a poll got no data: this device offline / no reply at all (network error, timeout) / the monitor answered with an error
const noAnswerKey=e=>navigator.onLine===false?"nodata_net":(e&&e.name&&e.name!=="SyntaxError")?"nodata_noreply":"nodata_resp";
// who / what started or stopped it: the event's `source` in the server's words. src = labels.sources of /status.json
const srcText=(e,src)=>{ if(!e||!e.source) return ""; const w=c=>{ const x=(src||{})[c]; return x?(x[LANG]||x.en||""):""; }, s=w(e.source);
  return s&&e.after_alarm?s+", "+w("after_alarm"):s; };
// "Started by: heating plan" / "Stopped by: ..." (tt = the page's text function)
const srcLine=(e,src,tt)=>{ const s=srcText(e,src); if(!s) return ""; const to=+e.to, fr=+e.from;
  return (to===2||to===3)?tt("started_by")+": "+s:(fr!==6&&(to===0||to===1||to===5))?tt("stopped_by")+": "+s:(fr===6&&to===0)?tt("reset_by")+": "+s:s; };
// the source of a command ("warm mode (low power)", "web", "phone start" ...) in the page's words: the same prefixes
// as burner_ext.source_of, anything else is a remote command
const CMD_SRC=[["warm","plan"],["schedule","schedule"],["auto_reset","auto_reset"],["mqtt","mqtt"],["web","web"],["phone","phone"]];
const cmdSrc=(s,src)=>{ s=String(s||""); const k=CMD_SRC.find(p=>s.startsWith(p[0])); return srcText({source:k?k[1]:"remote"},src)||s; };

// ---- Lighting help ("Trouble lighting?"), the same card on both pages. READ ONLY: it shows what the server worked
// out from the settings the boiler sent and from the start records (api/ignitions "help"). It never sends anything.
// The words are here (EN/DA); the server sends codes and numbers.
const LH_TXT={
 en:{title:"Trouble lighting?", ro:"This card only reads; it never changes a boiler setting.",
  v_ok:"Lights fine: about {a} min to flame in the last {n} starts.", v_ok1:"One start did not light.",
  v_slow:"Lights slowly: about {a} min to flame in the last {n} starts.",
  v_fails:"Often fails to light: {a} of the last {n} starts did not light.",
  v_few:"Too few starts yet to say how it lights.",
  set_h:"The boiler's lighting settings", c_set:"Setting", c_now:"Now", c_fac:"Factory", c_ref:"One 15 kW boiler",
  n_014:"Start fuel (TÆNDBRÆNDSEL)", n_015:"Air at lighting (TÆND LUFT)", n_017:"Lighting timeout (TIMEOUT)",
  n_018:"Top-up doses, max (MAX DOSER)", n_001:"Fuel rate (BRÆNDSEL KG/T)", n_035:"PID P",
  boiler:"Boiler: {m} type, software {v}.", sw_none:"not typed in", type_none:"unknown",
  fac_note:"Factory: the boiler's own values for this software and boiler type. One 15 kW boiler: what lit in 3–5 minutes there. It is an example, not the one right answer.",
  sw_note:"The software version is not typed in (Settings → My boiler) or is unknown, so the factory values are those of 1.40 and newer.",
  no_cfg:"The boiler has not sent its settings yet.",
  m_diff:"not the factory value", m_unused:"not used by software 1.40",
  m_doses:"MAX DOSER is below 2: 0 gives about 255 top-up doses (only the timeout stops them), 1 gives none.", m_high:"Start fuel is high. One 15 kW boiler took over 10 minutes to flame and smoked with 60 s; it lit in 3–5 minutes with 40 s.",
  m_low:"Start fuel is below the factory value. Too few pellets may not catch.",
  m_short:"The lighting timeout is short. The boiler may give up before the pellets catch.",
  blind:"The monitor cannot read the fan speed points (BLÆSER YDELSE) or the service menu. Look at them on the boiler's screen.",
  try_h:"What to try, one thing at a time",
  s1:"Clean the burner pot and its air holes, and empty the ash.",
  s2:"Wipe the flame sensor glass. The boiler only sees the fire through it.",
  s3:"Check the igniter: it must get hot. Test it on the boiler: MONTØR → AVANCERET → TEST MENU → OUTPUTS → TÆNDER.",
  s4:"Use dry pellets. Damp pellets light badly.",
  s5:"A boiler type change, and most likely a software upgrade too, puts the boiler back on factory settings. Compare with the table above and put your old values back: MONTØR → OPTÆNDING.",
  s6:"Soot or coarse ash as well? Look at the fan: MONTØR → SUGEINDSTILLINGER → BLÆSER YDELSE. The 30 % point is 500 rpm from the factory. That gave too little air on one 15 kW boiler; 1200 rpm worked there. (The rpm points and the 500 rpm factory value are from software 1.40 on the 10 and 15 kW types.)",
  s7:"Then the lighting settings in MONTØR → OPTÆNDING. Write the old value down, change one setting, and watch the next 3 starts here. Start with the start fuel (TÆNDBRÆNDSEL).",
  s7_sync:"The monitor's settings sync is on: it puts its saved value back. Change the value on the page instead, or turn the sync off.",
  s8:"Still bad? Press Copy lighting report and paste it where you ask for help.",
  copy:"Copy lighting report", copied:"Copied", copy_note:"The report includes your start times.", guide:"Guide: after a boiler software upgrade", open:"Trouble lighting? See what to try",
  r_title:"Lighting report (pellet boiler monitor {v})", r_set:"Lighting settings (now / factory):", r_starts:"Last {n} starts (newest first):",
  r_none:"No starts recorded yet.", r_lit:"lit, {a} min to flame", r_lit0:"lit", r_fail:"did not light", r_fail_a:"did not light (alarm {a})", r_stop:"stopped",
  r_cfg:"start fuel {a} s, max doses {b}", r_blind:"Not read by the monitor: fan speed points (BLÆSER YDELSE), service menu.", r_marks:"* = not the factory value, ! = worth a look"},
 da:{title:"Problemer med optænding?", ro:"Dette kort læser kun; det ændrer aldrig en indstilling på kedlen.",
  v_ok:"Tænder fint: ca. {a} min til flamme i de seneste {n} starter.", v_ok1:"Én start tændte ikke.",
  v_slow:"Tænder langsomt: ca. {a} min til flamme i de seneste {n} starter.",
  v_fails:"Tænder ofte ikke: {a} af de seneste {n} starter tændte ikke.",
  v_few:"For få starter endnu til at sige, hvordan den tænder.",
  set_h:"Kedlens indstillinger for optænding", c_set:"Indstilling", c_now:"Nu", c_fac:"Fabrik", c_ref:"Én 15 kW kedel",
  n_014:"Tændbrændsel (TÆNDBRÆNDSEL)", n_015:"Luft ved optænding (TÆND LUFT)", n_017:"Timeout for optænding (TIMEOUT)",
  n_018:"Ekstra doser, højst (MAX DOSER)", n_001:"Brændsel (BRÆNDSEL KG/T)", n_035:"PID P",
  boiler:"Kedel: type {m}, software {v}.", sw_none:"ikke skrevet ind", type_none:"ukendt",
  fac_note:"Fabrik: kedlens egne værdier for denne software og kedeltype. Én 15 kW kedel: det, der tændte på 3–5 minutter dér. Det er et eksempel, ikke det eneste rigtige svar.",
  sw_note:"Softwareversionen er ikke skrevet ind (Indstillinger → Min kedel) eller er ukendt, så fabriksværdierne er dem for 1.40 og nyere.",
  no_cfg:"Kedlen har ikke sendt sine indstillinger endnu.",
  m_diff:"ikke fabriksværdien", m_unused:"bruges ikke af software 1.40",
  m_doses:"MAX DOSER er under 2: 0 giver ca. 255 ekstra doser (kun timeout stopper dem), 1 giver ingen.", m_high:"Tændbrændsel er højt. Én 15 kW kedel var over 10 minutter om at få flamme og røg med 60 s; den tændte på 3–5 minutter med 40 s.",
  m_low:"Tændbrændsel er under fabriksværdien. For få piller fænger måske ikke.",
  m_short:"Timeout for optænding er kort. Kedlen giver måske op, før pillerne fænger.",
  blind:"Monitoren kan ikke aflæse røgsugerens omdrejninger (BLÆSER YDELSE) eller servicemenuen. Se dem på kedlens skærm.",
  try_h:"Det kan du prøve, én ting ad gangen",
  s1:"Rens herden og dens lufthuller, og tøm asken.",
  s2:"Tør flammefølerens glas af. Kedlen ser kun ilden gennem det.",
  s3:"Tjek tænderen: den skal blive varm. Test den på kedlen: MONTØR → AVANCERET → TEST MENU → OUTPUTS → TÆNDER.",
  s4:"Brug tørre piller. Fugtige piller tænder dårligt.",
  s5:"Et skift af kedeltype, og højst sandsynligt også en softwareopdatering, sætter kedlen tilbage til fabriksindstillinger. Sammenlign med tabellen ovenfor, og sæt dine gamle værdier ind igen: MONTØR → OPTÆNDING.",
  s6:"Også sod eller grov aske? Se på røgsugeren: MONTØR → SUGEINDSTILLINGER → BLÆSER YDELSE. Punktet ved 30 % er 500 o/min fra fabrikken. Det gav for lidt luft på én 15 kW kedel; 1200 o/min virkede dér. (Omdrejningspunkterne og fabriksværdien 500 o/min er fra software 1.40 på 10 og 15 kW typerne.)",
  s7:"Derefter indstillingerne i MONTØR → OPTÆNDING. Skriv den gamle værdi ned, ændr én indstilling, og hold øje med de næste 3 starter her. Begynd med tændbrændsel (TÆNDBRÆNDSEL).",
  s7_sync:"Monitorens indstillingssynkronisering er slået til: den sætter sin gemte værdi tilbage. Ændr værdien på siden i stedet, eller slå synkroniseringen fra.",
  s8:"Stadig dårligt? Tryk Kopiér optændingsrapport, og sæt den ind dér, hvor du spørger om hjælp.",
  copy:"Kopiér optændingsrapport", copied:"Kopieret", copy_note:"Rapporten indeholder dine starttidspunkter.", guide:"Guide: efter en softwareopdatering af kedlen", open:"Problemer med optænding? Se hvad du kan prøve",
  r_title:"Optændingsrapport (pillefyr-monitor {v})", r_set:"Indstillinger for optænding (nu / fabrik):", r_starts:"Seneste {n} starter (nyeste først):",
  r_none:"Ingen starter registreret endnu.", r_lit:"tændte, {a} min til flamme", r_lit0:"tændte", r_fail:"tændte ikke", r_fail_a:"tændte ikke (alarm {a})", r_stop:"stoppet",
  r_cfg:"tændbrændsel {a} s, max doser {b}", r_blind:"Aflæses ikke af monitoren: røgsugerens omdrejninger (BLÆSER YDELSE), servicemenuen.", r_marks:"* = ikke fabriksværdien, ! = værd at se på"}};
const lhT=(k,p)=>{ let s=(LH_TXT[LANG]||LH_TXT.en)[k]; if(s==null) s=k; for(const x in (p||{})) s=s.split("{"+x+"}").join(p[x]); return s; };
const LH_KW={0:"10 kW",1:"15 kW",2:"25 kW"};
let LH_OPEN=null;                 // the card: null = open by itself when lighting is slow or fails; true/false = the user's choice
// a setting's value as text: seconds, %, kg/h (the boiler's screen shows the fuel rate in kg/h)
const lhVal=(id,v)=>v==null?"–":id==="001"?fmt(v/1000,1)+" kg/h":id==="035"?fmt(v,1):fmt(v,0)+({"014":" s","015":" %","017":" s"}[id]||"");
const lhBad=h=>!!(h&&h.verdict&&(h.verdict.k==="slow"||h.verdict.k==="fails"));
function lhVerdict(h){
  const v=(h&&h.verdict)||{k:"few"};
  if(v.k==="fails") return lhT("v_fails",{a:v.failed,n:v.n});
  if(v.k==="slow") return lhT("v_slow",{a:fmt(v.min,1),n:v.n});
  if(v.k==="ok"&&v.min!=null) return lhT("v_ok",{a:fmt(v.min,1),n:v.n})+(v.failed===1?" "+lhT("v_ok1"):"");
  return lhT("v_few");
}
const lhBoiler=h=>lhT("boiler",{m:h.type!=null?LH_KW[h.type]:lhT("type_none"),v:h.sw||lhT("sw_none")});
// the inside of the card. c = the page's colours {warn, dim, line}, guide = the link to the guide section
function lhHTML(h,c,guide){
  if(!h) return "";
  const bad=lhBad(h);
  return `<details id="lhDet"${(LH_OPEN==null?bad:LH_OPEN)?" open":""}><summary style="cursor:pointer"><b id="lhTitle">${esc(lhT("title"))}</b> <span id="lhVerdict" style="color:${bad?c.warn:c.dim}">${esc(lhVerdict(h))}</span></summary>
    ${lhBody(h,c,guide)}</details>`;
}
// the inside of the card without its fold: the settings table, what to try, Copy lighting report (also used inside the
// Time to flame row of Burner health on /home)
function lhBody(h,c,guide){
  if(!h) return "";
  const rows=h.rows||[], any=rows.some(r=>r.now!=null), td=`padding:6px 8px 6px 0;border-bottom:1px solid ${c.line};text-align:left;vertical-align:top`;
  const notes=[...new Set(rows.filter(r=>r.mark&&r.mark!=="diff"&&r.mark!=="unused").map(r=>r.mark))];
  const nm=r=>esc(lhT("n_"+r.id))+(r.mark==="unused"?` <span style="color:${c.dim}">(${esc(lhT("m_unused"))})</span>`:"");
  const mk=r=>!r.mark||r.mark==="unused"?"":r.mark==="diff"?` <span title="${esc(lhT("m_diff"))}" style="color:${c.dim}">●</span>`:` <b style="color:${c.warn}">!</b>`;
  return `<p style="margin:12px 0 6px"><b>${esc(lhT("set_h"))}</b> <span style="color:${c.dim}">${esc(lhBoiler(h))}</span></p>
    ${any?`<div style="overflow-x:auto"><table id="lhTable" style="width:100%;border-collapse:collapse;font-size:14px"><tr>${["c_set","c_now","c_fac","c_ref"].map(k=>`<th style="${td};color:${c.dim};font-weight:600">${esc(lhT(k))}</th>`).join("")}</tr>
      ${rows.map(r=>`<tr data-id="${esc(r.id)}"${r.mark?` data-mark="${esc(r.mark)}"`:""}><td style="${td}">${nm(r)}</td><td style="${td};white-space:nowrap"><b>${esc(lhVal(r.id,r.now))}</b>${mk(r)}</td><td style="${td};white-space:nowrap">${esc(lhVal(r.id,r.fac))}</td><td style="${td};white-space:nowrap">${esc(lhVal(r.id,r.ref))}</td></tr>`).join("")}</table></div>
    ${notes.map(m=>`<p style="margin:8px 0 0;color:${c.warn}"><b>!</b> ${esc(lhT("m_"+m))}</p>`).join("")}
    <p style="margin:8px 0 0;color:${c.dim};font-size:13px">● ${esc(lhT("m_diff"))}. ${esc(lhT("fac_note"))}${h.sw&&h.sw_known!==false?"":" "+esc(lhT("sw_note"))}<br>${esc(lhT("blind"))}</p>`
    :`<p style="margin:0;color:${c.dim}">${esc(lhT("no_cfg"))}</p>`}
    <p style="margin:14px 0 4px"><b>${esc(lhT("try_h"))}</b></p>
    <ol id="lhSteps" style="margin:0;padding-left:22px;line-height:1.5">${[1,2,3,4,5,6,7,8].map(i=>`<li style="margin-top:4px">${esc(lhT("s"+i))}${i===7&&h.sync?" "+esc(lhT("s7_sync")):""}</li>`).join("")}</ol>
    <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:14px"><button type="button" class="btn sec" data-lh="copy">${esc(lhT("copy"))}</button>
      <a class="link" href="${esc(guide)}" target="_blank" rel="noopener">${esc(lhT("guide"))}</a></div>
    <p id="lhCopyNote" style="margin:6px 0 0;color:${c.dim};font-size:13px">${esc(lhT("copy_note"))}</p>
    <p style="margin:10px 0 0;color:${c.dim};font-size:13px">${esc(lhT("ro"))}</p>`;
}
// the plain text for the clipboard: boiler type, software, settings, the last starts, the monitor's version.
// Nothing personal: no names, addresses, topics, place or pellet batch names.
function lhReport(h,ver){
  const p=n=>String(n).padStart(2,"0"), when=e=>{ const d=new Date(e*1000); return p(d.getDate())+"/"+p(d.getMonth()+1)+" "+p(d.getHours())+":"+p(d.getMinutes()); };
  const L=[lhT("r_title",{v:ver||"?"}), lhBoiler(h), lhVerdict(h), "", lhT("r_set")];
  (h.rows||[]).forEach(r=>L.push("- "+lhT("n_"+r.id)+": "+lhVal(r.id,r.now)+" / "+lhVal(r.id,r.fac)+(r.mark==="unused"?"  ("+lhT("m_unused")+")":r.mark&&r.mark!=="diff"?"  !":r.mark?"  *":"")));
  if((h.rows||[]).some(r=>r.mark&&r.mark!=="unused")) L.push(lhT("r_marks"));
  L.push(lhT("r_blind"));
  if(!(h.sw&&h.sw_known!==false)) L.push(lhT("sw_note"));
  L.push("");
  const st=h.starts||[];
  L.push(st.length?lhT("r_starts",{n:st.length}):lhT("r_none"));
  st.forEach((s,i)=>{ const res=s.result==="ok"?(s.flame_min!=null?lhT("r_lit",{a:fmt(s.flame_min,1)}):lhT("r_lit0")):
      s.result==="failed"?(s.alarm?lhT("r_fail_a",{a:s.alarm}):lhT("r_fail")):lhT("r_stop");
    L.push((i+1)+". "+(s.epoch?when(s.epoch)+"  ":"")+res+(s.start_fuel!=null&&s.max_doses!=null?"  ("+lhT("r_cfg",{a:s.start_fuel,b:s.max_doses})+")":"")); });
  return L.join("\n");
}
// copy text; the pages are plain http on the home network, so there is a fallback for browsers without clipboard access
function lhCopy(txt,btn){
  const done=()=>{ if(!btn) return; btn.textContent=lhT("copied"); setTimeout(()=>{ btn.textContent=lhT("copy"); },2000); };
  if(navigator.clipboard&&window.isSecureContext){ navigator.clipboard.writeText(txt).then(done).catch(()=>{}); return; }
  const ta=document.createElement("textarea"); ta.value=txt; ta.style.position="fixed"; ta.style.opacity="0";
  document.body.appendChild(ta); ta.select(); try{ if(document.execCommand("copy")) done(); }catch(e){} ta.remove();
}
document.addEventListener("toggle",e=>{ if(e.target&&e.target.id==="lhDet") LH_OPEN=e.target.open; },true);

// ---- Burner health details: what the rows of the Burner health checklist open (/home History) and the fire
// rescue words of the /classic suggestions. READ ONLY: the server sends codes, numbers and times; the words are here.
const BH_TXT={
 en:{bh_rescue:"Fire rescues", bh_rescue_none:"None in the last {d} days that burned.",
  bh_rescue_one:"1 in the last {d} days that burned. The boiler relit it.", bh_rescue_many:"{a} in the last {d} days that burned.",
  bh_rescue_fail:"{f} of {a} in the last {d} days that burned did not relight.",
  rs_one:"Fire rescue on {w}: the flame went out while burning, and {o}.",
  rs_many:"{n} fire rescues in the last {d} days that burned (since {from}). The flame went out while burning. The last ones:",
  rs_ok:"the boiler relit it", rs_failed:"it did not relight: the boiler stopped with an alarm", rs_stopped:"the boiler stopped",
  rs_null:"the day ended before it was over", rs_when_none:"time not saved",
  rs_advice:"Check the pellet feed (silo, pellets stuck, auger) and clean the flame sensor.",
  rs_period:"“The last {d} days that burned” = the days of this list that had burning; days without burning are skipped.",
  rs_list_h:"The newest fire rescues", rs_none:"No fire rescues in the saved days.",
  st_h:"The last {n} starts", st_none:"No starts recorded yet.", st_by:"started by {s}", st_flame:"flame after {a} min",
  st_full:"full heat after {a} min", st_off:"off {a} h before", st_r_ok:"Lit", st_r_failed:"Did not light", st_r_stopped:"Stopped",
  st_r_resumed:"Went on burning", st_r_relight:"Relit after a pause", st_alarm:"alarm {a}",
  st_resumed_note:"Went on burning = the fire was still there, so there was no lighting. It is left out of every average.",
  st_cfg_h:"Lighting settings compared", st_cfg_n:"{n} starts, {ok} lit", st_cfg_flame:"to flame about {a} min",
  st_cfg_full:"to full heat about {a} min", st_cfg_now:"In force now", st_cfg_none:"The settings were the same for every start.",
  st_cfg_note:"Only the settings that differ between starts. Typical = the middle value of the starts that lit.",
  fs_h:"Is the flame sensor dirty?", fs_band_low:"Low power (under 45 %)", fs_band_mid:"Middle power (45–70 %)", fs_band_high:"High power (70 % and up)",
  fs_learn:"Learning: {m} of {nm} min steady burning, {b} of {nb} burns.", fs_none:"No steady burning at this power yet.",
  fs_ready:"Usual {a}, now {c}.", fs_drop:"{p} % less light than usual.", fs_same:"As usual.",
  fs_how:"It learns from steady burning: 5-minute stretches at an even power, from 10 minutes into a burn. A power level is ready after {nm} minutes spread over {nb} burns, so short burns add up. Readings from the saved history count too.",
  fs_clean:"I cleaned the flame sensor", fs_clean_q:"Start learning the flame sensor’s normal level again from now?",
  fs_cleaned:"Learning again since {d}.", fs_nofield:"No flame reading is chosen (classic page → Analysis).",
  cc_fan:"Fan at {p} %", cc_field:"Setting {f}"},
 da:{bh_rescue:"Redder ilden", bh_rescue_none:"Ingen i de seneste {d} dage med fyring.",
  bh_rescue_one:"1 i de seneste {d} dage med fyring. Kedlen tændte igen.", bh_rescue_many:"{a} i de seneste {d} dage med fyring.",
  bh_rescue_fail:"{f} af {a} i de seneste {d} dage med fyring tændte ikke igen.",
  rs_one:"Redder ilden {w}: flammen gik ud under fyring, og {o}.",
  rs_many:"Redder ilden {n} gange i de seneste {d} dage med fyring (siden {from}). Flammen gik ud under fyring. De seneste:",
  rs_ok:"kedlen tændte den igen", rs_failed:"den tændte ikke igen: kedlen stoppede med en alarm", rs_stopped:"kedlen stoppede",
  rs_null:"dagen sluttede, før den var ovre", rs_when_none:"tidspunkt ikke gemt",
  rs_advice:"Tjek pilletilførslen (silo, piller der hænger fast, snegl) og rens flammeføleren.",
  rs_period:"“De seneste {d} dage med fyring” = de dage på listen, hvor der blev fyret; dage uden fyring springes over.",
  rs_list_h:"De nyeste gange", rs_none:"Ingen gange i de gemte dage.",
  st_h:"De seneste {n} starter", st_none:"Ingen starter registreret endnu.", st_by:"startet af {s}", st_flame:"flamme efter {a} min",
  st_full:"fuld varme efter {a} min", st_off:"slukket {a} t før", st_r_ok:"Tændte", st_r_failed:"Tændte ikke", st_r_stopped:"Stoppet",
  st_r_resumed:"Brændte videre", st_r_relight:"Tændt igen efter pause", st_alarm:"alarm {a}",
  st_resumed_note:"Brændte videre = ilden var der stadig, så der var ingen optænding. Den tælles ikke med i gennemsnit.",
  st_cfg_h:"Indstillinger for optænding sammenlignet", st_cfg_n:"{n} starter, {ok} tændte", st_cfg_flame:"til flamme ca. {a} min",
  st_cfg_full:"til fuld varme ca. {a} min", st_cfg_now:"Gælder nu", st_cfg_none:"Indstillingerne var de samme ved alle starter.",
  st_cfg_note:"Kun de indstillinger, der er forskellige fra start til start. Typisk = midterværdien af de starter, der tændte.",
  fs_h:"Er flammeføleren snavset?", fs_band_low:"Lav ydelse (under 45 %)", fs_band_mid:"Mellem ydelse (45–70 %)", fs_band_high:"Høj ydelse (70 % og op)",
  fs_learn:"Lærer: {m} af {nm} min stabil fyring, {b} af {nb} fyringer.", fs_none:"Ingen stabil fyring ved denne ydelse endnu.",
  fs_ready:"Normalt {a}, nu {c}.", fs_drop:"{p} % mindre lys end normalt.", fs_same:"Som normalt.",
  fs_how:"Den lærer af stabil fyring: 5 minutter ad gangen ved jævn ydelse, fra 10 minutter inde i en fyring. Et ydelsesniveau er klar efter {nm} minutter fordelt på {nb} fyringer, så korte fyringer lægges sammen. Målinger fra den gemte historik tæller også med.",
  fs_clean:"Jeg har renset flammeføleren", fs_clean_q:"Lær flammefølerens normale niveau igen fra nu?",
  fs_cleaned:"Lærer igen siden {d}.", fs_nofield:"Der er ikke valgt en flammemåling (klassisk side → Analyse).",
  cc_fan:"Røgsuger ved {p} %", cc_field:"Indstilling {f}"}};
const bhT=(k,p)=>{ let s=(BH_TXT[LANG]||BH_TXT.en)[k]; if(s==null) s=k; for(const x in (p||{})) s=s.split("{"+x+"}").join(p[x]); return s; };
// "Tue 24 Nov 13:42" (Danish: "tirs. 24. nov. 13:42")
const bhWhen=ep=>ep?new Date(ep*1000).toLocaleDateString(LOC(),{weekday:"short",day:"numeric",month:"short"})+" "+clock(ep):bhT("rs_when_none");
// the boiler's own settings in plain words (EN/DA): the settings changes and the lighting settings compared
const CC_NAMES={en:{"000":"Set point (°C)","001":"Fuel (g per hour)","002":"Air clean interval (min)","003":"Air clean time (s)",
  "004":"Ash clean interval (g)","005":"Minimum mode stop at (%)","006":"Minimum mode pause (h)","007":"Boost 1","008":"Boost 2",
  "009":"Auger calibration (g)","010":"Silo capacity (kg)","012":"Stepping temp. limit (°C)","013":"Pump start temperature",
  "014":"Start fuel (s)","015":"Ignition air (%)","016":"Warm-up (s)","017":"Ignition timeout (s)","018":"Max doses",
  "019":"Top-up dose (s)","020":"Top-up interval (s)","031":"Max power (%)","033":"Boiler type","034":"Dose size",
  "035":"PID P","036":"PID I","037":"PID D","038":"Software version","041":"Hardware version"},
 da:{"000":"Kedeltemperatur (°C)","001":"Brændsel (g pr. time)","002":"Luftrens interval (min)","003":"Luftrens sugetid (s)",
  "004":"Askerens interval (g)","005":"Minimumdrift stop ved (%)","006":"Minimumdrift pause (t)","007":"Boost 1","008":"Boost 2",
  "009":"Snegl afvejning (g)","010":"Silo kapacitet (kg)","012":"Stepping temp. grænse (°C)","013":"Pumpe starttemperatur",
  "014":"Tændbrændsel (s)","015":"Tænd luft (%)","016":"Opvarmning (s)","017":"Optænding timeout (s)","018":"Max doser",
  "019":"Suplering (s)","020":"Interval mellem doser (s)","031":"Max ydelse (%)","033":"Kedeltype","034":"Dosis str.",
  "035":"PID P","036":"PID I","037":"PID D","038":"Softwareversion","041":"Hardwareversion"}};
const ccName=f=>{ const n=(CC_NAMES[LANG]||CC_NAMES.en)[f]; if(n) return n+" ("+f+")";
  const k=+f; return k>=21&&k<=30?bhT("cc_fan",{p:(k-20)*10})+" ("+f+")":bhT("cc_field",{f:f}); };
// name and unit apart: "Start fuel (s)" -> ["Start fuel","s"]
const ccParts=f=>{ const n=(CC_NAMES[LANG]||CC_NAMES.en)[f]; if(!n){ const k=+f; return [k>=21&&k<=30?bhT("cc_fan",{p:(k-20)*10}):bhT("cc_field",{f:f}),k>=21&&k<=30?"rpm":""]; }
  const m=n.match(/^(.*?)\s*\(([^)]*)\)$/); return m?[m[1],m[2]]:[n,""]; };
const ccNum=x=>typeof x==="number"?fmt(x,Number.isInteger(x)?0:(Math.abs(x)<1?3:1)):String(x==null?"–":x);
// one settings change as "Start fuel 60 → 40 s"
const ccChange=r=>{ const [n,u]=ccParts(r.field); return n+" "+ccNum(r.old)+" → "+ccNum(r.new)+(u?" "+u:""); };

// ---- fire rescues: [[epoch, "ok" / "failed" / "stopped" / null], ...] newest first
const rsOutcome=o=>bhT("rs_"+(o||"null"));
// the /classic suggestion and the /home detail: when, and whether the boiler relit it. p = the server's numbers
// (n, days, from, last, ok), "this period" said in words
function rescText(p){
  const last=(p&&p.last)||[], n=(p&&p.n)||0;
  if(n===1&&last.length) return bhT("rs_one",{w:bhWhen(last[0][0]),o:rsOutcome(last[0][1])})+" "+bhT("rs_advice");
  const from=p&&p.from?dmon(new Date(p.from+"T12:00").getTime()/1000):"—";
  return bhT("rs_many",{n,d:(p&&p.days)||"?",from})+" "+last.map(x=>bhWhen(x[0])+" ("+rsOutcome(x[1])+")").join("; ")+". "+bhT("rs_advice");
}
// the list of times for the Burner health detail. c = the page's colours {warn, dim, line}
function rescListHTML(p,c){
  const L=(p&&p.list)||[];
  if(!L.length) return `<p style="margin:0;color:${c.dim}">${esc(bhT("rs_none"))}</p>`;
  return `<p style="margin:0 0 6px"><b>${esc(bhT("rs_list_h"))}</b></p><div id="rsList">${L.map(x=>`<div style="padding:7px 0;border-top:1px solid ${c.line}">
    <b style="font-weight:600">${esc(bhWhen(x[0]))}</b><br><span style="color:${x[1]==="ok"?c.dim:c.warn}">${esc(sentenceCase(rsOutcome(x[1])))}</span></div>`).join("")}</div>
    <p style="margin:8px 0 0;color:${c.dim};font-size:13px">${esc(bhT("rs_advice"))} ${esc(bhT("rs_period",{d:p.d||0}))}</p>`;
}
const sentenceCase=s=>s?s.charAt(0).toUpperCase()+s.slice(1):s;

// ---- the last starts with the settings changes between them, and the lighting settings compared (api/ignitions:
// records, by_settings, cfg_changes). Stacked rows, so it fits a phone. by = the page's "started by" words for a record.
function startsHTML(ign,c,by,n){
  n=n||10;
  const recs=((ign&&ign.records)||[]).slice(-n).reverse(), ch=((ign&&ign.cfg_changes)||[]).slice();
  if(!recs.length) return `<p style="margin:0;color:${c.dim}">${esc(bhT("st_none"))}</p>`;
  const mark=rows=>{ const t0=rows[0].time; return `<div class="stMark" style="padding:6px 0;border-top:1px solid ${c.line};color:${c.warn};font-size:14px">▲ ${esc(bhWhen(t0))} ${esc(rows.map(ccChange).join(", "))}</div>`; };
  // the changes after the newest start go first, then between each start and the one before it
  const take=(a,b)=>{ const got=ch.filter(r=>r.time>=a&&r.time<b); if(!got.length) return "";
    const g={}; got.forEach(r=>{ const k=Math.round(r.time/60); (g[k]=g[k]||[]).push(r); });
    return Object.keys(g).sort((x,y)=>y-x).map(k=>mark(g[k])).join(""); };
  let h=`<p style="margin:0 0 4px"><b>${esc(bhT("st_h",{n:recs.length}))}</b></p><div id="stList">`, anyRes=false;
  recs.forEach((r,i)=>{
    h+=take(r.epoch, i?recs[i-1].epoch:Infinity);
    const res=r.resumed?"resumed":r.relight&&r.result==="ok"?"relight":r.result; if(r.resumed) anyRes=true;
    const col=res==="ok"||res==="relight"?c.ok||c.dim:res==="failed"?c.warn:c.dim, s=by?by(r):"";
    const bits=[s?bhT("st_by",{s}):"", r.flame_min!=null&&!r.resumed?bhT("st_flame",{a:fmt(r.flame_min,1)}):"",
      r.result==="ok"&&r.minutes!=null?bhT("st_full",{a:fmt(r.minutes,1)}):"", r.off_h!=null?bhT("st_off",{a:fmt(r.off_h,1)}):""].filter(Boolean);
    h+=`<div class="stRow" data-res="${esc(res)}" style="padding:7px 0;border-top:1px solid ${c.line}"><b style="font-weight:600">${esc(bhWhen(r.epoch))}</b> · <span style="color:${col}">${esc(bhT("st_r_"+res))}${r.alarm?" ("+esc(bhT("st_alarm",{a:r.alarm}))+")":""}</span>
      ${bits.length?`<br><span style="color:${c.dim};font-size:14px">${esc(bits.join(" · "))}</span>`:""}</div>`; });
  h+=`</div>`;                                       // changes before the oldest start shown are left out
  if(anyRes) h+=`<p style="margin:6px 0 0;color:${c.dim};font-size:13px">${esc(bhT("st_resumed_note"))}</p>`;
  return h+cfgCmpHTML(ign,c);
}
// the lighting settings compared: per combination how many starts, how many lit, typical time to flame and to full heat
function cfgCmpHTML(ign,c){
  const S=(ign&&ign.by_settings)||[];
  let h=`<p style="margin:14px 0 4px"><b>${esc(bhT("st_cfg_h"))}</b></p>`;
  if(!S.length) h+=`<p style="margin:0;color:${c.dim}">—</p>`;
  h+=`<div id="cfgCmp">`+S.map(s=>{ const cf=s.cfg||{}, name=Object.keys(cf).map(k=>{ const [nm,u]=ccParts(k); return nm+" "+ccNum(cf[k])+(u?" "+u:""); }).join(", ")||"—";
    return `<div class="cfgRow"${s.now?' data-now="1"':""} style="padding:7px 0;border-top:1px solid ${c.line}"><b style="font-weight:600">${esc(name)}</b>${s.now?` <span style="color:${c.ok||c.dim};font-weight:600">● ${esc(bhT("st_cfg_now"))}</span>`:""}
      <br><span style="color:${c.dim};font-size:14px">${esc([bhT("st_cfg_n",{n:s.n,ok:s.ok}), s.flame_min!=null?bhT("st_cfg_flame",{a:fmt(s.flame_min,1)}):"", s.median_min!=null?bhT("st_cfg_full",{a:fmt(s.median_min,1)}):""].filter(Boolean).join(" · "))}</span></div>`; }).join("")+`</div>`;
  if(S.length===1) h+=`<p style="margin:6px 0 0;color:${c.dim};font-size:13px">${esc(bhT("st_cfg_none"))}</p>`;
  return h+`<p style="margin:6px 0 0;color:${c.dim};font-size:13px">${esc(bhT("st_cfg_note"))}</p>`;
}

// ---- the flame sensor per power band (api/optimiser "lux"): learned so far, or usual against now
function luxHTML(lx,c){
  if(!lx) return "";
  if(lx.status==="no_field") return `<p style="margin:0;color:${c.dim}">${esc(bhT("fs_nofield"))}</p>`;
  const B=lx.bands||{}, nm=lx.need_min||30, nb=lx.need_burns||3;
  const row=k=>{ const b=B[k]||{}; let s, warn=false;
    if(!b.n) s=bhT("fs_none");
    else if(b.learning) s=bhT("fs_learn",{m:Math.min(b.min||0,nm),nm,b:Math.min(b.burns||0,nb),nb});
    else { warn=(b.drop_pct||0)>=10; s=bhT("fs_ready",{a:fmt(b.baseline,0),c:fmt(b.current,0)})+" "+(warn?bhT("fs_drop",{p:fmt(b.drop_pct,0)}):bhT("fs_same")); }
    return `<div class="fsRow" data-band="${k}" style="padding:7px 0;border-top:1px solid ${c.line}"><b style="font-weight:600">${esc(bhT("fs_band_"+k))}</b><br><span style="color:${warn?c.warn:c.dim};font-size:14px">${esc(s)}</span></div>`; };
  return `<p style="margin:0 0 4px"><b>${esc(bhT("fs_h"))}</b></p><div id="fsBands">${["low","mid","high"].map(row).join("")}</div>
    <p style="margin:8px 0 0;color:${c.dim};font-size:13px">${esc(bhT("fs_how",{nm,nb}))}${lx.reset_epoch?" "+esc(bhT("fs_cleaned",{d:bhWhen(lx.reset_epoch)})):""}</p>`;
}

// ---- software update card (a Raspberry Pi with the updater, pi/pelletpi-update.sh). The monitor says which state
// it is (api/update "view", made by burner_ext.update_info); here are the words and the buttons for both pages.
const UPD_TXT={
 en:{title:"Software update", ver:"Version {current}", open:"Update…",
  auto_on:"Automatic updates at night: on", auto_off:"Automatic updates at night: off", btn_auto_off:"Turn off", btn_auto_on:"Turn on",
  btn_check:"Check for update", btn_install:"Install {latest}", btn_retry:"Try {latest} again", btn_back:"Go back to {prev}",
  note:"A new version is installed when the boiler is off or waiting. If it does not work here, the Pi goes back to the old version by itself. Settings and history are kept.",
  back_confirm:"Go back to {prev}? Settings and history are kept.", send_fail:"Could not ask the Pi. Try again.",
  asked:"Checking…", no_updater:"The Pi’s updater did not answer. Try again in a minute.",
  old:"This Pi has the first updater: it updates, but shows no progress here. See “Updating” in the guide.",
  ph_check:"Checking for a new version…",
  ph_download:"New version {to} found. Downloading it now (5–15 minutes). The boiler runs as usual.",
  ph_waiting:"{to} is downloaded. It is installed as soon as the boiler is off or waiting. Internet is no longer needed.",
  ph_install:"Installing {to} and testing it with your boiler (up to 7 minutes). The page may be away for a moment.",
  ph_back:"Going back to {to}…",
  uptodate:"You have the newest version.",
  offline:"No internet. Share internet from your phone (hotspot) and try again.",
  no_release:"There is no release to install yet.",
  no_repo:"This Pi has no update address, so it cannot update. See “Updating” in the guide.",
  updated:"Now on {current}.",
  rolled_back:"{to} did not work here – went back to {current}.",
  why_no_answer:"The new version did not start.", why_status:"The new version did not show the boiler’s data.",
  why_settings:"The new version could not read your settings.", why_boiler:"The boiler did not connect again.",
  why_swap:"Docker did not start it.",
  build_failed:"Could not download {to}: the internet was lost, or the Pi’s card is full. Still on {current}.",
  disk_full:"Not enough free space on the Pi’s card for {to}. Still on {current}.",
  busy:"{to} was downloaded, but the boiler stayed busy. Press again when the boiler is off or waiting.",
  later:"New version {latest} is out. It is installed tonight when the boiler is off or waiting, or now with the button.",
  available:"New version {latest} is out.",
  bad_skipped:"{latest} did not work here before, so it is skipped.", left_skipped:"You went back from {latest} earlier, so it is not installed by itself.",
  went_back:"Went back to {current}. {from} is not installed again by itself.",
  no_previous:"There is no older version to go back to.",
  back_busy:"Going back is only done when the boiler is off or waiting. Try again then.",
  back_failed:"The older version did not start. Still on {current}.",
  interrupted:"The update was interrupted. The version that was running is kept. Try again.",
  local_changes:"Local changes in the program folder; the update was not installed. Still on {current}.",
  unknown_base:"New version {latest} is out. This Pi runs a copy that is not a release, so it is not installed by itself. The button installs it if your copy is older.",
  tidy_wait:"An update was interrupted. The old version’s files are back; it starts again when the boiler is off or waiting.",
  error:"The update stopped with an error. Still on {current}."},
 da:{title:"Opdatering af programmet", ver:"Version {current}", open:"Opdatering…",
  auto_on:"Automatiske opdateringer om natten: til", auto_off:"Automatiske opdateringer om natten: fra", btn_auto_off:"Slå fra", btn_auto_on:"Slå til",
  btn_check:"Søg efter opdatering", btn_install:"Installér {latest}", btn_retry:"Prøv {latest} igen", btn_back:"Gå tilbage til {prev}",
  note:"En ny version installeres, når kedlen er slukket eller venter. Virker den ikke her, går Pi’en selv tilbage til den gamle version. Indstillinger og historik bevares.",
  back_confirm:"Gå tilbage til {prev}? Indstillinger og historik bevares.", send_fail:"Kunne ikke spørge Pi’en. Prøv igen.",
  asked:"Søger…", no_updater:"Pi’ens opdatering svarede ikke. Prøv igen om et minut.",
  old:"Denne Pi har den første opdatering: den opdaterer, men viser ikke forløbet her. Se “Opdatering” i guiden.",
  ph_check:"Søger efter en ny version…",
  ph_download:"Ny version {to} fundet. Den hentes nu (5–15 minutter). Kedlen kører som normalt.",
  ph_waiting:"{to} er hentet. Den installeres, så snart kedlen er slukket eller venter. Internet er ikke længere nødvendigt.",
  ph_install:"Installerer {to} og afprøver den med din kedel (op til 7 minutter). Siden kan være væk et øjeblik.",
  ph_back:"Går tilbage til {to}…",
  uptodate:"Du har den nyeste version.",
  offline:"Intet internet. Del internet fra din telefon (hotspot), og prøv igen.",
  no_release:"Der er endnu ingen udgave at installere.",
  no_repo:"Denne Pi har ingen opdateringsadresse og kan derfor ikke opdatere. Se “Opdatering” i guiden.",
  updated:"Nu på {current}.",
  rolled_back:"{to} virkede ikke her – gik tilbage til {current}.",
  why_no_answer:"Den nye version startede ikke.", why_status:"Den nye version viste ikke kedlens data.",
  why_settings:"Den nye version kunne ikke læse dine indstillinger.", why_boiler:"Kedlen fik ikke forbindelse igen.",
  why_swap:"Docker startede den ikke.",
  build_failed:"Kunne ikke hente {to}: internettet forsvandt, eller Pi’ens kort er fuldt. Stadig på {current}.",
  disk_full:"Ikke nok fri plads på Pi’ens kort til {to}. Stadig på {current}.",
  busy:"{to} blev hentet, men kedlen var hele tiden i gang. Tryk igen, når kedlen er slukket eller venter.",
  later:"Ny version {latest} er ude. Den installeres i nat, når kedlen er slukket eller venter, eller nu med knappen.",
  available:"Ny version {latest} er ude.",
  bad_skipped:"{latest} virkede ikke her før og springes derfor over.", left_skipped:"Du gik tilbage fra {latest} tidligere, så den installeres ikke af sig selv.",
  went_back:"Gik tilbage til {current}. {from} installeres ikke igen af sig selv.",
  no_previous:"Der er ingen ældre version at gå tilbage til.",
  back_busy:"Der gås kun tilbage, når kedlen er slukket eller venter. Prøv igen da.",
  back_failed:"Den ældre version startede ikke. Stadig på {current}.",
  interrupted:"Opdateringen blev afbrudt. Den version, der kørte, er beholdt. Prøv igen.",
  local_changes:"Lokale ændringer i programmappen; opdateringen blev ikke installeret. Stadig på {current}.",
  unknown_base:"Ny version {latest} er ude. Denne Pi kører en kopi, der ikke er en udgave, så den installeres ikke af sig selv. Knappen installerer den, hvis din kopi er ældre.",
  tidy_wait:"En opdatering blev afbrudt. Den gamle versions filer er tilbage; den starter igen, når kedlen er slukket eller venter.",
  error:"Opdateringen stoppede med en fejl. Stadig på {current}."}};
const updT=(k,v)=>String((UPD_TXT[LANG]||UPD_TXT.en)[k]||"").replace(/\{(\w+)\}/g,(m,x)=>v&&v[x]!=null?v[x]:"");
let UPD=null, UPD_BUSY=false, UPD_TIMER=0, UPD_WATCH=0, UPD_KEY0=null;       // the last api/update answer (null = no updater here); still at work?
// the words for the state; "" when there is no updater on this machine
function updStateText(){ const v=UPD&&UPD.view; if(!v) return ""; return updT(v.key,v)+(v.why?" "+updT("why_"+v.why,v):""); }
// the inside of the card: version, state, buttons (data-upd = check / retry / back), the short rule.
// cls = the page's own button classes [main, second]
function updCardHTML(cls){ const v=UPD&&UPD.view; if(!v) return "";
  const b=(what,label,c)=>`<button type="button" class="${c}" data-upd="${what}">${esc(label)}</button>`;
  let h=`<div style="font-weight:700">${esc(updT("title"))}</div><div style="margin-top:4px;opacity:.75">${esc(updT("ver",v))}</div>`+
    `<div id="updState" data-key="${esc(v.key)}" aria-live="polite" style="margin-top:10px;font-weight:600">${v.busy?"⏳ ":""}${esc(updStateText())}</div>`+
    `<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:14px">`;
  if(v.btn) h+=b(v.btn==="retry"?"retry":v.btn==="install"?"install":"check",updT("btn_"+v.btn,v),cls[0]);
  if(v.back) h+=b("back",updT("btn_back",{prev:v.back}),cls[1]);
  h+=`</div>`;
  if(!v.busy) h+=`<div style="margin-top:14px;display:flex;flex-wrap:wrap;align-items:center;gap:10px">`+
    `<span>${esc(updT(v.auto?"auto_on":"auto_off"))}</span>`+b(v.auto?"auto-off":"auto-on",updT(v.auto?"btn_auto_off":"btn_auto_on"),cls[1])+`</div>`;
  return h+`<div style="margin-top:12px;font-size:13px;opacity:.75">${esc(updT("note"))}</div>`; }
// Reads the state and draws it (the page's updDraw). While the updater works it asks again every 3 s; the monitor
// restarts in the middle of an update, so a failed read keeps the last state and tries again.
async function updLoad(){ clearTimeout(UPD_TIMER);
  let ok=true;
  try{ const {j}=await api("api/update"); UPD=j&&j.current&&j.view?j:null; }catch(e){ ok=false; }
  if(ok){ const v=UPD&&UPD.view, busy=!!(v&&v.busy);
    // the version changed under the page: load the page of the version that runs now (it shows the same result)
    // also right after a press (UPD_WATCH): the updater may pick the request up a moment after the first read
    const watching=Date.now()<UPD_WATCH;
    if(!busy&&v&&["updated","rolled_back","went_back"].includes(v.key)&&(UPD_BUSY||(watching&&v.key!==UPD_KEY0))){
      UPD_WATCH=0; setTimeout(()=>location.reload(),4000); }
    UPD_BUSY=busy; }
  updDraw();
  if(UPD_BUSY||Date.now()<UPD_WATCH) UPD_TIMER=setTimeout(updLoad,3000); }
// a button was pressed: what = check / retry / back. Only that word is sent. -> true when the Pi took the request
async function updAsk(what){ let ok=false;
  UPD_KEY0=UPD&&UPD.view?UPD.view.key:null; UPD_WATCH=Date.now()+120000;   // keep reading for 2 min after a press
  try{ ok=(await api("api/update/"+what,{})).ok; }catch(e){}
  await updLoad(); return ok; }

// ---- Two graphs: "Did the change help?" (api/optimiser "change") and "Pellets and the weather" (api/optimiser "wx").
// READ ONLY. The server sends codes and numbers (burner_features.change_help / weather_fit); the words are here.
// kg only, no prices. c = the page's colours and its card header: {ink, dim, grid, before, after, ok, warn, okBg, warnBg,
// nBg, accBg, acc, bar, bar2, card, head:(title, sub)=>html}
const GR_TXT={
 en:{ch_h:"Did the change help?", ch_sub:"Each dot is one lighting · the same boiler, before and after",
  ch_better:"Yes: the flame now comes {d} min sooner", ch_worse:"No: the flame now comes {d} min later",
  ch_same:"No clear change in time to flame", ch_few:"Too few starts since the change to say ({n} of {need})",
  ch_mixed:"Mixed: the flame comes {d} min sooner, but {a} of {na} starts did not light", ch_failed:"Worse: {a} of {na} starts did not light",
  ch_few_before:"Too few starts before the change to compare", ch_batch:"The pellet batch changed too, so it cannot say",
  ch_what:"Changed {d}: {p}.", ch_n:"{b} starts before, {a} after.", ch_many:"Several things changed at once: the graph cannot tell which one did it.",
  ch_batch_note:"A new pellet batch lights differently, so a settings change cannot be judged across it.",
  ch_p_batch:"new pellet batch “{n}”", ch_p_clean:"burner cleaned", ch_p_cleaned:"Cleaned: {w}", cl_burner:"burner", cl_fan:"fan", cl_sensor:"flame sensor", ch_pick:"Change",
  ch_band:"Only lightings with the boiler at {a}–{b} °C at start are compared ({n} others left out). Relights and starts that went on burning are never counted.",
  ch_ax:"minutes to flame", ch_mark:"changed", ch_usual:"usual {a} min",
  c_m:"", c_b:"Before", c_a:"After", c_v:"Verdict",
  r_flame:"Time to flame", r_full:"Time to full heat", r_failed:"Starts that did not light", r_power_flame:"Flame at full power",
  r_short:"Short burns (under 30 min)", r_wx:"Pellets against the weather", of:"{a} of {n}", u_wx:"{a} kg at {t} °C",
  v_better:"better", v_worse:"worse", v_same:"the same", v_few:"too few to say", v_batch:"no verdict (new pellets)",
  v_learning:"learning: {n} of {need} days",
  ch_how:"Better or worse only with at least {need} starts (or days) on each side, when the middle halves of the two sides do not overlap. Medians, so one odd start does not decide it. Starts that did not light and short burns: at least 30 points apart and 3 more.",
  wx_h:"Pellets and the weather", wx_sub:"Each dot is one full day (06:00 to 06:00) · the line is your house",
  wx_learn:"Learning: {n} of {need} days", wx_spread:"Learning: the days so far had too alike weather ({s} °C apart, needs {need} °C).",
  wx_fit:"Learning: the days do not follow the weather clearly enough yet.", wx_flat:"Until then: about {a} kg a day on average.",
  wx_off:"Needs the outside temperature: Settings → Outside temperature.",
  wx_learn_how:"A day counts with at least 23 h recorded and some burning. A heating plan change starts again.",
  wx_next:"Next {n} days: about {a} kg", wx_next_r:"Next {n} days: roughly {a} kg", wx_last:"Pellets at home last to about {d}.", wx_buy:"Buy by {d}.",
  wx_at0:"At {t} °C about {a} kg a day", wx_yday:"yesterday", wx_young:"Still a young line: the buy-by date uses the plain average until {n} days are learned.",
  wx_ax_x:"outside, average of the day", wx_ax_y:"kg a day", wx_week:"The coming week",
  wx_week_note:"kg a day from the weather forecast · ±{a} kg over the week", wx_nofc:"No weather forecast yet.",
  wx_foot:"Learned from {n} days. Most days are within ±{b} kg of the line. Sun, wind and hot water use move single days."},
 da:{ch_h:"Hjalp ændringen?", ch_sub:"Hver prik er én optænding · samme kedel, før og efter",
  ch_better:"Ja: flammen kommer nu {d} min hurtigere", ch_worse:"Nej: flammen kommer nu {d} min senere",
  ch_same:"Ingen klar forskel i tid til flamme", ch_few:"For få starter siden ændringen til at sige noget ({n} af {need})",
  ch_mixed:"Blandet: flammen kommer {d} min hurtigere, men {a} af {na} starter tændte ikke", ch_failed:"Værre: {a} af {na} starter tændte ikke",
  ch_few_before:"For få starter før ændringen til at sammenligne", ch_batch:"Pillepartiet skiftede også, så det kan ikke siges",
  ch_what:"Ændret {d}: {p}.", ch_n:"{b} starter før, {a} efter.", ch_many:"Flere ting blev ændret på én gang: grafen kan ikke sige, hvilken der gjorde det.",
  ch_batch_note:"Et nyt pilleparti tænder anderledes, så en ændret indstilling kan ikke bedømmes hen over det.",
  ch_p_batch:"nyt pilleparti “{n}”", ch_p_clean:"brænder renset", ch_p_cleaned:"Renset: {w}", cl_burner:"brænder", cl_fan:"røgsuger", cl_sensor:"flammeføler", ch_pick:"Ændring",
  ch_band:"Kun optændinger med kedlen på {a}–{b} °C ved start sammenlignes ({n} andre udeladt). Genoptændinger og starter, der brændte videre, tælles aldrig med.",
  ch_ax:"minutter til flamme", ch_mark:"ændret", ch_usual:"normalt {a} min",
  c_m:"", c_b:"Før", c_a:"Efter", c_v:"Dom",
  r_flame:"Tid til flamme", r_full:"Tid til fuld varme", r_failed:"Starter, der ikke tændte", r_power_flame:"Flamme ved fuld ydelse",
  r_short:"Korte fyringer (under 30 min)", r_wx:"Piller i forhold til vejret", of:"{a} af {n}", u_wx:"{a} kg ved {t} °C",
  v_better:"bedre", v_worse:"værre", v_same:"det samme", v_few:"for få til at sige", v_batch:"ingen dom (nye piller)",
  v_learning:"lærer: {n} af {need} dage",
  ch_how:"Bedre eller værre kun med mindst {need} starter (eller dage) på hver side, når de midterste halvdele af de to sider ikke overlapper. Medianer, så én skæv start ikke afgør det. Starter, der ikke tændte, og korte fyringer: mindst 30 procentpoint og 3 flere.",
  wx_h:"Piller og vejret", wx_sub:"Hver prik er ét helt døgn (06:00 til 06:00) · linjen er dit hus",
  wx_learn:"Lærer: {n} af {need} dage", wx_spread:"Lærer: dagene indtil nu havde for ens vejr ({s} °C imellem, skal være {need} °C).",
  wx_fit:"Lærer: dagene følger endnu ikke vejret tydeligt nok.", wx_flat:"Indtil da: ca. {a} kg om dagen i snit.",
  wx_off:"Kræver udetemperaturen: Indstillinger → Udetemperatur.",
  wx_learn_how:"Et døgn tæller med mindst 23 timers målinger og noget fyring. En ændret varmeplan starter forfra.",
  wx_next:"De næste {n} dage: ca. {a} kg", wx_next_r:"De næste {n} dage: omkring {a} kg", wx_last:"Pillerne derhjemme rækker til ca. {d}.", wx_buy:"Køb senest {d}.",
  wx_at0:"Ved {t} °C ca. {a} kg om dagen", wx_yday:"i går", wx_young:"Linjen er stadig ung: købsdatoen bruger det almindelige gennemsnit, til {n} dage er lært.",
  wx_ax_x:"ude, døgnets gennemsnit", wx_ax_y:"kg om dagen", wx_week:"Den kommende uge",
  wx_week_note:"kg om dagen ud fra vejrudsigten · ±{a} kg over ugen", wx_nofc:"Ingen vejrudsigt endnu.",
  wx_foot:"Lært af {n} dage. De fleste dage ligger inden for ±{b} kg af linjen. Sol, vind og varmt vand flytter enkelte dage."}};
const grT=(k,p)=>{ let s=(GR_TXT[LANG]||GR_TXT.en)[k]; if(s==null) s=k; for(const x in (p||{})) s=s.split("{"+x+"}").join(p[x]); return s; };
// a change's parts in words: "Start fuel 60 → 40 s, new pellet batch “B”", "Cleaned: burner, fan"
const clParts=w=>(w||[]).map(x=>grT("cl_"+x)).join(", ");
const chParts=c=>(c.parts||[]).map(p=>p.k==="cfg"?ccChange(p):p.k==="batch"?grT("ch_p_batch",{n:p.name})
  :p.what&&p.what.length?grT("ch_p_cleaned",{w:clParts(p.what)}):grT("ch_p_clean")).join(", ");
let CH_PICK=null, CH_ARGS=null;            // the change picked (its time), and what was drawn last (for the picker)
function chDraw(el,ch,wx,c){
  CH_ARGS=[el&&el.id,ch,wx,c];
  if(!el) return;
  const L=(ch&&ch.changes)||[];
  if(!L.length){ el.innerHTML=""; return; }
  let i=L.findIndex(x=>x.t===CH_PICK); if(i<0) i=ch.pick||0;
  const C=L[i], h=C.head||{}, nb=C.rows[0].nb, na=C.rows[0].na;
  const tone=h.k==="better"?[c.okBg,c.ok]:h.k==="worse"||h.k==="mixed"||h.k==="failed"?[c.warnBg,c.warn]:[c.nBg,c.ink];
  const many=(C.parts||[]).length>1;
  const pick=L.length>1?`<label style="display:flex;gap:8px;align-items:center;margin-top:10px;font-size:14px;color:${c.dim}">${esc(grT("ch_pick"))}
    <select data-chpick style="font:inherit;font-size:14px;max-width:100%;min-width:0;flex:1 1 auto;padding:6px 8px;border-radius:10px;border:1px solid ${c.grid};background:${c.card};color:${c.ink}">${L.map((x,k)=>`<option value="${x.t}"${k===i?" selected":""}>${esc(dmon(x.t)+": "+chParts(x))}</option>`).join("")}</select></label>`:"";
  const val=r=>{ if(r.k==="failed"||r.k==="short") return [grT("of",{a:r.before,n:r.nb}),grT("of",{a:r.after,n:r.na})];
    if(r.k==="wx") return r.v==="learning"?["–","–"]:[r.before!=null?grT("u_wx",{a:fmt(r.before,1),t:r.ref_c||0}):"–",r.after!=null?grT("u_wx",{a:fmt(r.after,1),t:r.ref_c||0}):"–"];
    const u=r.k==="power_flame"?"":" min", d=r.k==="flame"?1:0; return [r.before!=null?fmt(r.before,d)+u:"–",r.after!=null?fmt(r.after,d)+u:"–"]; };
  const vcol={better:c.ok,worse:c.warn}, td=`padding:6px 8px 6px 0;border-top:1px solid ${c.grid};text-align:left;vertical-align:top`;
  const rows=C.rows.map(r=>{ const [b,a]=val(r), vt=r.v==="learning"?grT("v_learning",{n:r.n,need:r.need}):grT("v_"+r.v);
    return `<tr class="chRow" data-k="${r.k}" data-v="${r.v}"><td style="${td}">${esc(grT("r_"+r.k))}</td><td style="${td};white-space:nowrap">${esc(b)}</td><td style="${td};white-space:nowrap"><b>${esc(a)}</b></td><td style="${td};color:${vcol[r.v]||c.dim}"><span aria-hidden="true">●</span> ${esc(vt)}</td></tr>`; }).join("");
  // a phone: one stacked block per row (name, then before → after and the verdict), no narrow columns
  const narrow=grW(el)<480, rowsN=C.rows.map(r=>{ const [b,a]=val(r), vt=r.v==="learning"?grT("v_learning",{n:r.n,need:r.need}):grT("v_"+r.v);
    return `<div class="chRow" data-k="${r.k}" data-v="${r.v}" style="padding:7px 0;border-top:1px solid ${c.grid}"><b style="font-weight:600">${esc(grT("r_"+r.k))}</b><br>
      <span style="color:${c.dim}">${esc(grT("c_b"))}</span> ${esc(b)} <span style="color:${c.dim}">→ ${esc(grT("c_a"))}</span> <b>${esc(a)}</b> · <span style="color:${vcol[r.v]||c.dim}">${esc(vt)}</span></div>`; }).join("");
  el.innerHTML=`${c.head(grT("ch_h"),grT("ch_sub"))}${pick}
    <div id="chHead" data-k="${esc(h.k)}" style="margin-top:12px;padding:12px 14px;border-radius:14px;background:${tone[0]}"><div style="font-size:17px;font-weight:650;color:${tone[1]}">${esc(grT("ch_"+h.k,{d:fmt(h.d,1),n:h.n,need:h.need,a:h.a,na:h.na}))}</div>
      <div style="margin-top:4px;font-size:14px">${esc(grT("ch_what",{d:dmon(C.t),p:chParts(C)})+" "+grT("ch_n",{b:nb,a:na}))}${many?" "+esc(grT("ch_many")):""}${h.k==="batch"?" "+esc(grT("ch_batch_note")):""}</div></div>
    <div id="chStrip" style="margin-top:12px"></div>
    ${narrow?`<div style="margin-top:8px;font-size:14px">${rowsN}</div>`:`<table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:8px"><tr>${["c_m","c_b","c_a","c_v"].map(k=>`<th style="${td};border-top:0;color:${c.dim};font-weight:600">${esc(grT(k))}</th>`).join("")}</tr>${rows}</table>`}
    <p style="margin:8px 0 0;color:${c.dim};font-size:13px">${C.band?esc(grT("ch_band",{a:fmt(C.band[0],0),b:fmt(C.band[1],0),n:C.left_out}))+" ":""}${esc(grT("ch_how",{need:ch.need||8}))}</p>`;
  chStrip(el.querySelector("#chStrip"),C,c);
}
// the dot strip: minutes to flame over time, grey before and orange after the dashed line, the middle half as a band
function chStrip(el,C,c){
  const D=(C.dots||[]).filter(d=>d[1]!=null), W=grW(el), H=190, x0=30, x1=W-8, y0=22, y1=160;
  if(!D.length){ el.innerHTML=""; return; }
  const t0=Math.min(C.t-3600,...D.map(d=>d[0])), t1=Math.max(C.t+3600,...D.map(d=>d[0])), X=t=>x0+(x1-x0)*(t-t0)/Math.max(1,t1-t0);
  const [,top,st]=niceAxis(0,Math.max(4,...D.map(d=>d[1]))*1.1,true), Y=v=>y1-(y1-y0)*v/top, fr=C.rows[0];
  let s="";
  for(let g=0;g<=4;g++){ const v=st*g, y=Y(v); s+=`<line x1="${x0}" x2="${x1}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" style="stroke:${c.grid}"/><text x="${x0-5}" y="${(y+4).toFixed(1)}" text-anchor="end" font-size="11" style="fill:${c.dim}">${fmt(v,axisDec(st))}</text>`; }
  const xm=X(C.t), side=(q,med,a,b,col)=>{ const xa=X(a), xb=X(b);
    if(q) s+=`<rect x="${xa.toFixed(1)}" y="${Y(q[1]).toFixed(1)}" width="${Math.max(0,xb-xa).toFixed(1)}" height="${Math.max(1,Y(q[0])-Y(q[1])).toFixed(1)}" style="fill:${col}" opacity=".18"/>`;
    if(med!=null) s+=`<line x1="${xa.toFixed(1)}" x2="${xb.toFixed(1)}" y1="${Y(med).toFixed(1)}" y2="${Y(med).toFixed(1)}" style="stroke:${col}" stroke-width="2.5"/>`; };
  side(fr.qb,fr.before,t0,C.t,c.before); side(fr.qa,fr.after,C.t,t1,c.after);
  s+=`<line x1="${xm.toFixed(1)}" x2="${xm.toFixed(1)}" y1="${y0-10}" y2="${y1}" style="stroke:${c.ink}" stroke-dasharray="4 4"/><text x="${(xm+(xm>x1-70?-4:4)).toFixed(1)}" y="${y0-12}" font-size="11" font-weight="650" style="fill:${c.ink}"${xm>x1-70?' text-anchor="end"':""}>${esc(grT("ch_mark"))}</text>`;
  D.forEach(d=>{ s+=`<circle class="chDot" cx="${X(d[0]).toFixed(1)}" cy="${Y(d[1]).toFixed(1)}" r="4.5" style="fill:${d[2]?c.after:c.before};stroke:${c.card}" stroke-width="1.2"><title>${esc(dmon(d[0])+" "+clock(d[0])+" · "+fmt(d[1],1)+" min")}</title></circle>`; });
  if(fr.before!=null) s+=`<text x="${x0+4}" y="${(Y(fr.before)-9).toFixed(1)}" font-size="11" style="fill:${c.dim}">${esc(grT("ch_usual",{a:fmt(fr.before,1)}))}</text>`;
  if(fr.after!=null) s+=`<text x="${x1}" y="${(Y(fr.after)+17).toFixed(1)}" text-anchor="end" font-size="11" font-weight="650" style="fill:${c.after}">${esc(grT("ch_usual",{a:fmt(fr.after,1)}))}</text>`;
  s+=`<text x="${x0}" y="${H-10}" font-size="11" style="fill:${c.dim}">${esc(dmon(t0))}</text><text x="${x1}" y="${H-10}" text-anchor="end" font-size="11" style="fill:${c.dim}">${esc(dmon(t1))}</text>`;
  s+=`<text x="0" y="10" font-size="11" style="fill:${c.dim}">${esc(grT("ch_ax"))}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block;max-width:100%" role="img" aria-label="${esc(grT("ch_h"))}">${s}</svg>`;
}
document.addEventListener("change",e=>{
  if(!e.target.matches||!e.target.matches("[data-chpick]")||!CH_ARGS) return;
  CH_PICK=+e.target.value; chDraw(document.getElementById(CH_ARGS[0]),CH_ARGS[1],CH_ARGS[2],CH_ARGS[3]);
});
// graph 2: learning, or the scatter with the line and the coming week. stock = api/optimiser "stock" (or null),
// on = the outside temperature is turned on
function pwDraw(el,wx,stock,c,on){
  if(!el) return;
  if(!wx){ el.innerHTML=""; return; }
  const head=c.head(grT("wx_h"),grT("wx_sub"));
  if(!wx.learned){
    const why=wx.why==="spread"?grT("wx_spread",{s:fmt(wx.spread,1),need:fmt(wx.need_spread,0)}):wx.why==="fit"?grT("wx_fit"):"";
    el.innerHTML=`${head}<div id="wxLearn" style="margin-top:10px;font-size:17px;font-weight:650;color:${c.dim}">${esc(grT("wx_learn",{n:wx.n,need:wx.need}))}</div>
      ${why?`<p style="margin:6px 0 0">${esc(why)}</p>`:""}${wx.flat!=null?`<p style="margin:6px 0 0">${esc(grT("wx_flat",{a:fmt(wx.flat,1)}))}</p>`:""}
      <p style="margin:6px 0 0;color:${c.dim};font-size:13px">${esc(on===false&&!wx.n?grT("wx_off"):grT("wx_learn_how"))}</p>`;
    return; }
  // one buy-by date: the weather's dates only when the pellet card uses them too (stock.buy_by_src, the server decides)
  const useWx=!!(stock&&stock.buy_by_src==="weather"&&stock.buy_by), fc=wx.fc||[];
  const last=useWx&&wx.last_to?grT("wx_last",{d:dmon(wx.last_to)}):"", buy=useWx?grT("wx_buy",{d:dmon(stock.buy_by)}):"";
  const young=stock&&stock.buy_by&&!useWx&&wx.n<21?grT("wx_young",{n:21}):"";
  const big=fc.length?grT(wx.early?"wx_next_r":"wx_next",{a:fmt(wx.fc_kg,0),n:wx.fc_n||fc.length}):grT("wx_at0",{a:fmt(wx.at0,1),t:wx.ref_c||0});
  el.innerHTML=`${head}<div id="wxTop" style="margin-top:12px;padding:12px 14px;border-radius:14px;background:${c.accBg}"><div style="font-size:17px;font-weight:650;color:${c.acc}">${esc(big)}</div>${last||buy||young?`<div style="margin-top:4px;font-size:14px">${esc((last+" "+buy+" "+young).trim())}</div>`:""}</div>
    <div style="display:flex;flex-wrap:wrap;gap:20px;margin-top:12px"><div id="wxSc" style="flex:1 1 300px;min-width:0"></div><div id="wxFc" style="flex:1 1 240px;min-width:0"></div></div>
    <p style="margin:8px 0 0;color:${c.dim};font-size:13px">${esc(grT("wx_foot",{n:wx.n,b:fmt(wx.band,1)}))}</p>`;
  pwScatter(el.querySelector("#wxSc"),wx,c); pwWeek(el.querySelector("#wxFc"),wx,c);
}
const pwKg=(wx,t)=>Math.max(0,wx.a+wx.b*Math.max(0,wx.base_c-t));
function pwScatter(el,wx,c){
  const P=wx.pts||[], W=grW(el), H=240, x0=34, x1=W-8, y0=26, y1=200, band=wx.band*(wx.early?1.5:1);   // a young line: a wider band
  const [ta,tb,ts]=niceAxis(Math.min(-5,...P.map(p=>p[1])),Math.max(wx.base_c+2,...P.map(p=>p[1]))), X=t=>x0+(x1-x0)*(t-ta)/(tb-ta);
  const [,ky,kst]=niceAxis(0,Math.max(pwKg(wx,ta)+band,...P.map(p=>p[2]))*1.05,true), Y=v=>y1-(y1-y0)*Math.max(0,v)/ky;
  let s="";
  for(let g=0;g<=4;g++){ const v=kst*g, y=Y(v); s+=`<line x1="${x0}" x2="${x1}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" style="stroke:${c.grid}"/><text x="${x0-5}" y="${(y+4).toFixed(1)}" text-anchor="end" font-size="11" style="fill:${c.dim}">${fmt(v,axisDec(kst))}</text>`; }
  for(let v=ta;v<=tb+1e-9;v+=ts) s+=`<text x="${X(v).toFixed(1)}" y="${y1+16}" text-anchor="middle" font-size="11" style="fill:${c.dim}">${fmt(v,0)}°</text>`;
  if(ta<0&&tb>0) s+=`<line x1="${X(0).toFixed(1)}" x2="${X(0).toFixed(1)}" y1="${y0}" y2="${y1}" style="stroke:${c.dim}" stroke-dasharray="2 3"/>`;
  const xs=[]; for(let k=0;k<=40;k++) xs.push(ta+(tb-ta)*k/40); if(wx.base_c>ta&&wx.base_c<tb) xs.push(wx.base_c); xs.sort((a,b)=>a-b);
  const up=xs.map(t=>X(t).toFixed(1)+","+Y(pwKg(wx,t)+band).toFixed(1)), dn=xs.slice().reverse().map(t=>X(t).toFixed(1)+","+Y(pwKg(wx,t)-band).toFixed(1));
  s+=`<polygon points="${up.concat(dn).join(" ")}" style="fill:${c.after}" opacity=".14"/>`;
  s+=`<polyline fill="none" points="${xs.map(t=>X(t).toFixed(1)+","+Y(pwKg(wx,t)).toFixed(1)).join(" ")}" style="stroke:${c.after}" stroke-width="2.5"/>`;
  const yd=P.length?P[P.length-1]:null;
  P.forEach(p=>{ const me=p===yd; s+=`<circle class="wxPt" cx="${X(p[1]).toFixed(1)}" cy="${Y(p[2]).toFixed(1)}" r="${me?6:3.5}" style="fill:${me?c.ink:c.bar}${me?";stroke:"+c.card:""}" stroke-width="1.5"><title>${esc(p[0]+" · "+fmt(p[1],1)+" °C · "+fmt(p[2],1)+" kg")}</title></circle>`; });
  // the newest day: "yesterday" only when it is yesterday's pellets day, else its date
  if(yd){ const r=X(yd[1])>x1-70, y=new Date(); y.setDate(y.getDate()-1);
    const iso=y.getFullYear()+"-"+String(y.getMonth()+1).padStart(2,"0")+"-"+String(y.getDate()).padStart(2,"0");
    const lb=yd[0]===iso?grT("wx_yday"):dmon(new Date(yd[0]+"T12:00").getTime()/1000);
    s+=`<text class="wxYd" x="${(X(yd[1])+(r?-9:9)).toFixed(1)}" y="${(Y(yd[2])-8).toFixed(1)}" font-size="11"${r?' text-anchor="end"':""} style="fill:${c.ink}">${esc(lb)}</text>`; }
  s+=`<text x="${x1}" y="12" text-anchor="end" font-size="12" font-weight="650" style="fill:${c.ink}">${esc(grT("wx_at0",{a:fmt(wx.at0,1),t:wx.ref_c||0}))}</text>`;
  s+=`<text x="0" y="12" font-size="11" style="fill:${c.dim}">${esc(grT("wx_ax_y"))}</text><text x="${x1}" y="${H-4}" text-anchor="end" font-size="11" style="fill:${c.dim}">${esc(grT("wx_ax_x"))}</text>`;
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block;max-width:100%" role="img" aria-label="${esc(grT("wx_h"))}">${s}</svg>`;
}
function pwWeek(el,wx,c){
  const F=wx.fc||[];
  if(!F.length){ el.innerHTML=`<p style="margin:0;color:${c.dim}">${esc(grT("wx_nofc"))}</p>`; return; }
  const W=grW(el), H=240, x0=4, x1=W-4, y0=40, y1=190, n=F.length, gw=(x1-x0)/n, bw=Math.min(44,gw*.72);
  const mx=Math.max(1,...F.map(f=>f[2])), avg=F.reduce((a,f)=>a+f[2],0)/n, Y=v=>y1-(y1-y0)*v/mx;
  let s=`<text x="${x0}" y="14" font-size="13" font-weight="650" style="fill:${c.ink}">${esc(grT("wx_week"))}</text>`;
  F.forEach((f,i)=>{ const cx=x0+gw*(i+.5), y=Y(f[2]), d=new Date(f[0]+"T12:00");
    s+=`<rect class="wxBar" x="${(cx-bw/2).toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${(y1-y).toFixed(1)}" rx="5" style="fill:${f[2]>avg*1.08?c.bar2:c.bar}"><title>${esc(fmt(f[1],0)+" °C · "+fmt(f[2],1)+" kg")}</title></rect>`;
    s+=`<text x="${cx.toFixed(1)}" y="${(y-6).toFixed(1)}" text-anchor="middle" font-size="12" font-weight="650" style="fill:${c.ink}">${fmt(f[2],0)}</text>`;
    s+=`<text x="${cx.toFixed(1)}" y="${y1+16}" text-anchor="middle" font-size="11" style="fill:${c.ink}">${esc(d.toLocaleDateString(LOC(),{weekday:"short"}).replace(".",""))}</text><text x="${cx.toFixed(1)}" y="${y1+30}" text-anchor="middle" font-size="11" style="fill:${c.dim}">${fmt(f[1],0)}°</text>`; });
  el.innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block;max-width:100%">${s}</svg><p style="margin:4px 0 0;color:${c.dim};font-size:13px">${esc(grT("wx_week_note",{a:fmt(wx.fc_band,0)}))}</p>`;
}
