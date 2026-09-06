// Shared helpers for the scoreboard pages. Plain JS, no build step.
const SB = (() => {
  const UNIT = {t2m:"°F", td2m:"°F", rh2m:"%", vis:"mi", mslp:"hPa", precip1h:"in", precip3h:"in", precip6h:"in", soilm_0_10:"m³/m³", soilm_10_40:"m³/m³"};
  const VARLABEL = {t2m:"2 m T", td2m:"2 m Td", rh2m:"RH", vis:"Vis", mslp:"MSLP", precip1h:"Precip 1h", precip3h:"Precip 3h", precip6h:"Precip 6h"};
  const MODELCOLOR = {hrrr:"#e6b450", nam3km:"#7fc97f", gfs:"#5aa9e6", ifs:"#d67ad6", aifs:"#f08a7a"};
  const PRECIP = v => /^precip\d+h$/.test(v);
  let META = null;

  async function json(path) {
    const r = await fetch(path + "?t=" + Math.floor(Date.now()/600000), {cache:"no-cache"});
    if (!r.ok) throw new Error(path + " " + r.status);
    return r.json();
  }
  async function meta() { if (!META) META = await json("data/meta.json"); return META; }
  const label = m => (META && META.models[m] && META.models[m].label) || m.toUpperCase();
  const unit = v => UNIT[v] || (META && META.units[v]) || "";
  const varLabel = v => VARLABEL[v] || v;
  const fmt = (x, d=1) => (x === null || x === undefined || Number.isNaN(x)) ? "—" : Number(x).toFixed(d);
  const sgn = (x, d=1) => (x === null || x === undefined) ? "—" : (x > 0 ? "+" : (x < 0 ? "−" : "")) + Math.abs(x).toFixed(d);
  const dec = v => (v === "mslp" ? 1 : PRECIP(v) ? 2 : (v === "rh2m" ? 0 : 1));
  const biasClass = x => (x === null || x === undefined) ? "na" : (x > 0.05 ? "warm" : (x < -0.05 ? "cool" : ""));
  const utc = s => s ? s.replace("T"," ").replace("Z","Z") : "—";
  function nav(active) {
    const pages = [["index.html","Trust Today"],["explorer.html","Explorer"],["series.html","Series"],["health.html","Health"]];
    const h = document.querySelector("header");
    h.innerHTML = `<span class="brand">Point Verification</span><nav>${pages.map(([u,t])=>`<a href="${u}" class="${u===active?"active":""}">${t}</a>`).join("")}</nav><span class="meta" id="hdr-meta"></span>`;
  }
  function setMeta(text) { const e = document.getElementById("hdr-meta"); if (e) e.textContent = text; }
  const $ = s => document.querySelector(s);
  const el = (tag, attrs={}, html="") => { const e=document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); e.innerHTML=html; return e; };
  const opt = (sel, items, val) => { sel.innerHTML = items.map(([v,t]) => `<option value="${v}" ${v==val?"selected":""}>${t}</option>`).join(""); };
  const qs = () => Object.fromEntries(new URLSearchParams(location.search));
  const setQs = obj => { const u = new URL(location); for (const k in obj) { if (obj[k]===null||obj[k]===undefined||obj[k]==="") u.searchParams.delete(k); else u.searchParams.set(k, obj[k]); } history.replaceState(null, "", u); };
  return {json, meta, label, unit, varLabel, fmt, sgn, dec, biasClass, utc, nav, setMeta, $, el, opt, qs, setQs, PRECIP, MODELCOLOR};
})();
