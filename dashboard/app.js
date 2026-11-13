"use strict";
// Claude chat vault dashboard. All text from the vault is inserted with textContent.
(() => {
  const TOKEN = new URLSearchParams(location.hash.slice(1)).get("t") || "";
  const $ = (sel) => document.querySelector(sel);
  const DAY = 864e5;
  const state = {
    data: null,
    f: { q: "", source: "", status: "", days: 0, dir: "" },
    weak: false,
    sortChats: { key: "last_activity", dir: -1 },
    sortDirs: { key: "last_activity", dir: -1 },
    chatLimit: 100,
    weeksTable: false,
    drawer: null,
  };
  const EVIDENCE = {
    transcript: "transcripts", project_dir: "~/.claude folder", nodejs_cache: "Claude log cache",
    running: "running session", vscode_tabs: "VS Code chat tabs", vscode_state: "VS Code Claude state",
    vscode_views: "VS Code views only",
  };

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) {
      if (c == null || c === false) continue;
      n.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return n;
  }
  function svgEl(tag, attrs) {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  function fmtBytes(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    const v = n / Math.pow(1024, i);
    return (i === 0 ? v.toFixed(0) : v < 10 ? v.toFixed(1) : v.toFixed(0)) + " " + u[i];
  }
  // dates in the viewer's own time zone (transcripts store UTC)
  const pad2 = (n) => String(n).padStart(2, "0");
  function day(iso) {
    const d = iso ? new Date(iso) : null;
    if (!d || isNaN(d)) return iso ? String(iso).slice(0, 10) : "";
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  }
  function dayTime(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !isNaN(d) ? `${day(iso)} ${pad2(d.getHours())}:${pad2(d.getMinutes())}` : day(iso);
  }
  function ago(iso) {
    if (!iso) return "";
    const s = (Date.now() - Date.parse(iso)) / 1000;
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function shortPath(p) {
    const parts = String(p || "").split(/[\\/]/).filter(Boolean);
    return parts.length > 2 ? "…\\" + parts.slice(-2).join("\\") : String(p || "");
  }
  let toastTimer = null;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), 6000);
  }
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { "X-Vault-Token": TOKEN, "Content-Type": "application/json" } });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).error || msg; } catch (e) { /* not JSON */ }
      throw new Error(msg);
    }
    return (res.headers.get("Content-Type") || "").includes("json") ? res.json() : res.text();
  }
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

  // ---------------------------------------------------------------- tooltip
  const tip = $("#tooltip");
  function showTip(evt, head, rows) {
    tip.textContent = "";
    tip.append(el("div", { class: "tt-head", text: head }));
    for (const [value, label, key] of rows) {
      tip.append(el("div", { class: "tt-row" }, el("strong", { text: String(value) }),
        key ? el("span", { class: "line-key " + key }) : null, el("span", { text: label })));
    }
    tip.classList.remove("hidden");
    let x, y;
    if (evt.type === "focus") {
      const r = evt.target.getBoundingClientRect();
      x = r.left + r.width / 2; y = r.top;
    } else { x = evt.clientX; y = evt.clientY; }
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(innerWidth - w - 8, x + 14)) + "px";
    tip.style.top = Math.max(8, y - h - 12) + "px";
  }
  const hideTip = () => tip.classList.add("hidden");

  // ---------------------------------------------------------------- theme
  const THEMES = ["auto", "light", "dark"];
  let theme = "auto";
  try { theme = localStorage.getItem("ccv-theme") || "auto"; } catch (e) { /* storage blocked */ }
  function applyTheme() {
    if (theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    $("#btn-theme").textContent = "Theme: " + theme;
  }
  $("#btn-theme").addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem("ccv-theme", theme); } catch (e) { /* storage blocked */ }
    applyTheme();
  });
  applyTheme();

  // ---------------------------------------------------------------- data + filters
  const annS = (s) => ((state.data.annotations || {}).session || {})[s.key] || {};

  function filtered() {
    const { q, source, status, days, dir } = state.f;
    const since = days ? new Date(Date.now() - days * DAY).toISOString() : "";
    const needle = q.trim().toLowerCase();
    return state.data.sessions.filter((s) => {
      if (source && s.source !== source) return false;
      if (dir && s.dir_key !== dir) return false;
      if (status === "in" && !s.in_source) return false;
      if (status === "gone" && s.in_source) return false;
      const a = annS(s);
      if (status === "star" && !a.star) return false;
      if (since && (s.last_activity || "") < since) return false;
      if (needle) {
        const hay = [s.title, s.first_prompt, s.last_prompt, s.dir_path, s.session_id, a.note,
          (a.tags || []).join(" ")].join("\n").toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }

  async function load() {
    try {
      state.data = await api("/api/data");
    } catch (e) {
      $("#sub").textContent = TOKEN ? "Could not load the vault: " + e.message
        : "Open the dashboard with the link printed by `python vault.py serve` (or dashboard.cmd).";
      return;
    }
    render();
    if (state.data.backup && state.data.backup.running) pollBackup();
    if (new URLSearchParams(location.hash.slice(1)).get("view") === "settings" && view !== "settings") showView("settings");
    const link = new URLSearchParams(location.hash.slice(1)).get("chat");   // #t=...&chat=<source>/<id>
    if (link && !state.drawer) {
      const s = state.data.sessions.find((x) => x.key === link);
      if (s) openChat(s);
    }
  }

  function render() {
    const d = state.data;
    if (!d) return;
    const sess = filtered();
    $("#sub").textContent = `Backups of Claude Code chats from ${d.sources.filter((s) => s.enabled).map((s) => s.name).join(" and ")}, kept outside ~/.claude`;
    renderWelcome(d);
    renderKpis(d);
    renderSources(d);
    renderDirChip(d);
    renderWeeks(sess);
    renderSizes(sess);
    renderDirs(d, sess);
    renderChats(sess);
    $("#foot").textContent = `Vault folder: ${d.vault.path} · recovery steps: ${d.vault.path}\\README.md · data as of ${new Date(d.generated).toLocaleString()}`;
  }

  // ---------------------------------------------------------------- KPI tiles
  function renderKpis(d) {
    const all = d.sessions, gone = all.filter((s) => !s.in_source).length;
    const strong = d.directories.filter((x) => x.strong).length;
    const withChats = d.directories.filter((x) => x.n_chats > 0).length;
    const last = d.runs[d.runs.length - 1];
    const srcLine = last ? Object.entries(last.sources).map(([k, v]) => `${k} ${v.ok ? "ok" : "unreachable"}`).join(" · ") : "press Back up now";
    const tiles = [
      ["Chats in the vault", all.length.toLocaleString(), `${(all.length - gone).toLocaleString()} still in ~/.claude`],
      ["Only in the vault", gone.toLocaleString(), gone ? "gone from ~/.claude, restorable" : "nothing lost since the vault started"],
      ["Directories", strong.toLocaleString(), `${withChats} with chats in the vault`],
      ["Vault size", fmtBytes(d.vault.bytes), `${fmtBytes(d.vault.disk_free)} free on this disk`],
      ["Last backup", last ? ago(last.finished) : "never", srcLine],
    ];
    const host = $("#kpis");
    host.textContent = "";
    for (const [label, value, detail] of tiles) {
      host.append(el("div", { class: "tile" }, el("div", { class: "label", text: label }),
        el("div", { class: "value", text: value }), el("div", { class: "detail", text: detail })));
    }
  }

  function renderSources(d) {
    const sel = $("#f-source");
    const names = [...new Set(d.sessions.map((s) => s.source).concat(d.sources.filter((s) => s.enabled).map((s) => s.name)))];
    sel.textContent = "";
    sel.append(el("option", { value: "", text: "All sources" }));
    for (const n of names) sel.append(el("option", { value: n, text: n }));
    sel.value = state.f.source;
  }

  function renderDirChip(d) {
    const chip = $("#f-dir");
    if (!state.f.dir) { chip.classList.add("hidden"); return; }
    const dir = d.directories.find((x) => x.key === state.f.dir);
    chip.textContent = `Directory: ${shortPath(dir ? dir.path || dir.encoded : state.f.dir)}  ✕`;
    chip.classList.remove("hidden");
  }
  function setDir(key) {
    state.f.dir = key || "";
    state.chatLimit = 100;
    render();
    if (key) $("#t-chats").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------------- chart: chats per week
  function weekStart(t) {
    const d = new Date(t);
    const u = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
    return u - ((new Date(u).getUTCDay() + 6) % 7) * DAY;
  }
  function niceTicks(max) {
    const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000].find((s) => Math.ceil(max / s) <= 5) || Math.ceil(max / 5);
    const top = Math.max(step, Math.ceil(max / step) * step);
    const out = [];
    for (let v = 0; v <= top; v += step) out.push(v);
    return out;
  }
  function roundedTop(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function renderWeeks(sess) {
    const host = $("#ch-weeks"), tb = $("#tb-weeks");
    host.textContent = "";
    tb.textContent = "";
    const lg = $("#lg-weeks");
    lg.textContent = "";
    lg.append(el("span", {}, el("span", { class: "key k1" }), "Still in ~/.claude"),
      el("span", {}, el("span", { class: "key k2" }), "Only in the vault"));
    const dated = sess.filter((s) => s.last_activity);
    if (!dated.length) { host.append(el("div", { class: "empty", text: "No chats in this selection" })); return; }
    const now = Date.now();
    const firstT = state.f.days ? now - state.f.days * DAY : Math.min(...dated.map((s) => Date.parse(s.last_activity)));
    const start = weekStart(firstT), end = weekStart(now);
    const buckets = [];
    for (let t = start; t <= end; t += 7 * DAY) buckets.push({ t, in: 0, gone: 0 });
    for (const s of dated) {
      const i = Math.round((weekStart(Date.parse(s.last_activity)) - start) / (7 * DAY));
      if (i >= 0 && i < buckets.length) buckets[i][s.in_source ? "in" : "gone"] += 1;
    }
    const W = Math.max(280, host.clientWidth || 520), H = 230;
    const m = { t: 10, r: 6, b: 26, l: 30 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const ticks = niceTicks(Math.max(1, ...buckets.map((b) => b.in + b.gone)));
    const top = ticks[ticks.length - 1];
    const y = (v) => m.t + ph - (v / top) * ph;
    const band = pw / buckets.length, bw = Math.max(2, Math.min(24, band * 0.68));
    const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": "Stacked columns: chats per week of last activity, split into still in ~/.claude and only in the vault" });
    for (const v of ticks) {
      svg.append(svgEl("line", { class: v === 0 ? "base" : "grid", x1: m.l, x2: W - m.r, y1: Math.round(y(v)) + 0.5, y2: Math.round(y(v)) + 0.5 }));
      const t = svgEl("text", { class: "tick", x: m.l - 6, y: y(v) + 4, "text-anchor": "end" });
      t.textContent = String(v);
      svg.append(t);
    }
    let lastLabelX = -1e9, prevMonth = -1;
    buckets.forEach((b, i) => {
      const x0 = m.l + i * band, bx = x0 + (band - bw) / 2;
      const segs = [[b.in, "s1"], [b.gone, "s2"]].filter((s) => s[0] > 0);
      let base = y(0);
      segs.forEach(([v, cls], k) => {
        let h = (v / top) * ph - (k > 0 ? 2 : 0);
        h = Math.max(h, 1.5);
        const yTop = base - h;
        const isTop = k === segs.length - 1;
        svg.append(isTop ? svgEl("path", { class: cls, d: roundedTop(bx, yTop, bw, h, 4) })
          : svgEl("rect", { class: cls, x: bx, y: yTop, width: bw, height: h }));
        base = yTop - 2;
      });
      const date = new Date(b.t);
      const month = date.getUTCMonth();
      if (month !== prevMonth && x0 - lastLabelX > 40) {
        const label = svgEl("text", { class: "tick", x: x0 + band / 2, y: H - 8, "text-anchor": "middle" });
        label.textContent = MONTHS[month] + (month === 0 || lastLabelX < 0 ? " " + date.getUTCFullYear() : "");
        svg.append(label);
        lastLabelX = x0;
      }
      prevMonth = month;
      const head = `Week of ${new Date(b.t).toISOString().slice(0, 10)}`;
      const rows = [[b.in, "Still in ~/.claude", "k1"], [b.gone, "Only in the vault", "k2"]];
      const hit = svgEl("rect", { class: "hit", x: x0, y: m.t, width: band, height: ph, tabindex: 0,
        "aria-label": `${head}: ${b.in} still in ~/.claude, ${b.gone} only in the vault` });
      hit.addEventListener("pointermove", (e) => showTip(e, head, rows));
      hit.addEventListener("focus", (e) => showTip(e, head, rows));
      hit.addEventListener("pointerleave", hideTip);
      hit.addEventListener("blur", hideTip);
      svg.append(hit);
    });
    host.append(svg);

    const used = buckets.filter((b) => b.in + b.gone > 0).reverse();
    tb.append(el("table", {}, el("thead", {}, el("tr", {},
      el("th", { text: "Week of" }), el("th", { class: "num", text: "Still in ~/.claude" }),
      el("th", { class: "num", text: "Only in the vault" }), el("th", { class: "num", text: "Total" }))),
      el("tbody", {}, used.map((b) => el("tr", {}, el("td", { text: new Date(b.t).toISOString().slice(0, 10) }),
        el("td", { class: "num", text: b.in }), el("td", { class: "num", text: b.gone }),
        el("td", { class: "num", text: b.in + b.gone }))))));
    tb.classList.toggle("hidden", !state.weeksTable);
  }
  $("#tg-weeks").addEventListener("click", () => {
    state.weeksTable = !state.weeksTable;
    $("#tb-weeks").classList.toggle("hidden", !state.weeksTable);
    $("#tg-weeks").textContent = state.weeksTable ? "Hide table" : "Show as table";
  });

  // ---------------------------------------------------------------- chart: space by directory
  function renderSizes(sess) {
    const host = $("#ch-size");
    host.textContent = "";
    const by = new Map();
    for (const s of sess) {
      const o = by.get(s.dir_key) || { key: s.dir_key, path: s.dir_path, size: 0, n: 0 };
      o.size += s.size + (s.sidecar_bytes || 0);
      o.n += 1;
      by.set(s.dir_key, o);
    }
    const rows = [...by.values()].sort((a, b) => b.size - a.size);
    if (!rows.length) { host.append(el("div", { class: "empty", text: "No chats in this selection" })); return; }
    const shown = rows.slice(0, 8), rest = rows.slice(8);
    if (rest.length) shown.push({ key: null, path: `Other (${rest.length} directories)`, size: rest.reduce((a, r) => a + r.size, 0), n: rest.reduce((a, r) => a + r.n, 0) });
    const max = Math.max(...shown.map((r) => r.size)) || 1;
    for (const r of shown) {
      const bar = el("span", { class: "bar" });
      bar.style.width = `calc((100% - 72px) * ${(r.size / max).toFixed(4)})`;
      const rows2 = [[fmtBytes(r.size), "in the vault"], [r.n, r.n === 1 ? "chat" : "chats"]];
      host.append(el("div", { class: "lab", title: r.path, text: r.key ? shortPath(r.path) : r.path }),
        el("button", {
          class: "row", type: "button", "aria-label": `${r.path}: ${fmtBytes(r.size)}, ${r.n} chats`,
          onclick: () => r.key && setDir(r.key),
          onpointermove: (e) => showTip(e, r.path, rows2), onpointerleave: hideTip,
          onfocus: (e) => showTip(e, r.path, rows2), onblur: hideTip,
        }, bar, el("span", { class: "val", text: fmtBytes(r.size) })));
    }
  }

  // ---------------------------------------------------------------- tables
  function sortRows(rows, sort, get) {
    const k = sort.key;
    return rows.sort((a, b) => {
      const va = get(a, k), vb = get(b, k);
      const ea = va === "" || va == null, eb = vb === "" || vb == null;
      if (ea || eb) return ea === eb ? 0 : ea ? 1 : -1;   // empty values last in either direction
      return (va > vb ? 1 : va < vb ? -1 : 0) * sort.dir;
    });
  }
  function header(cols, sort, onSort) {
    return el("thead", {}, el("tr", {}, cols.map(([key, label, cls]) => el("th", {
      class: cls, text: label, scope: "col",
      "aria-sort": key && sort.key === key ? (sort.dir > 0 ? "ascending" : "descending") : null,
      onclick: key ? () => onSort(key) : null,
    }))));
  }

  function renderDirs(d, sess) {
    const { q, source, status, days, dir } = state.f;
    const counts = new Map();
    for (const s of sess) {
      const c = counts.get(s.dir_key) || { n: 0, gone: 0, size: 0, last: "" };
      c.n += 1; c.gone += s.in_source ? 0 : 1; c.size += s.size + (s.sidecar_bytes || 0);
      c.last = (s.last_activity || "") > c.last ? s.last_activity : c.last;
      counts.set(s.dir_key, c);
    }
    const weak = d.directories.filter((x) => !x.strong).length;
    $("#n-weak").textContent = weak;
    const needle = q.trim().toLowerCase();
    let rows = d.directories.filter((x) => state.weak || x.strong).filter((x) => {
      if (source && x.source !== source) return false;
      if (dir && x.key !== dir) return false;
      const pathHit = needle && (x.path || x.encoded).toLowerCase().includes(needle);
      if (status || days) return counts.has(x.key);
      if (needle) return pathHit || counts.has(x.key);
      return true;
    }).map((x) => ({ x, c: counts.get(x.key) || { n: 0, gone: 0, size: 0, last: "" } }));
    const get = (r, k) => k === "path" ? (r.x.path || r.x.encoded).toLowerCase() : k === "source" ? r.x.source
      : k === "n" ? r.c.n : k === "size" ? r.c.size : r.c.last || "";
    rows = sortRows(rows, state.sortDirs, get);
    const t = $("#t-dirs");
    t.textContent = "";
    t.append(header([["path", "Directory"], ["source", "Source"], ["n", "Chats", "num"], ["last_activity", "Last activity"],
      ["size", "Size", "num"], [null, "Evidence"], [null, ""]], state.sortDirs, (k) => {
      state.sortDirs = { key: k, dir: state.sortDirs.key === k ? -state.sortDirs.dir : (k === "path" || k === "source" ? 1 : -1) };
      renderDirs(state.data, filtered());
    }));
    const body = el("tbody");
    for (const { x, c } of rows) {
      const pathCell = el("td", { class: "path" },
        el("div", { text: x.path || x.encoded }),
        x.root ? el("span", { class: "tag", text: "under " + x.root }) : null,
        x.path && x.exists === false ? el("span", { class: "tag", text: "folder not found" }) : null,
        !x.path ? el("span", { class: "tag", text: "real path unknown (Claude folder name shown)" }) : null);
      const pending = !c.n && x.found_chats ? x.found_chats : 0;   // found by a scan, not copied yet
      const chats = el("td", { class: "num" }, String(c.n),
        c.gone ? el("div", { class: "muted", text: `${c.gone} vault only` }) : null,
        pending ? el("div", { class: "muted", text: `${pending} in ~/.claude, not backed up yet` }) : null);
      const ev = el("td", {}, Object.keys(x.evidence).sort().map((k) => el("span", { class: "tag", title: `first seen ${day(x.evidence[k].first_seen)}, last seen ${day(x.evidence[k].last_seen)}`, text: EVIDENCE[k] || k })));
      const acts = el("td", { class: "actions" },
        c.n ? el("button", { class: "btn small", type: "button", text: "Chats", onclick: () => setDir(x.key) }) : null,
        x.path && x.exists !== false ? el("button", {
          class: "btn small", type: "button", text: "Open in VS Code",
          onclick: async () => {
            try { await post("/api/open", { key: x.key }); toast("Opening " + x.path + " in VS Code"); } catch (e) { toast(e.message); }
          },
        }) : null);
      body.append(el("tr", {}, pathCell, el("td", { text: x.source }), chats,
        el("td", { text: day(c.last) || "—" }), el("td", { class: "num", text: c.n ? fmtBytes(c.size) : "—" }), ev, acts));
    }
    if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 7, class: "empty", text: "No directories in this selection" })));
    t.append(body);
  }

  function statusCell(s) {
    const good = s.in_source;
    return el("td", {}, el("span", { class: "status " + (good ? "good" : "warn") },
      el("span", { class: "icon", "aria-hidden": "true", text: good ? "✓" : "!" }),
      good ? "In ~/.claude" : "Vault only"),
      s.pending_bytes ? el("div", { class: "muted", title: "The chat has changed since the last backup; the next backup copies the rest.",
        text: `${fmtBytes(s.pending_bytes)} newer than the backup` + (s.pending_prompts ? ` (${s.pending_prompts} prompt${s.pending_prompts > 1 ? "s" : ""})` : "") }) : null,
      s.versions ? el("div", { class: "muted", text: `${s.versions} earlier version${s.versions > 1 ? "s" : ""} kept` }) : null);
  }

  function starButton(s, after) {
    const a = annS(s);
    return el("button", {
      class: "star" + (a.star ? " on" : ""), type: "button", "aria-pressed": a.star ? "true" : "false",
      "aria-label": a.star ? "Unstar" : "Star", text: a.star ? "★" : "☆",
      onclick: async (e) => {
        e.stopPropagation();
        try {
          const ent = await post("/api/annotate", { scope: "session", key: s.key, star: !a.star });
          state.data.annotations.session[s.key] = ent;
          render();
          if (after) after();
        } catch (err) { toast(err.message); }
      },
    });
  }

  function renderChats(sess) {
    const get = (s, k) => k === "title" ? (s.title || "").toLowerCase() : k === "dir_path" ? (s.dir_path || "").toLowerCase()
      : k === "in_source" ? (s.in_source ? 1 : 0) : k === "size" ? s.size + (s.sidecar_bytes || 0) : s[k] == null ? "" : s[k];
    const rows = sortRows(sess.slice(), state.sortChats, get);
    $("#n-chats").textContent = `${rows.length.toLocaleString()} of ${state.data.sessions.length.toLocaleString()} chats`;
    const t = $("#t-chats");
    t.textContent = "";
    t.append(header([[null, ""], ["title", "Chat"], ["dir_path", "Directory"], ["started", "Started"], ["last_activity", "Last activity"],
      ["n_prompts", "Prompts", "num"], ["size", "Size", "num"], ["in_source", "Status"]], state.sortChats, (k) => {
      state.sortChats = { key: k, dir: state.sortChats.key === k ? -state.sortChats.dir : (k === "title" || k === "dir_path" ? 1 : -1) };
      renderChats(filtered());
    }));
    const body = el("tbody");
    for (const s of rows.slice(0, state.chatLimit)) {
      const a = annS(s);
      body.append(el("tr", { class: "clickable", tabindex: 0, onclick: () => openChat(s), onkeydown: (e) => { if (e.key === "Enter") openChat(s); } },
        el("td", {}, starButton(s)),
        el("td", {}, el("div", { class: "title", text: s.title || "(untitled)" }),
          el("div", { class: "snippet", text: s.first_prompt || "" }),
          (a.tags || []).map((g) => el("span", { class: "tag", text: g }))),
        el("td", { class: "path", title: s.dir_path, text: shortPath(s.dir_path) }),
        el("td", { text: day(s.started) }),
        el("td", { class: "nowrap", text: dayTime(s.last_activity) }),
        el("td", { class: "num", text: s.n_prompts }),
        el("td", { class: "num", text: fmtBytes(s.size + (s.sidecar_bytes || 0)) }),
        statusCell(s)));
    }
    if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 8, class: "empty", text: "No chats in this selection" })));
    t.append(body);
    $("#more-chats").classList.toggle("hidden", rows.length <= state.chatLimit);
  }
  $("#more-chats").addEventListener("click", () => { state.chatLimit += 200; renderChats(filtered()); });

  // ---------------------------------------------------------------- chat drawer
  function resumeCommand(s) {
    const dir = s.cwd || s.dir_path;
    const src = state.data.sources.find((x) => x.name === s.source) || {};
    if (src.type === "ssh") return `ssh ${src.host}\ncd '${dir}' && claude --resume ${s.session_id}`;
    return `cd "${dir}"; claude --resume ${s.session_id}`;
  }

  async function openChat(s) {
    const dr = $("#drawer");
    state.drawer = { s, offset: 0, total: null };
    dr.textContent = "";
    dr.classList.remove("hidden");
    const a = annS(s);
    const dirEnt = state.data.directories.find((x) => x.key === s.dir_key);
    const meta = el("dl", { class: "meta" });
    const add = (k, v) => { if (v != null && v !== "") meta.append(el("dt", { text: k }), el("dd", { text: String(v) })); };
    add("Directory", s.dir_path);
    add("Source", s.source);
    add("Session id", s.session_id);
    add("Started", s.started ? new Date(s.started).toLocaleString() : "");
    add("Last activity", s.last_activity ? new Date(s.last_activity).toLocaleString() : "");
    if (s.pending_bytes) {
      add("Last activity in the vault copy", s.vault_last_activity ? new Date(s.vault_last_activity).toLocaleString() : "");
      add("Not backed up yet", `${fmtBytes(s.pending_bytes)}` + (s.pending_prompts ? `, ${s.pending_prompts} prompt${s.pending_prompts > 1 ? "s" : ""}` : "") + " - copied by the next backup");
    }
    add("Last backed up", s.backed_up ? new Date(s.backed_up).toLocaleString() : "");
    add("Prompts / replies", `${s.n_prompts} / ${s.n_replies}`);
    add("Models", (s.models || []).join(", "));
    add("Cost (Claude Code estimate)", s.cost_usd != null ? "$" + Number(s.cost_usd).toFixed(2) : "");
    add("Size", `${fmtBytes(s.size)} transcript + ${fmtBytes(s.sidecar_bytes || 0)} tool results / subagents (${s.subagents} subagents)`);
    add("In ~/.claude", s.in_source ? "yes" : `no (gone since ${day(s.missing_since)}) - restorable from the vault`);
    add("Earlier versions kept", s.versions || "");
    add("First backed up", day(s.first_backup));

    const actions = el("div", { class: "drawer-actions" },
      el("button", { class: "btn small", type: "button", text: "Copy resume command", onclick: async () => {
        try { await navigator.clipboard.writeText(resumeCommand(s)); toast("Copied: " + resumeCommand(s)); } catch (e) { toast(resumeCommand(s)); }
      } }),
      el("button", { class: "btn small", type: "button", text: "Export Markdown", onclick: () => exportChat(s) }),
      !s.in_source ? el("button", { class: "btn small primary", type: "button", text: "Restore to ~/.claude", onclick: () => restoreChat(s) }) : null,
      dirEnt && dirEnt.path && dirEnt.exists !== false ? el("button", { class: "btn small", type: "button", text: "Open folder in VS Code", onclick: async () => {
        try { await post("/api/open", { key: dirEnt.key }); toast("Opening " + dirEnt.path); } catch (e) { toast(e.message); }
      } }) : null);

    const tags = el("input", { type: "text", value: (a.tags || []).join(", "), placeholder: "tags, comma separated", "aria-label": "Tags" });
    const note = el("textarea", { placeholder: "Notes about this chat (kept in the vault)", "aria-label": "Notes" });
    note.value = a.note || "";
    const notes = el("div", { class: "notes" }, tags, note, el("div", {},
      el("button", { class: "btn small", type: "button", text: "Save notes", onclick: async () => {
        try {
          const ent = await post("/api/annotate", { scope: "session", key: s.key, note: note.value, tags: tags.value.split(",").map((x) => x.trim()) });
          state.data.annotations.session[s.key] = ent;
          render();
          toast("Notes saved");
        } catch (e) { toast(e.message); }
      } })));

    const list = el("div", { id: "transcript" });
    const more = el("button", { class: "link hidden", type: "button", text: "Load more", onclick: () => loadEntries(list, more) });
    dr.append(
      el("div", { class: "drawer-head" },
        el("h2", { text: s.title || "(untitled)" }),
        el("button", { class: "btn small ghost drawer-close", type: "button", text: "Close", onclick: closeDrawer }),
        el("div", {}, starButton(s, () => openChat(s)), el("span", { class: "muted", text: s.first_prompt ? " " + s.first_prompt.slice(0, 140) : "" }))),
      el("div", { class: "drawer-body" }, meta, actions, notes, el("h2", { text: "Conversation" }),
        el("p", { class: "card-sub", text: "Your prompts and Claude's replies; tool calls are summarised and long tool output is shortened." }),
        list, more));
    await loadEntries(list, more);
  }

  async function loadEntries(list, more) {
    const dz = state.drawer;
    if (!dz) return;
    more.disabled = true;
    try {
      const r = await api(`/api/session?source=${encodeURIComponent(dz.s.source)}&id=${encodeURIComponent(dz.s.session_id)}&offset=${dz.offset}&limit=150`);
      if (state.drawer !== dz) return;
      for (const e of r.entries) list.append(entryNode(e));
      dz.offset += r.entries.length;
      dz.total = r.total;
      more.textContent = `Load more (${(r.total - dz.offset).toLocaleString()} entries left)`;
      more.classList.toggle("hidden", dz.offset >= r.total);
      if (!r.total) list.append(el("p", { class: "muted", text: "This transcript has no readable messages." }));
    } catch (e) {
      list.append(el("p", { class: "muted", text: "Could not read the transcript: " + e.message }));
    } finally { more.disabled = false; }
  }

  function entryNode(e) {
    const when = e.ts ? new Date(e.ts).toLocaleString() : "";
    if (e.role === "user" || e.role === "assistant") {
      const who = e.role === "user" ? (e.queued ? "You (sent while Claude was working)" : "You") : "Claude";
      return el("div", { class: "msg " + e.role }, el("div", { class: "who", text: who + (when ? " · " + when : "") }),
        el("div", { class: "body", text: e.text }));
    }
    if (e.role === "tool_use") return el("div", { class: "msg tool" }, el("span", { class: "mono", text: "› " + e.text }));
    if (e.role === "tool_result") {
      return el("div", { class: "msg tool" }, el("details", {}, el("summary", { text: (e.error ? "Tool error" : "Tool result") + ` (${e.text.length.toLocaleString()} characters shown)` }),
        el("pre", { text: e.text })));
    }
    return el("div", { class: "msg system", text: e.text });
  }

  function closeDrawer() {
    state.drawer = null;
    $("#drawer").classList.add("hidden");
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.drawer) closeDrawer(); });

  async function exportChat(s) {
    try {
      const res = await fetch(`/api/export?source=${encodeURIComponent(s.source)}&id=${encodeURIComponent(s.session_id)}`, { headers: { "X-Vault-Token": TOKEN } });
      if (!res.ok) throw new Error(res.statusText);
      const url = URL.createObjectURL(await res.blob());
      const slug = (s.title || "chat").replace(/[^A-Za-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 50);
      const a = el("a", { href: url, download: `${slug}-${s.session_id.slice(0, 8)}.md` });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) { toast("Export failed: " + e.message); }
  }

  async function restoreChat(s) {
    if (!confirm(`Copy "${s.title || s.session_id}" back into ~/.claude on ${s.source}?\n\nFiles that are already there are never overwritten.`)) return;
    try {
      const r = await post("/api/restore", { source: s.source, id: s.session_id });
      toast(`Restored ${r.restored} files (${r.skipped} were already there). Reopen ${shortPath(s.dir_path)} in VS Code to see the chat.`);
      closeDrawer();
      backupNow();
    } catch (e) { toast("Restore failed: " + e.message); }
  }

  // ---------------------------------------------------------------- backup button
  async function backupNow() {
    $("#btn-backup").disabled = true;
    try { await post("/api/backup"); } catch (e) { toast(e.message); }
    pollBackup();
  }
  async function pollBackup() {
    $("#btn-backup").disabled = true;
    $("#backup-state").textContent = "Backing up…";
    let st;
    try { st = await api("/api/backup"); } catch (e) { st = { running: false, error: e.message }; }
    if (st.running) { setTimeout(pollBackup, 1500); return; }
    $("#btn-backup").disabled = false;
    $("#backup-state").textContent = "";
    if (st.error) toast("Backup failed: " + st.error);
    else if (st.result) {
      const parts = Object.entries(st.result.sources).map(([k, v]) => v.ok ? `${k}: ${(v.new || 0) + (v.updated || 0) + (v.versioned || 0)} files copied` : `${k}: unreachable`);
      toast("Backup done. " + parts.join(", "));
    }
    await load();
  }
  $("#btn-backup").addEventListener("click", backupNow);

  // ---------------------------------------------------------------- filter wiring
  let searchTimer = null;
  $("#f-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.f.q = e.target.value; state.chatLimit = 100; render(); }, 150);
  });
  $("#f-source").addEventListener("change", (e) => { state.f.source = e.target.value; render(); });
  $("#f-status").addEventListener("change", (e) => { state.f.status = e.target.value; render(); });
  $("#f-period").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.f.days = Number(b.dataset.days);
    for (const x of $("#f-period").querySelectorAll("button")) x.classList.toggle("on", x === b);
    render();
  });
  $("#f-dir").addEventListener("click", () => setDir(""));
  $("#f-weak").addEventListener("change", (e) => { state.weak = e.target.checked; renderDirs(state.data, filtered()); });
  let resizeTimer = null;
  addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.data) { const s = filtered(); renderWeeks(s); } }, 150);
  });

  // ---------------------------------------------------------------- pages
  let view = "chats";
  function showView(name) {
    view = name;
    for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.view === name);
    $("#view-chats").classList.toggle("hidden", name !== "chats");
    $("#view-settings").classList.toggle("hidden", name !== "settings");
    if (name === "settings") loadSettings();
  }
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showView(b.dataset.view));

  function renderWelcome(d) {
    const w = $("#welcome");
    w.textContent = "";
    if (d.runs.length) { w.classList.add("hidden"); return; }
    w.classList.remove("hidden");
    w.append(el("h2", { text: "Welcome - nothing is backed up yet" }),
      el("p", { class: "card-sub", text: d.found_chats
        ? `The scan found ${d.found_chats} Claude Code chats (${fmtBytes(d.found_bytes)}) in ${d.found_folders} folders on this PC; the folders are listed under Directories below. The first backup copies them into ${d.vault.path}; later backups copy only what changed.`
        : "The scan found no Claude Code chats yet. Back up anyway to start the register, or check Settings & this PC." }),
      el("div", { class: "drawer-actions" },
        el("button", { class: "btn primary", type: "button", text: "Run the first backup", onclick: backupNow }),
        el("button", { class: "btn", type: "button", text: "Review settings first", onclick: () => showView("settings") })));
  }

  // ---------------------------------------------------------------- settings page
  let settings = null;
  async function loadSettings() {
    const host = $("#retention");
    if (!settings) host.textContent = "Loading…";
    try { settings = await api("/api/settings"); } catch (e) { host.textContent = "Could not load settings: " + e.message; return; }
    renderRetention();
    renderSchedule();
    renderSourcesTable();
    renderPC();
    renderVaultInfo();
  }

  function dl(pairs) {
    const m = el("dl", { class: "meta" });
    for (const [k, v] of pairs) if (v != null && v !== "") m.append(el("dt", { text: k }), el("dd", {}, v));
    return m;
  }

  function labelFor(days) {
    const p = settings.presets.find((x) => x[0] === days);
    if (p) return p[1].replace(" (Claude Code default)", "");
    return days % 365 === 0 ? `${days / 365} years` : `${days} days`;
  }

  function retentionBlock(name, st) {
    const box = el("div", { class: "ret" });
    if (st.error) { box.append(el("p", { class: "muted", text: `${name}: ${st.error}` })); return box; }
    const where = st.type === "ssh" ? `${st.host}: ${st.file}` : st.file;
    box.append(el("div", { class: "ret-now" }, "Chats on ", el("strong", { text: name }), " are kept for ",
      el("strong", { text: labelFor(st.effective) }), " after their last use"),
      el("div", { class: "muted", text: `${st.value != null ? "cleanupPeriodDays = " + st.value : "cleanupPeriodDays not set (Claude Code default)"} in ${where}${st.exists ? "" : " (file will be created)"} · ${st.n_chats} chats${st.oldest_days != null ? `, oldest last used ${Math.round(st.oldest_days)} days ago` : ""}` }));
    if (st.locked) {
      box.append(el("p", { class: "notice", text: `Set by your organisation in ${st.managed_file}; it cannot be changed here.` }));
      return box;
    }
    if (!st.valid) {
      box.append(el("p", { class: "notice", text: `${st.file} is not valid JSON (${st.error}). Fix it by hand; the vault will not touch it.` }));
      return box;
    }
    const sel = el("select", { "aria-label": "Keep chats for" });
    for (const [v, label] of settings.presets) sel.append(el("option", { value: v, text: label }));
    sel.append(el("option", { value: "custom", text: "Custom number of days…" }));
    const num = el("input", { type: "number", min: settings.limits[0], max: settings.limits[1], step: 1, "aria-label": "Days", class: "days" });
    const preview = el("div", { class: "ret-preview", role: "status" });
    const apply = el("button", { class: "btn primary small", type: "button", text: "Apply" });
    const current = st.effective;
    if (settings.presets.some((p) => p[0] === current)) sel.value = String(current); else { sel.value = "custom"; }
    num.value = current;
    num.classList.toggle("hidden", sel.value !== "custom");
    const chosen = () => Number(sel.value === "custom" ? num.value : sel.value);
    function update() {
      num.classList.toggle("hidden", sel.value !== "custom");
      if (sel.value !== "custom") num.value = sel.value;
      const days = chosen();
      preview.textContent = "";
      if (!Number.isInteger(days) || days < settings.limits[0] || days > settings.limits[1]) {
        preview.append(el("span", { class: "status bad" }, el("span", { class: "icon", text: "!" }), `Choose a whole number from ${settings.limits[0]} to ${settings.limits[1]}.`));
        apply.disabled = true;
        return;
      }
      const old = st.chat_ages.filter((a) => a[0] > days), safe = old.filter((a) => a[1]).length;
      apply.disabled = days === current && st.value === current;
      if (!old.length) {
        preview.append(el("span", { class: "status good" }, el("span", { class: "icon", text: "✓" }),
          `No chat here is older than ${labelFor(days)}; nothing would be deleted.`));
      } else {
        preview.append(el("span", { class: "status warn" }, el("span", { class: "icon", text: "!" }),
          `${old.length} chat${old.length > 1 ? "s were" : " was"} last used more than ${labelFor(days)} ago and would be deleted the next time Claude Code starts. ${safe} of them ${safe === 1 ? "is" : "are"} already in the vault${safe < old.length ? "; run a backup first for the rest" : ""}.`));
      }
    }
    sel.addEventListener("change", update);
    num.addEventListener("input", update);
    apply.addEventListener("click", async () => {
      const days = chosen();
      const old = st.chat_ages.filter((a) => a[0] > days).length;
      const msg = `Set cleanupPeriodDays to ${days} (${labelFor(days)}) in\n${where}?\n\n` +
        (old ? `${old} chats are older than that and would be deleted at Claude Code's next start.\n\n` : "") +
        "Your current settings file is copied into the vault first, and all other settings are kept.";
      if (!confirm(msg)) return;
      apply.disabled = true;
      try {
        const r = await post("/api/retention", { source: name, days });
        toast(`Saved: chats on ${name} are now kept ${labelFor(days)}.` + (r.backup ? ` Previous settings: ${r.backup}` : ""));
        settings.retention[name] = r.status;
        renderRetention();
      } catch (e) { toast("Not changed: " + e.message); apply.disabled = false; }
    });
    box.append(el("div", { class: "ret-controls" }, el("span", { text: "Keep chats for" }), sel, num, apply), preview);
    update();
    return box;
  }

  function renderRetention() {
    const host = $("#retention");
    host.textContent = "";
    for (const [name, st] of Object.entries(settings.retention)) host.append(retentionBlock(name, st));
    for (const s of settings.sources.filter((x) => x.type === "ssh" && x.enabled && !settings.retention[x.name])) {
      host.append(el("div", { class: "ret" }, el("span", { text: `${s.name} (${s.host}) ` }),
        el("button", { class: "btn small", type: "button", text: "Check over ssh", onclick: async (e) => {
          e.target.disabled = true;
          e.target.textContent = "Checking…";
          try { settings.retention[s.name] = await api(`/api/retention?source=${encodeURIComponent(s.name)}`); }
          catch (err) { settings.retention[s.name] = { error: err.message }; }
          renderRetention();
        } })));
    }
    if (!host.childNodes.length) host.append(el("p", { class: "muted", text: "No source is switched on." }));
  }

  function renderSchedule() {
    const st = settings.schedule, host = $("#schedule");
    host.textContent = "";
    const time = el("input", { type: "time", value: st.time || st.default_time || "12:30", "aria-label": "Time" });
    const on = !!st.installed;
    host.append(el("div", { class: "ret-now" }, el("span", { class: "status " + (on ? "good" : "warn") },
      el("span", { class: "icon", text: on ? "✓" : "!" }), on ? `On: every day at ${st.time} (${st.system})` : `Off (${st.system || "scheduler"})`)));
    if (st.error) host.append(el("p", { class: "muted", text: st.error }));
    if (on) {
      host.append(dl([["Next run", st.next || ""], ["Last run", st.last ? `${st.last}${st.last_result != null ? ` (result ${st.last_result}${st.last_result === 0 ? ", ok" : ""})` : ""}` : ""],
        ["Runs", st.command || st.line || st.file || ""]]));
      if (st.points_here === false) host.append(el("p", { class: "notice", text: "This task runs another copy of the vault tool. Saving here points it at this copy." }));
    }
    const save = el("button", { class: "btn primary small", type: "button", text: on ? "Save time" : "Turn on" });
    save.addEventListener("click", async () => {
      save.disabled = true;
      try { settings.schedule = Object.assign(await post("/api/schedule", { action: "install", time: time.value }), { default_time: time.value }); toast("Daily backup scheduled at " + time.value); }
      catch (e) { toast("Could not schedule: " + e.message); }
      renderSchedule();
    });
    const off = on ? el("button", { class: "btn small", type: "button", text: "Turn off", onclick: async () => {
      if (!confirm("Stop the daily automatic backup?")) return;
      try { settings.schedule = Object.assign(await post("/api/schedule", { action: "remove" }), { default_time: time.value }); toast("Daily backup turned off"); }
      catch (e) { toast(e.message); }
      renderSchedule();
    } }) : null;
    host.append(el("div", { class: "ret-controls" }, el("span", { text: "Every day at" }), time, save, off));
  }

  function renderSourcesTable() {
    const t = $("#t-sources");
    t.textContent = "";
    t.append(el("thead", {}, el("tr", {}, ["On", "Name", "Kind", "Location", "Last backup"].map((h) => el("th", { text: h })))));
    const body = el("tbody");
    for (const s of settings.sources) {
      const box = el("input", { type: "checkbox", "aria-label": `Back up ${s.name}` });
      box.checked = s.enabled;
      box.disabled = s.primary;
      box.addEventListener("change", async () => {
        try { settings.sources = (await post("/api/sources", { name: s.name, enabled: box.checked })).sources; toast(`${s.name} ${box.checked ? "will be" : "will no longer be"} backed up`); }
        catch (e) { toast(e.message); box.checked = !box.checked; }
        renderSourcesTable();
      });
      const lr = s.last_run;
      body.append(el("tr", {}, el("td", {}, box), el("td", { text: s.name + (s.primary ? " (this PC)" : "") }),
        el("td", { text: s.type === "ssh" ? "server over ssh" : "folder" }), el("td", { class: "path", text: s.where }),
        el("td", {}, lr ? el("span", { class: "status " + (lr.ok ? "good" : "warn") }, el("span", { class: "icon", text: lr.ok ? "✓" : "!" }),
          lr.ok ? `${lr.files_total} files` : (lr.error || "failed")) : el("span", { class: "muted", text: "not yet" }))));
    }
    t.append(body);
    const sug = $("#suggestions");
    sug.textContent = "";
    const have = new Set(settings.sources.map((s) => s.name));
    const list = ((settings.discovery || {}).suggested_sources || []).filter((x) => !have.has(x.source.name));
    if (list.length) {
      sug.append(el("h3", { text: "Also found" }));
      for (const x of list) {
        sug.append(el("div", { class: "sug" }, el("span", {}, el("strong", { text: x.source.name }), ` - ${x.why} (${x.source.type === "ssh" ? "ssh " + x.source.host : x.source.claude_dir})`),
          el("button", { class: "btn small", type: "button", text: "Add and back it up", onclick: async () => {
            try { settings.sources = (await post("/api/sources", { add: x.id })).sources; toast(`${x.source.name} added; it is included from the next backup`); }
            catch (e) { toast(e.message); }
            renderSourcesTable();
          } })));
      }
    }
  }

  function renderPC() {
    const host = $("#pc");
    host.textContent = "";
    const disc = settings.discovery;
    if (!disc) { host.append(el("p", { class: "muted", text: "Not scanned yet - press Scan this PC again." })); return; }
    const s = disc.system;
    const eds = s.editors.map((e) => `${e.name}: ${e.claude_extension ? e.claude_extension[0] : "no Claude extension"}${e.command ? "" : " (command not on PATH)"}`).join("\n");
    host.append(dl([
      ["Computer", `${s.hostname} · ${s.os} · ${s.machine}`],
      ["User", `${s.user} (${s.home})`],
      ["Hardware", `${s.cpu_count} CPUs, ${s.memory_gb ?? "?"} GB RAM, ${fmtBytes(s.disk.free)} free of ${fmtBytes(s.disk.total)}`],
      ["Python", `${s.python.version} (${s.python.path})`],
      ["Claude Code CLI", s.claude_cli ? `${s.claude_cli.version || "?"} (${s.claude_cli.path})` : "not on PATH (the VS Code extension brings its own)"],
      ["Editors", eds || "none found"],
      ["Claude folders", disc.claude_dirs.map((c) => `${c.path}: ${c.chats} chats, ${fmtBytes(c.bytes)}`).join("\n") || "none found"],
      ["Claude log cache", s.claude_log_cache || "not found"],
      ["WSL", (s.wsl_distros || []).join(", ") || "not installed"],
      ["SSH hosts", (s.ssh_hosts || []).join(", ") || "none in ~/.ssh/config"],
      ["Folders with Claude use", `${disc.folders.filter((f) => f.strong).length} with clear evidence, ${disc.folders.filter((f) => !f.strong).length} weak (listed under Chats > Directories after a backup)`],
      ["Suggested root folders", (disc.suggested_roots || []).join(", ")],
      ["Scanned", new Date(disc.generated).toLocaleString()],
    ]));
  }

  function renderVaultInfo() {
    const d = state.data;
    $("#vault-info").textContent = "";
    $("#vault-info").append(dl([["Folder", d.vault.path], ["Size", `${fmtBytes(d.vault.bytes)} (${fmtBytes(d.vault.disk_free)} free on this disk)`],
      ["Recovery without this tool", `${d.vault.path}${d.vault.path.includes("\\") ? "\\" : "/"}README.md`],
      ["Tip", "The vault protects against Claude Code's cleanup. Copy this folder to another disk now and then to also survive a disk failure."]]));
  }

  $("#btn-scan").addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Scanning…";
    try {
      const r = await post("/api/discover", { wsl: $("#scan-wsl").checked });
      await load();
      await loadSettings();
      toast(`Scan finished: ${r.folders} folders with Claude use (see Chats > Directories), ${r.chats} chats found.` +
        (state.data.runs.length ? " New chats are copied at the next backup." : " Run the first backup to copy the chats into the vault."));
    }
    catch (err) { toast("Scan failed: " + err.message); }
    e.target.disabled = false;
    e.target.textContent = "Scan this PC again";
  });
  $("#btn-open-vault").addEventListener("click", async () => { try { await post("/api/open-vault"); } catch (e) { toast(e.message); } });

  load();
})();
