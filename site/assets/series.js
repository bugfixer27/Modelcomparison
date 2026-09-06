(async () => {
  SB.nav("series.html");
  const meta = await SB.meta();
  const q = SB.qs();
  const state = {site: q.site || Object.keys(meta.sites)[0], var: q.var || "t2m", lead: q.lead || String(meta.series_leads_h[0])};
  SB.opt(SB.$("#site"), Object.keys(meta.sites).map(s=>[s,s]), state.site);
  SB.opt(SB.$("#var"), meta.variables.map(v=>[v,SB.varLabel(v)]), state.var);
  SB.opt(SB.$("#lead"), meta.series_leads_h.map(l=>[String(l), l+" h"]), state.lead);
  let chart = null;
  const t = s => Date.parse(s.replace("Z","Z").replace(" ","T"));
  const fmtT = ms => { const d = new Date(ms); return d.toISOString().slice(5,16).replace("T"," ") + "Z"; };
  async function load() {
    SB.setQs(state);
    let d; try { d = await SB.json(`data/series/${state.site}_${state.var}.json`); } catch (e) { SB.$("#status").textContent = "no data file"; return; }
    SB.setMeta("generated " + SB.utc(d.generated_at));
    const good = d.obs.filter(o => !/(range|td_gt_t|flatline|spike)/.test(o[2])).map(o => ({x: t(o[0]), y: o[1]}));
    const flagged = d.obs.filter(o => /(range|td_gt_t|flatline|spike)/.test(o[2])).map(o => ({x: t(o[0]), y: o[1], f:o[2]}));
    const ds = [{label:"obs", data: good, borderColor:"#d7dbe0", backgroundColor:"#d7dbe0", borderWidth:1.5, pointRadius:0, tension:0, spanGaps:false},
                {label:"obs (flagged)", data: flagged, showLine:false, pointStyle:"circle", pointRadius:3, borderColor:"#d7dbe0", backgroundColor:"transparent", borderWidth:1}];
    const rows = [];
    for (const m of Object.keys(d.models)) {
      const L = d.models[m][state.lead]; if (!L) continue;
      const pts = L.map(p => ({x: t(p[0]), y: p[1]}));
      ds.push({label: `${SB.label(m)} f${state.lead}`, data: pts, borderColor: SB.MODELCOLOR[m]||"#888", backgroundColor: SB.MODELCOLOR[m]||"#888", borderWidth:1.2, pointRadius: pts.length < 80 ? 2 : 0, tension:0, spanGaps:false});
      // quick stats vs obs at exactly matching times
      const om = new Map(good.map(o=>[o.x,o.y])); const e = pts.filter(p=>om.has(p.x)).map(p=>p.y-om.get(p.x));
      if (e.length) rows.push([m, e.length, e.reduce((a,b)=>a+b,0)/e.length, e.reduce((a,b)=>a+Math.abs(b),0)/e.length]);
    }
    const dec = SB.dec(state.var);
    SB.$("#status").innerHTML = `${state.site} · ${SB.varLabel(state.var)} ${SB.unit(state.var)} · lead ${state.lead} h · obs n=${good.length}, flagged=${flagged.length}`;
    SB.$("#tbl").innerHTML = `<tr><th>model @ f${state.lead}</th><th class="num">matched</th><th class="num">bias</th><th class="num">MAE</th></tr>` +
      rows.map(r=>`<tr><td><span style="color:${SB.MODELCOLOR[r[0]]}">■</span> ${SB.label(r[0])}</td><td class="num">${r[1]}</td><td class="num ${SB.biasClass(r[2])}">${SB.sgn(r[2],dec)}</td><td class="num">${SB.fmt(r[3],dec)}</td></tr>`).join("") +
      (Object.keys(meta.models).filter(m=>!meta.models[m].vars.includes(state.var)).map(m=>`<tr><td class="mute">${SB.label(m)}</td><td class="mute" colspan="3">n/a for this variable</td></tr>`).join(""));
    if (chart) chart.destroy();
    chart = new Chart(SB.$("#ts"), {type:"line", data:{datasets: ds}, options:{responsive:true, maintainAspectRatio:false, animation:false, parsing:false,
      interaction:{mode:"nearest", intersect:false},
      plugins:{legend:{labels:{color:"#9aa3ad", boxWidth:10, font:{family:"ui-monospace, Menlo, monospace", size:11}}},
               tooltip:{callbacks:{title: items => fmtT(items[0].parsed.x)}}},
      scales:{x:{type:"linear", ticks:{color:"#9aa3ad", maxTicksLimit:10, callback: v => fmtT(v)}, grid:{color:"#22262c"}},
              y:{ticks:{color:"#9aa3ad"}, grid:{color:"#22262c"}, title:{display:true, text:SB.unit(state.var), color:"#5c636c"}}}}});
  }
  SB.$("#site").onchange = e => { state.site = e.target.value; load(); };
  SB.$("#var").onchange = e => { state.var = e.target.value; load(); };
  SB.$("#lead").onchange = e => { state.lead = e.target.value; load(); };
  load();
})().catch(e => { document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="bad">failed: ${e.message}</p>`); });
