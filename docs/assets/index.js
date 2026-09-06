(async () => {
  SB.nav("index.html");
  const [meta, tt] = await Promise.all([SB.meta(), SB.json("data/trust_today.json")]);
  SB.setMeta("generated " + SB.utc(tt.generated_at) + " · window " + (meta.analysis_window.months) + " mo");
  let mode = SB.qs().mode === "all" ? "all" : "fair";
  const sites = Object.keys(meta.sites), vars = meta.variables;
  const $grid = SB.$("#grid"), $detail = SB.$("#detail");

  function chip(k, v) { return `<span class="chip ${v==="unknown"?"unknown":""}">${k} <b>${v}</b></span>`; }
  function regimeChips(r) {
    const o = r.obs || {};
    const extra = [];
    if (o.t2m != null) extra.push(`T ${SB.fmt(o.t2m,0)}°`);
    if (o.wspd != null) extra.push(`wind ${o.wdir!=null?Math.round(o.wdir)+"°/":""}${SB.fmt(o.wspd,0)} kt`);
    return `<div class="chips">${chip("sky", r.sky)}${chip("flow", r.flow)}${chip("dn", r.daynight)}${chip("wet", r.wet)}${chip("season", r.season)}</div>
      <div class="small mute num">obs ${r.obs_time ? SB.utc(r.obs_time) : "none"}${r.stale_h!=null && r.stale_h>3 ? ` <span class="bad">stale ${r.stale_h} h</span>`:""}${extra.length?" · "+extra.join(" · "):""}</div>`;
  }
  function condLine(c) {
    if (!c || c.insufficient && !c.winner) return "no data";
    const parts = [];
    if (c.relaxed && c.relaxed.length && c.relaxed.length < 5) parts.push("relaxed: " + c.relaxed.filter(d => !(c.unknown_dims||[]).includes(d)).join(", "));
    if (c.unknown_dims && c.unknown_dims.length) parts.push("unknown here: " + c.unknown_dims.join(", "));
    const cons = Object.entries(c.constraint||{}).map(([k,v]) => `${k}=${v}`).join(" ");
    return (cons ? "on " + cons : "unconditioned") + (parts.length ? " · " + parts.join(" · ") : "");
  }
  function cellHtml(site, v, cv) {
    const lab = `data-label="${SB.varLabel(v)}"`;
    if (!cv || cv.na) return `<td class="nav" ${lab}>n/a<div class="cond">${cv && cv.reason ? cv.reason : "no model carries this variable here"}</div></td>`;
    const c = cv[mode];
    if (!c || !c.winner) return `<td class="nav" ${lab}>no data<div class="cond">${condLine(c)}</div></td>`;
    const top = c.models[0], d = SB.dec(v);
    let line;
    if (SB.PRECIP(v)) {
      const k = Object.keys(top.thr||{}).sort()[0]; const t = top.thr[k];
      line = `CSI ${SB.fmt(t.csi,2)} · POD ${SB.fmt(t.pod,2)} · FAR ${SB.fmt(t.far,2)}<br>fbias ${SB.fmt(t.fbias,2)} · n=${top.n} · ev=${t.events}` +
             (c.margin!=null ? `<br>+${SB.fmt(c.margin,2)} CSI over ${SB.label(c.runner_up)}` : "");
    } else {
      line = `<span class="${SB.biasClass(top.bias)}">${SB.sgn(top.bias,d)} ${SB.unit(v)}</span> bias · ${SB.fmt(top.mae,d)} MAE<br>n=${top.n}` +
             (c.margin!=null ? ` · +${SB.fmt(c.margin,d)} over ${SB.label(c.runner_up)}` : "");
    }
    const ins = c.insufficient ? "insufficient" : "";
    return `<td class="cell ${ins}" ${lab} data-site="${site}" data-var="${v}"><div class="win">${SB.label(c.winner)}</div><div class="line">${line}</div><div class="cond">${condLine(c)}</div></td>`;
  }
  function render() {
    SB.$("#mode-fair").classList.toggle("on", mode==="fair");
    SB.$("#mode-all").classList.toggle("on", mode==="all");
    SB.$("#mode-note").textContent = mode === "fair"
      ? "fair: only valid times where every compared model has a forecast in the same lead window"
      : "all data: each model scored on everything it has; sample sets differ between models";
    let h = `<thead><tr><th class="site">site / current regime</th>${vars.map(v=>`<th>${SB.varLabel(v)} <span class="mute">${SB.unit(v)}</span></th>`).join("")}</tr></thead><tbody>`;
    for (const s of sites) {
      const st = tt.sites[s]; if (!st) continue;
      h += `<tr><td><div class="num">${s}</div><div class="sitename">${meta.sites[s].name}</div>${regimeChips(st.regime)}</td>`;
      for (const v of vars) h += cellHtml(s, v, st.vars[v]);
      h += "</tr>";
    }
    $grid.innerHTML = h + "</tbody>";
    $grid.querySelectorAll("td.cell").forEach(td => td.addEventListener("click", () => showDetail(td.dataset.site, td.dataset.var)));
  }
  function showDetail(site, v) {
    const c = tt.sites[site].vars[v][mode], d = SB.dec(v);
    let rows;
    if (SB.PRECIP(v)) {
      const ks = Object.keys(c.models[0].thr||{}).sort();
      rows = `<tr><th>model</th><th class="num">n</th>${ks.map(k=>`<th class="num">CSI@${k}</th><th class="num">POD</th><th class="num">FAR</th><th class="num">fbias</th><th class="num">ev</th>`).join("")}<th class="num">cond. bias</th><th class="num">cond. MAE</th><th class="num">n wet</th></tr>` +
        c.models.map(m => `<tr class="${m.n<meta.min_n?"insufficient":""}"><td>${SB.label(m.model)}</td><td class="num">${m.n}</td>${ks.map(k=>{const t=m.thr[k];return `<td class="num">${SB.fmt(t.csi,2)}</td><td class="num">${SB.fmt(t.pod,2)}</td><td class="num">${SB.fmt(t.far,2)}</td><td class="num">${SB.fmt(t.fbias,2)}</td><td class="num">${t.events}</td>`;}).join("")}<td class="num">${SB.sgn(m.cond.bias,2)}</td><td class="num">${SB.fmt(m.cond.mae,2)}</td><td class="num">${m.cond.n}</td></tr>`).join("");
    } else {
      rows = `<tr><th>model</th><th class="num">n</th><th class="num">bias</th><th class="num">MAE</th><th class="num">RMSE</th><th class="num">p10</th><th class="num">p50</th><th class="num">p90</th></tr>` +
        c.models.map(m => `<tr class="${m.n<meta.min_n?"insufficient":""}"><td>${SB.label(m.model)}</td><td class="num">${m.n}</td><td class="num ${SB.biasClass(m.bias)}">${SB.sgn(m.bias,d)}</td><td class="num">${SB.fmt(m.mae,d)}</td><td class="num">${SB.fmt(m.rmse,d)}</td><td class="num">${SB.sgn(m.p10,d)}</td><td class="num">${SB.sgn(m.p50,d)}</td><td class="num">${SB.sgn(m.p90,d)}</td></tr>`).join("");
    }
    const ladder = c.steps.map(s => `<span class="chip ${s.label===c.label?"":"unknown"}">${s.label}: ${s.n_models_ok} ok / best n=${s.n_best}</span>`).join(" ");
    $detail.innerHTML = `<div class="detail"><h3>${site} · ${SB.varLabel(v)} · ${mode} · leads 0–${c.lead_h[1]} h · compared: ${c.compared.map(SB.label).join(", ")}</h3>
      <div class="small mute" style="margin-bottom:6px">${condLine(c)}</div>
      <div class="scroll"><table>${rows}</table></div>
      <div class="chips" style="margin-top:8px">${ladder}</div>
      <div class="small mute" style="margin-top:6px">ladder: first step where ≥2 models reach n≥${meta.min_n} is used. <a href="explorer.html?site=${site}&var=${v}&mode=${mode}">open in explorer →</a></div></div>`;
    $detail.scrollIntoView({behavior:"smooth", block:"nearest"});
  }
  SB.$("#mode-fair").onclick = () => { mode="fair"; SB.setQs({mode}); render(); };
  SB.$("#mode-all").onclick = () => { mode="all"; SB.setQs({mode}); render(); };
  render();
})().catch(e => { document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="bad">failed to load: ${e.message}</p>`); });
