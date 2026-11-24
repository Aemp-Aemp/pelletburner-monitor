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
  const bad=lhBad(h), rows=h.rows||[], any=rows.some(r=>r.now!=null), td=`padding:6px 8px 6px 0;border-bottom:1px solid ${c.line};text-align:left;vertical-align:top`;
  const notes=[...new Set(rows.filter(r=>r.mark&&r.mark!=="diff"&&r.mark!=="unused").map(r=>r.mark))];
  const nm=r=>esc(lhT("n_"+r.id))+(r.mark==="unused"?` <span style="color:${c.dim}">(${esc(lhT("m_unused"))})</span>`:"");
  const mk=r=>!r.mark||r.mark==="unused"?"":r.mark==="diff"?` <span title="${esc(lhT("m_diff"))}" style="color:${c.dim}">●</span>`:` <b style="color:${c.warn}">!</b>`;
  return `<details id="lhDet"${(LH_OPEN==null?bad:LH_OPEN)?" open":""}><summary style="cursor:pointer"><b>${esc(lhT("title"))}</b> <span id="lhVerdict" style="color:${bad?c.warn:c.dim}">${esc(lhVerdict(h))}</span></summary>
    <p style="margin:12px 0 6px"><b>${esc(lhT("set_h"))}</b> <span style="color:${c.dim}">${esc(lhBoiler(h))}</span></p>
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
    <p style="margin:10px 0 0;color:${c.dim};font-size:13px">${esc(lhT("ro"))}</p></details>`;
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
