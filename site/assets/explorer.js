(async () => {
  SB.nav("explorer.html");
  const meta = await SB.meta();
  SB.setMeta("window " + SB.utc(meta.analysis_window.start).slice(0,10) + " → " + SB.utc(meta.analysis_window.end).slice(0,10));
  const q = SB.qs();
  const state = {site: q.site || Object.keys(meta.sites)[0], var: q.var || "t2m", mode: q.mode === "all" ? "all" : "fair",
                 sky: q.sky||"", flow: q.flow||"", daynight: q.daynight||"", season: q.season||"", wet: q.wet||"", month: q.month||"", thr: q.thr||""};
  const DIMS = ["sky","flow","daynight","season","wet"];
  const VALUES = {sky:["clear","cloudy","unknown"], flow:["onshore","offshore","other","calm","unknown"], daynight:["day","night","transition"], season:["DJF","MAM","JJA","SON"], wet:["wet","dry","unknown"]};
  SB.opt(SB.$("#site"), Object.keys(meta.sites).map(s=>[s,s]), state.site);
  SB.opt(SB.$("#var"), meta.variables.map(v=>[v,SB.varLabel(v)]), state.var);
  for (const d of DIMS) SB.opt(SB.$("#f-"+d), [["","any"], ...VALUES[d].map(v=>[v,v])], state[d]);
  SB.opt(SB.$("#f-month"), [["","any"], ...Array.from({length:12},(_,i)=>[String(i+1), new Date(2000,i,1).toLocaleString("en",{month:"short"})])], state.month);
  SB.opt(SB.$("#f-thr"), meta.precip_thresholds_in.map(t=>[t.toFixed(2), t.toFixed(2)+"″"]), state.thr || meta.precip_thresholds_in[0].toFixed(2));
  let data = null, chart = null;

  function regimeKey() {
    const cons = DIMS.filter(d => state[d]).map(d => [d, state[d]]);
    if (state.month) cons.push(["month", state.month]);
    if (!cons.length) return "all";
    return cons.sort((a,b)=>a[0]<b[0]?-1:1).map(([k,v])=>`${k}=${v}`).join("|");
  }
  function enforceLimits() {
    const active = DIMS.filter(d => state[d]);
    const monthOn = !!state.month;
    for (const d of DIMS) SB.$("#f-"+d).disabled = monthOn ? !state[d] : (active.length >= 2 && !state[d]);
    SB.$("#f-month").disabled = active.length >= 1;
  }
  async function load() {
    SB.$("#status").textContent = "loading…";
    try { data = await SB.json(`data/explorer/${state.site}_${state.var}.json`); }
    catch (e) { data = null; SB.$("#status").textContent = "no data file for this site/variable"; }
    render();
  }
  function heatColor(x, lo, hi) {
    if (x == null) return "transparent";
    const t = Math.max(0, Math.min(1, (x - lo) / Math.max(1e-9, hi - lo)));
    return `rgba(224,108,108,${0.08 + 0.55*t})`;   // single hue, intensity = worse
  }
  function render() {
    enforceLimits();
    SB.setQs(state);
    SB.$("#mode-fair").classList.toggle("on", state.mode==="fair");
    SB.$("#mode-all").classList.toggle("on", state.mode==="all");
    const precip = SB.PRECIP(state.var);
    SB.$("#thr-label").hidden = !precip;
    const key = regimeKey();
    const models = Object.keys(meta.models).filter(m => meta.models[m].vars.includes(state.var));
    const naModels = Object.keys(meta.models).filter(m => !meta.models[m].vars.includes(state.var));
    const bins = meta.lead_bins;
    if (!data) { SB.$("#heat").innerHTML = ""; if (chart) chart.destroy(); return; }
    const cells = data.cells.filter(c => c.mode === state.mode && c.regime === key);
    const byMB = {}; for (const c of cells) byMB[c.model+"|"+c.lead_bin] = c;
    const thr = SB.$("#f-thr").value;
    const score = c => precip ? (c.thr && c.thr[thr] ? c.thr[thr].csi : null) : c.mae;
    const vals = cells.map(score).filter(x => x != null);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    SB.$("#status").innerHTML = `${state.site} · ${SB.varLabel(state.var)} · ${state.mode} · regime <b>${key}</b> · ${cells.length ? "" : "<span class='bad'>no cells for this slice</span>"} compared: ${(data.compared||[]).map(SB.label).join(", ")} · pairs in window: ${data.n_pairs}`;
    SB.$("#heat-title").textContent = precip ? `CSI at ${thr}″ (higher is better) · model × lead bin` : `MAE ${SB.unit(state.var)} · model × lead bin`;
    let h = `<thead><tr><th>model</th>${bins.map(b=>`<th class="num">${b} h</th>`).join("")}</tr></thead><tbody>`;
    for (const m of models) {
      h += `<tr><td>${SB.label(m)} <span class="mute small">${meta.models[m].step_h}h step</span></td>`;
      for (const b of bins) {
        const c = byMB[m+"|"+b];
        if (!c) { h += `<td class="h na">—</td>`; continue; }
        const s = score(c), low = c.n < meta.min_n;
        const bg = precip ? heatColor(s==null?null:(1-s), 0, 1) : heatColor(s, lo, hi);
        const main = precip ? SB.fmt(s,2) : `${SB.fmt(c.mae, SB.dec(state.var))} <span class="${SB.biasClass(c.bias)}">${SB.sgn(c.bias, SB.dec(state.var))}</span>`;
        const sub = precip ? `n=${c.n} ev=${c.thr[thr].events} fb=${SB.fmt(c.thr[thr].fbias,2)}` : `n=${c.n}`;
        h += `<td class="h ${low?"low":""}" style="background:${low?"transparent":bg}">${main}<span class="n">${sub}</span></td>`;
      }
      h += "</tr>";
    }
    for (const m of naModels) h += `<tr><td class="mute">${SB.label(m)}</td><td class="h na" colspan="${bins.length}">n/a — this model's open data does not carry ${SB.varLabel(state.var)}</td></tr>`;
    SB.$("#heat").innerHTML = h + "</tbody>";
    SB.$("#legend").innerHTML = precip ? `<i style="background:rgba(224,108,108,.08)"></i> CSI high · <i style="background:rgba(224,108,108,.63)"></i> CSI low · grey text = n &lt; ${meta.min_n}` : `<i style="background:rgba(224,108,108,.08)"></i> low MAE · <i style="background:rgba(224,108,108,.63)"></i> high MAE · bias shown <span class="warm">+warm</span>/<span class="cool">−cool</span> · grey text = n &lt; ${meta.min_n}`;
    // bar chart: bias (or frequency bias) by lead bin, one dataset per model
    SB.$("#bar-title").textContent = precip ? `frequency bias at ${thr}″ (1 = unbiased) by lead bin` : `mean bias ${SB.unit(state.var)} (forecast − obs) by lead bin`;
    const ds = models.map(m => ({label: SB.label(m), backgroundColor: SB.MODELCOLOR[m] || "#888", borderWidth: 0,
      data: bins.map(b => { const c = byMB[m+"|"+b]; if (!c) return null; return precip ? (c.thr[thr].fbias) : c.bias; })}));
    if (chart) chart.destroy();
    chart = new Chart(SB.$("#bars"), {type:"bar", data:{labels: bins.map(b=>b+" h"), datasets: ds},
      options:{responsive:true, maintainAspectRatio:false, animation:false,
        plugins:{legend:{labels:{color:"#9aa3ad", boxWidth:10, font:{family:"ui-monospace, Menlo, monospace", size:11}}}},
        scales:{x:{ticks:{color:"#9aa3ad"}, grid:{color:"#22262c"}}, y:{ticks:{color:"#9aa3ad"}, grid:{color:"#22262c"}, beginAtZero:!precip, suggestedMin: precip?0:undefined}}}});
  }
  SB.$("#site").onchange = e => { state.site = e.target.value; load(); };
  SB.$("#var").onchange = e => { state.var = e.target.value; load(); };
  SB.$("#mode-fair").onclick = () => { state.mode="fair"; render(); };
  SB.$("#mode-all").onclick = () => { state.mode="all"; render(); };
  for (const d of DIMS) SB.$("#f-"+d).onchange = e => { state[d] = e.target.value; render(); };
  SB.$("#f-month").onchange = e => { state.month = e.target.value; render(); };
  SB.$("#f-thr").onchange = e => { state.thr = e.target.value; render(); };
  load();
})().catch(e => { document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="bad">failed: ${e.message}</p>`); });
