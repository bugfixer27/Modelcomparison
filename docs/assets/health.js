(async () => {
  SB.nav("health.html");
  const [meta, h] = await Promise.all([SB.meta(), SB.json("data/health.json")]);
  SB.setMeta("generated " + SB.utc(h.generated_at));
  const pctCls = p => p == null ? "mute" : (p >= 95 ? "ok" : (p >= 50 ? "warm" : "bad"));
  const pctCell = p => `<td class="cov ${pctCls(p)}"><span class="bar" style="width:${Math.round((p||0)*0.4)}px"></span>${p==null?"—":p.toFixed(0)+"%"}</td>`;
  const ageCls = iso => { if (!iso) return "bad"; const hrs = (Date.now() - Date.parse(iso)) / 3.6e6; return hrs < 3 ? "ok" : (hrs < 12 ? "warm" : "bad"); };
  // last success
  const srcs = [...Object.keys(meta.models).map(m=>"fcst/"+m), ...Object.keys(meta.sites).map(s=>"obs/"+s)];
  SB.$("#last").innerHTML = `<tr><th>source</th><th>last ok</th><th>archive</th><th class="num">files ok</th><th class="num">missing</th><th class="num">errors</th></tr>` + srcs.map(s => {
    const t = h.last_success[s]; const m = s.startsWith("fcst/") ? h.models[s.slice(5)] : h.obs[s.slice(4)];
    const arc = m ? (m.first_init ? `${m.first_init} → ${m.last_init}` : (m.first ? `${m.first} → ${m.last}` : "—")) : "—";
    return `<tr><td class="num">${s}</td><td class="num ${ageCls(t)}">${t||"never"}</td><td class="num small">${arc}</td><td class="num">${m ? (m.files_ok ?? m.n_hours) : ""}</td><td class="num">${m ? (m.files_missing ?? m.n_flagged) : ""}</td><td class="num">${m ? (m.files_error ?? "") : ""}</td></tr>`;
  }).join("");
  SB.$("#runs").innerHTML = `<tr><th>kind</th><th>started</th><th>status</th><th>summary</th></tr>` + h.runs.map(r => {
    let s = ""; try { const j = JSON.parse(r.summary||"{}"); s = [j.elapsed_s?`${j.elapsed_s}s`:"", j.fcst_stats||"", j.git||"", j.aggregate?`${j.aggregate.n_pairs} pairs`:""].filter(Boolean).join(" · "); } catch(e) { s = r.summary||""; }
    return `<tr><td class="num">${r.kind}</td><td class="num">${r.started_at}</td><td class="${r.status==="ok"?"ok":"bad"}">${r.status}</td><td class="small" style="white-space:normal">${s}</td></tr>`; }).join("");
  // matrices
  function matrix(el, rows, rowKey, valKey) {
    const months = [...new Set(rows.map(r=>r.month))].sort();
    const keys = [...new Set(rows.map(r=>rowKey(r)))];
    const idx = {}; for (const r of rows) idx[rowKey(r)+"|"+r.month] = r;
    el.innerHTML = `<tr><th></th>${months.map(m=>`<th class="num">${m}</th>`).join("")}</tr>` + keys.map(k => `<tr><td class="num">${k}</td>${months.map(m => { const r = idx[k+"|"+m]; return r ? pctCell(valKey(r)) : `<td class="cov mute">·</td>`; }).join("")}</tr>`).join("");
  }
  matrix(SB.$("#fcov"), h.fcst_coverage, r=>SB.label(r.model), r=>r.pct);
  matrix(SB.$("#ocov"), h.obs_coverage, r=>r.site, r=>r.pct);
  matrix(SB.$("#pcov"), h.pair_coverage, r=>`${SB.label(r.model)} ${r.site}`, r=>r.fcst_rows ? 100*r.paired/r.fcst_rows : null);
  SB.$("#fgap-n").textContent = `(${h.gaps.fcst.length}${h.gaps.fcst.length>=400?"+":""})`;
  SB.$("#ogap-n").textContent = `(${h.gaps.obs.length}${h.gaps.obs.length>=400?"+":""})`;
  SB.$("#fgaps").innerHTML = `<tr><th>model</th><th>init</th><th class="num">files</th></tr>` + h.gaps.fcst.slice().reverse().slice(0,150).map(g=>`<tr><td>${SB.label(g.model)}</td><td class="num">${g.init}</td><td class="num">${g.files_ok}/${g.expected}</td></tr>`).join("");
  SB.$("#ogaps").innerHTML = `<tr><th>site</th><th>day</th><th class="num">good hours</th></tr>` + h.gaps.obs.slice().reverse().slice(0,150).map(g=>`<tr><td>${g.site}</td><td class="num">${g.day}</td><td class="num">${g.hours_ok}</td></tr>`).join("");
  SB.$("#errs").innerHTML = `<tr><th>time</th><th>source</th><th>message</th></tr>` + (h.errors.length ? h.errors.map(e=>`<tr><td class="num">${e.ts}</td><td class="num">${e.source}</td><td class="small" style="white-space:normal">${(e.msg||"").replace(/</g,"&lt;")}</td></tr>`).join("") : `<tr><td colspan="3" class="mute">none</td></tr>`);
})().catch(e => { document.querySelector("main").insertAdjacentHTML("afterbegin", `<p class="bad">failed: ${e.message}</p>`); });
