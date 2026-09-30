/* Barry Benchmark Tools web app. Plain ES2020, no build step, no external
   resources. Everything from the server is inserted with textContent. */
(function () {
  "use strict";
  const C = window.BBTCharts, MD = window.BBTMarkdown;
  const main = document.getElementById("main");

  // ---------------------------------------------------------------- helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
    }
    return el;
  }
  async function api(path, opts) {
    opts = opts || {};
    const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (init.method !== "GET") init.headers["X-BBT"] = "1";
    if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    const r = await fetch("/api/" + path, init);
    let data = null;
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
    return data;
  }
  const enc = encodeURIComponent;
  function num(v, nd) {
    if (v == null || v === "" || (typeof v === "number" && !isFinite(v))) return "-";
    if (typeof v !== "number") return String(v);
    if (nd == null) nd = Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 100 ? 1 : 2;
    return v.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: nd });
  }
  function when(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function duration(a, b) {
    if (!a || !b) return "";
    const s = (new Date(b) - new Date(a)) / 1000;
    if (!(s >= 0)) return "";
    return s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min` : `${Math.max(1, Math.round(s / 60))} min`;
  }
  const ICONS = {
    good: "M3 8.5l3 3 7-7",
    bad: "M4 4l8 8M12 4l-8 8",
    warn: "M8 2l6.5 11.5h-13z M8 6.5v3.2 M8 11.6v.2",
    neutral: "M3.5 8h9",
  };
  function icon(kind, color) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", 14); svg.setAttribute("height", 14); svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", ICONS[kind]); p.setAttribute("fill", kind === "warn" ? "none" : "none");
    p.setAttribute("stroke-width", 2); p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    p.style.stroke = color || "currentColor";
    svg.appendChild(p);
    return svg;
  }
  function status(kind, label) {
    const color = { good: "var(--good)", bad: "var(--critical)", warn: "var(--warning)", neutral: "var(--muted)" }[kind];
    return h("span", { class: "status " + kind }, icon(kind, color), label);
  }
  function banner(msg, kind) {
    return h("div", { class: "banner " + (kind || ""), role: kind === "error" ? "alert" : "status" },
      kind === "error" ? icon("bad", "var(--critical)") : icon("neutral"), h("div", null, msg));
  }
  function setPage(title, ...kids) {
    main.textContent = "";
    kids.forEach(k => k && main.appendChild(k));
    document.title = title ? `${title} - Barry Benchmark Tools` : "Barry Benchmark Tools";
  }
  function loading(what) { setPage(null, h("p", { class: "muted" }, `Loading ${what}...`)); }
  function failed(e) { main.textContent = ""; main.appendChild(banner(e.message || String(e), "error")); }
  function table(headers, rows, opts) {
    opts = opts || {};
    const t = h("table", null, h("thead", null, h("tr", null, headers.map((c, i) =>
      h("th", { class: (opts.align || [])[i] === "r" ? "r" : null, scope: "col" }, c)))));
    const tb = h("tbody");
    rows.forEach(r => {
      const tr = h("tr", r.attrs || null, r.cells.map((c, i) => h("td", { class: (opts.align || [])[i] === "r" ? "r" : null }, c)));
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    return h("div", { class: "tablewrap" }, t);
  }
  function tile(label, value, sub) {
    return h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
  }
  function card(...kids) { return h("section", { class: "card" }, ...kids); }

  const LABELS = {
    vkmark_score: "vkmark score", avg_fps: "Average FPS", low_1pct_fps: "1% low", low_01pct_fps: "0.1% low",
    ft_p50_ms: "Frame time p50", ft_p99_ms: "Frame time p99", ft_p999_ms: "p99.9", system_w_avg: "Power",
    cpu_temp_max_c: "CPU max", gpu_temp_max_c: "GPU max", gpu_mhz_avg: "GPU clock", gpu_busy_avg: "GPU busy",
    mj_per_frame: "Energy/frame", game_avg_fps: "Game avg", hitches: "Hitches", cpu_load_avg: "CPU load",
  };
  const UNITS = { ft_p50_ms: "ms", ft_p99_ms: "ms", ft_p999_ms: "ms", system_w_avg: "W", cpu_temp_max_c: "°C",
    gpu_temp_max_c: "°C", gpu_mhz_avg: "MHz", gpu_busy_avg: "%", mj_per_frame: "mJ", cpu_load_avg: "%" };
  function devName(id) {
    const d = info && info.devices ? info.devices.find(x => x.id === id) : null;
    return d ? d.name : id;
  }
  function sessionDevice(s) { return s.device ? devName(s.device) : (s.model || "-"); }
  const POWER = { battery: "Battery", "ac-estimate": "Charger (estimated draw)", "ac-unknown": "Charger (draw unknown)" };
  function powerText(p) { return POWER[p] || (p ? p : "-"); }

  // ---------------------------------------------------------------- theme
  const themeBtn = document.getElementById("theme");
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
    themeBtn.textContent = { light: "Light", dark: "Dark" }[t] || "Auto";
    themeBtn.setAttribute("aria-label", `Theme: ${themeBtn.textContent}. Switch theme`);
  }
  let theme = "auto";
  try { theme = localStorage.getItem("bbt-theme") || "auto"; } catch (e) { /* storage blocked */ }
  applyTheme(theme);
  themeBtn.addEventListener("click", () => {
    theme = { auto: "light", light: "dark", dark: "auto" }[theme] || "auto";
    try { localStorage.setItem("bbt-theme", theme); } catch (e) { /* ignore */ }
    applyTheme(theme);
    route(); // charts pick up the new tokens
  });

  // ---------------------------------------------------------------- job bar
  const jobbar = document.getElementById("jobbar");
  let info = null;
  async function refreshInfo() {
    try {
      info = await api("info");
      const m = document.getElementById("mode");
      m.textContent = "";
      if (info.mode === "remote") {
        const def = info.devices.find(d => d.id === info.default_device);
        m.append(`${info.devices.length} device${info.devices.length === 1 ? "" : "s"} · default `, h("b", null, def ? def.name : "-"));
      } else m.append("On device ", h("b", null, info.host));
      jobbar.textContent = "";
      if (info.jobs.length) {
        jobbar.classList.remove("hidden");
        jobbar.append(h("span", { class: "pulse", "aria-hidden": "true" }));
        info.jobs.forEach(j => jobbar.append(h("span", null, h("b", null, j.label), " ",
          h("a", { href: `#/job/${enc(j.id)}` }, "output"))));
        if (info.jobs.length > 1) jobbar.append(h("a", { href: "#/jobs" }, "all jobs"));
      } else jobbar.classList.add("hidden");
    } catch (e) { /* keep last state */ }
  }
  setInterval(refreshInfo, 5000);

  // ---------------------------------------------------------------- router
  const routes = [
    [/^\/?$/, pageSessions, "sessions"],
    [/^\/s\/([^/]+)$/, pageSession, "sessions"],
    [/^\/s\/([^/]+)\/([^/]+)\/([^/]+)$/, pageRun, "sessions"],
    [/^\/compare$/, pageCompare, "compare"],
    [/^\/run$/, pageNewRun, "run"],
    [/^\/job\/([^/]+)$/, pageJob, "run"],
    [/^\/devices$/, pageDevices, "devices"],
    [/^\/devices\/([^/]+)$/, pageDevice, "devices"],
    [/^\/device$/, () => { location.hash = "#/devices"; }, "devices"],
    [/^\/jobs$/, pageJobs, "run"],
    [/^\/matrices$/, pageMatrices, "matrices"],
  ];
  let cleanup = [];
  function onLeave(fn) { cleanup.push(fn); }
  function route() {
    cleanup.forEach(fn => { try { fn(); } catch (e) { /* ignore */ } });
    cleanup = [];
    const raw = location.hash.replace(/^#/, "") || "/";
    const [path, qs] = raw.split("?");
    const q = new URLSearchParams(qs || "");
    for (const [re, fn, nav] of routes) {
      const m = re.exec(path);
      if (m) {
        document.querySelectorAll("[data-nav]").forEach(a => {
          if (a.dataset.nav === nav) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
        });
        fn(...m.slice(1).map(decodeURIComponent), q);
        window.scrollTo(0, 0);
        return;
      }
    }
    setPage("Not found", banner("No such page.", "error"));
  }
  window.addEventListener("hashchange", route);

  // ---------------------------------------------------------------- sessions
  async function pageSessions() {
    loading("sessions");
    let list;
    try { list = await api("sessions"); } catch (e) { return failed(e); }
    const picked = new Set();
    const devIds = [...new Set(list.map(s => s.device || s.model || "-"))];
    let filter = "";
    try { filter = sessionStorage.getItem("bbt-dev-filter") || ""; } catch (e) { /* ignore */ }
    if (filter && !devIds.includes(filter)) filter = "";
    const cmp = h("button", { class: "primary", disabled: true, onclick: () => {
      const [b, a] = [...picked]; // list is newest first: older one is A
      location.hash = `#/compare?a=${enc(a)}&b=${enc(b)}`;
    } }, "Compare selected");
    const hint = h("span", { class: "muted small" }, "Tick two sessions (any devices) to compare them.");
    const rows = list.map(s => {
      const box = h("input", { type: "checkbox", "aria-label": `Select ${s.tag}`, onclick: e => e.stopPropagation(),
        onchange: e => {
          if (e.target.checked) picked.add(s.name); else picked.delete(s.name);
          if (picked.size > 2) { const first = [...picked][0]; picked.delete(first);
            const other = main.querySelector(`input[data-s="${CSS.escape(first)}"]`); if (other) other.checked = false; }
          cmp.disabled = picked.size !== 2;
          tr.classList.toggle("sel", e.target.checked);
        } });
      box.dataset.s = s.name;
      const chips = h("div", { class: "chips" }, s.scenarios.map(sc => h("span", { class: "chip" },
        `${sc.title}: `, h("b", { class: "num" }, sc.mean == null ? "no data" :
          `${num(sc.mean)} ${sc.primary_metric === "vkmark_score" ? "score" : "fps"}`),
        sc.flags ? icon("warn", "var(--warning)") : null, sc.flags ? ` ${sc.flags}` : null)));
      const tr = h("tr", { class: "link", tabindex: 0, onclick: () => { location.hash = `#/s/${enc(s.name)}`; },
        onkeydown: e => { if (e.key === "Enter") location.hash = `#/s/${enc(s.name)}`; } },
        h("td", null, box),
        h("td", null, h("div", null, h("b", null, s.tag)), h("div", { class: "muted small" }, when(s.started))),
        h("td", { class: "small" }, sessionDevice(s)),
        h("td", null, chips),
        h("td", { class: "small" }, powerText(s.power_source)),
        h("td", { class: "small r" }, `${s.scenarios.reduce((n, x) => n + x.runs_ok, 0)} runs`));
      tr.dataset.dev = s.device || s.model || "-";
      return tr;
    });
    const devSel = h("select", { "aria-label": "Device filter", onchange: e => {
      filter = e.target.value;
      try { sessionStorage.setItem("bbt-dev-filter", filter); } catch (err) { /* ignore */ }
      applyFilter();
    } }, h("option", { value: "" }, "All devices"), devIds.map(d => h("option", { value: d, selected: d === filter }, devName(d))));
    function applyFilter() { rows.forEach(tr => tr.classList.toggle("hidden", !!filter && tr.dataset.dev !== filter)); }
    applyFilter();
    const t = h("div", { class: "tablewrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, h("span", { class: "hidden" }, "Select")), h("th", null, "Session"), h("th", null, "Device"),
        h("th", null, "Results (mean of measured runs, flags)"), h("th", null, "Power"), h("th", { class: "r" }, "Measured"))),
      h("tbody", null, rows)));
    setPage("Sessions",
      h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Sessions"),
        h("p", { class: "muted" }, `${list.length} saved in ${info ? info.results : "results"}`)),
        h("div", { class: "btnrow" }, devIds.length > 1 ? devSel : null, hint, cmp, h("a", { class: "btn", href: "#/run" }, "New run"))),
      list.length ? card(t) : card(h("p", null, "No sessions yet. "), h("a", { href: "#/run" }, "Start a run")));
  }

  // ---------------------------------------------------------------- one session
  async function pageSession(name) {
    loading("session");
    let d;
    try { d = await api(`sessions/${enc(name)}`); } catch (e) { return failed(e); }
    const s = d.summary, meta = d.meta;
    const files = h("div", { class: "btnrow" },
      ["runs.csv", "summary.json"].concat(d.reports).map(f =>
        h("a", { class: "btn", href: `/api/sessions/${enc(name)}/files/${enc(f)}?download=1` }, f)),
      h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete session ${name} from this computer? This cannot be undone.`)) return;
        try { await api(`sessions/${enc(name)}`, { method: "DELETE" }); location.hash = "#/"; } catch (e) { alert(e.message); }
      } }, "Delete"));
    const body = h("div");
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const views = { Results: () => sessionResults(d, body), Report: () => sessionReports(d, body), Conditions: () => sessionConditions(d, body) };
    Object.keys(views).forEach((k, i) => {
      const b = h("button", { role: "tab", "aria-selected": i === 0 ? "true" : "false", onclick: () => {
        tabs.querySelectorAll("button").forEach(x => x.setAttribute("aria-selected", "false"));
        b.setAttribute("aria-selected", "true");
        views[k]();
      } }, k);
      tabs.appendChild(b);
    });
    setPage(s.tag,
      h("div", { class: "pagehead" },
        h("div", { class: "grow" }, h("div", { class: "small" }, h("a", { href: "#/" }, "Sessions"), " / "),
          h("h1", null, s.tag),
          h("p", { class: "muted" }, `${sessionDevice({ device: s.device, model: ((meta.snapshot || {}).system || {}).model })} · ` +
            `${when(s.started)} · ${duration(s.started, s.ended)} · ${name}` + (meta.interrupted ? " · interrupted" : ""))),
        files),
      tabs, body);
    views.Results();
  }

  function sessionResults(d, body) {
    body.textContent = "";
    const name = d.name;
    d.summary.scenarios.forEach(res => {
      const agg = res.aggregate || {};
      const pm = res.primary_metric;
      const a = k => agg[k] || {};
      const measured = res.runs.filter(r => !r.warmup);
      const ok = measured.filter(r => r.status === "ok");
      const cfg = res.config || {};
      const tiles = h("div", { class: "tiles" });
      if (pm === "vkmark_score") tiles.append(tile("vkmark score", num(a(pm).mean, 0), `CV ${num(a(pm).cv_pct)}%`));
      else if (pm) {
        tiles.append(tile("Average FPS", num(a("avg_fps").mean, 1), `CV ${num(a("avg_fps").cv_pct)}%`),
          tile("1% low", num(a("low_1pct_fps").mean, 1) + " fps", `CV ${num(a("low_1pct_fps").cv_pct)}%`),
          tile("0.1% low", num(a("low_01pct_fps").mean, 1) + " fps", `CV ${num(a("low_01pct_fps").cv_pct)}%`),
          tile("Frame time p99", num(a("ft_p99_ms").mean, 1) + " ms"));
      }
      if (a("system_w_avg").mean != null) tiles.append(tile("Power", num(a("system_w_avg").mean, 1) + " W",
        powerText(ok[0] && ok[0].metrics.power_source)));
      if (a("gpu_temp_max_c").mean != null) tiles.append(tile("GPU / CPU max", `${num(a("gpu_temp_max_c").mean, 0)} / ${num(a("cpu_temp_max_c").mean, 0)} °C`));
      const ref = ok[0] || res.runs.find(r => r.status === "ok");
      const facts = [];
      if (res.kind === "steam") facts.push(`App ${cfg.appid}`, cfg.api || null, (cfg.args || []).join(" ") || null);
      if (ref && ref.proton) facts.push(`${ref.proton.name}${ref.proton.version ? " (" + ref.proton.version + ")" : ""}`);
      if (ref && ref.emulation) facts.push(ref.emulation.x86_emulated ? `x86 emulated: ${ref.emulation.method}` : "native, no emulation");
      facts.push(`${ok.length} of ${measured.length} measured runs OK, ${res.runs.length - measured.length} warm-up discarded`);
      const flags = (res.flags || []).length ? h("ul", { class: "flags" }, res.flags.map(f =>
        h("li", null, icon("warn", "var(--warning)"), h("span", null, f)))) : h("p", { class: "small" }, status("good", "No flags"));
      const chart = h("div");
      const runsTable = runTable(name, res);
      body.appendChild(card(
        h("div", { class: "cardhead" }, h("h2", { class: "grow" }, res.title),
          h("span", { class: "muted small" }, res.name)),
        h("p", { class: "small ink-2" }, facts.filter(Boolean).join(" · ")),
        tiles, flags,
        chart, runsTable));
      if (pm) {
        C.dotPlot(chart, {
          title: `${pm === "vkmark_score" ? "vkmark score" : "Average FPS"} per run`,
          subtitle: `Each dot is one run. The line is the median of the measured runs: ${num(a(pm).median, pm === "vkmark_score" ? 0 : 1)}${pm === "vkmark_score" ? "" : " fps"}.`,
          items: res.runs.filter(r => r.status === "ok").map(r => ({ label: r.run, value: r.metrics[pm], muted: r.warmup,
            note: r.metrics.low_1pct_fps != null ? `1% low ${num(r.metrics.low_1pct_fps, 1)} fps` : null })),
          median: a(pm).median, unit: pm === "vkmark_score" ? "" : "fps", valueName: pm === "vkmark_score" ? "score" : "average",
        });
      }
    });
  }

  function runTable(name, res) {
    const pm = res.primary_metric || "avg_fps";
    const cols = pm === "vkmark_score" ? ["vkmark_score", "gpu_mhz_avg", "gpu_temp_max_c", "system_w_avg"]
      : ["avg_fps", "low_1pct_fps", "ft_p99_ms", "gpu_temp_max_c", "system_w_avg"];
    const rows = res.runs.map(r => ({
      attrs: { class: "link" + (r.warmup ? " warm" : ""), tabindex: 0,
        onclick: () => { location.hash = `#/s/${enc(name)}/${enc(res.dir)}/${enc(r.run)}`; },
        onkeydown: e => { if (e.key === "Enter") location.hash = `#/s/${enc(name)}/${enc(res.dir)}/${enc(r.run)}`; } },
      cells: [h("a", { class: "nowrap", href: `#/s/${enc(name)}/${enc(res.dir)}/${enc(r.run)}` }, r.run + (r.warmup ? " (warm-up)" : "")),
        r.status === "ok" ? status("good", "ok") : status("bad", r.status)]
        .concat(cols.map(c => num(r.metrics[c], c === "vkmark_score" ? 0 : 1)))
        .concat([num(r.cooldown && r.cooldown.waited_s, 0) + " s"]),
    }));
    return table(["Run", "Status"].concat(cols.map(c => LABELS[c] + (UNITS[c] ? ` (${UNITS[c]})` : ""))).concat(["Cooldown"]),
      rows, { align: ["", ""].concat(cols.map(() => "r")).concat(["r"]) });
  }

  async function sessionReports(d, body) {
    body.textContent = "";
    if (!d.reports.length) { body.appendChild(card(h("p", null, "No report files."))); return; }
    const sel = h("select", { "aria-label": "Report" }, d.reports.map(r => h("option", { value: r }, r)));
    const raw = h("input", { type: "checkbox", id: "rawmd" });
    const out = h("div");
    async function show() {
      out.textContent = "";
      try {
        const r = await fetch(`/api/sessions/${enc(d.name)}/files/${enc(sel.value)}`, { credentials: "same-origin" });
        const text = await r.text();
        out.appendChild(raw.checked ? h("pre", { class: "md raw" }, text) : MD.render(text));
      } catch (e) { out.appendChild(banner(e.message, "error")); }
    }
    sel.addEventListener("change", show); raw.addEventListener("change", show);
    body.appendChild(card(h("div", { class: "btnrow" }, sel, h("label", { class: "small", for: "rawmd" }, raw, " Show as plain text"),
      h("a", { class: "btn", href: "#", onclick: e => { e.preventDefault(); location.href = `/api/sessions/${enc(d.name)}/files/${enc(sel.value)}?download=1`; } }, "Download")),
      h("div", { style: null }, out)));
    show();
  }

  function sessionConditions(d, body) {
    body.textContent = "";
    const snap = d.meta.snapshot || {}, sys = snap.system || {};
    const rows = [];
    const add = (k, v) => rows.push({ cells: [k, v == null ? "-" : String(v)] });
    add("Device", `${sys.model} (${sys.soc})`);
    add("OS", sys.os && `${sys.os.name} ${sys.os.version_id} (build ${sys.os.build_id})`);
    add("Kernel", sys.kernel && sys.kernel.release);
    add("GPU driver", sys.gpu && `${sys.gpu.driver_info} (Vulkan ${sys.gpu.vulkan_api})`);
    (snap.cpufreq || []).forEach(p => add(`CPU ${p.policy} (cpu ${p.cpus.join(",")})`,
      `${p.governor}, ${p.min_mhz}-${p.max_mhz} MHz` + (p.max_mhz !== p.hw_max_mhz || p.min_mhz !== p.hw_min_mhz ? ` (hardware ${p.hw_min_mhz}-${p.hw_max_mhz})` : "")));
    (snap.devfreq || []).forEach(x => add(`devfreq ${x.name}`, `${x.governor}, ${x.min_mhz}-${x.max_mhz} MHz`));
    if (snap.scheduler) add("Scheduler", `sched_ext ${snap.scheduler.sched_ext}${snap.scheduler.sched_ext_ops ? " (" + snap.scheduler.sched_ext_ops + ")" : ""}, boostd ${snap.scheduler.boostd ? "on" : "off"}, cpuidle ${snap.cpuidle && snap.cpuidle.governor}`);
    if (snap.memory) add("Memory", `${snap.memory.total}, swappiness ${snap.memory.vm.swappiness}, THP ${snap.memory.thp}, ` +
      Object.entries(snap.memory.zram || {}).map(([k, v]) => `${k} ${v.algorithm} ${v.disksize_mib} MiB`).join(", "));
    if (snap.power) add("Power at start", `${powerText(snap.power.power_source)}, battery ${snap.power.battery_pct}% ${snap.power.battery_status}`);
    add("Idle baseline", `CPU ${d.meta.baseline_c.cpu} °C, GPU ${d.meta.baseline_c.gpu} °C`);
    if (snap.gamescope && snap.gamescope.running) add("Gamescope", `nested ${snap.gamescope.nested}, output ${snap.gamescope.output}, frame limit ${snap.gamescope.framerate_limit}`);
    Object.entries(sys.packages || {}).forEach(([k, v]) => add(`package ${k}`, v));
    body.appendChild(card(h("h2", null, "System at session start"), table(["Setting", "Value"], rows)));
  }

  // ---------------------------------------------------------------- one run
  async function pageRun(name, scen, run) {
    loading("run");
    let d, sess;
    try { [d, sess] = await Promise.all([api(`sessions/${enc(name)}/runs/${enc(scen)}/${enc(run)}`), api(`sessions/${enc(name)}`)]); }
    catch (e) { return failed(e); }
    const m = d.metrics, mt = m.metrics || {};
    const res = sess.summary.scenarios.find(x => x.dir === scen) || {};
    const tiles = h("div", { class: "tiles" });
    [["avg_fps", 1, "fps"], ["low_1pct_fps", 1, "fps"], ["low_01pct_fps", 1, "fps"], ["ft_p99_ms", 1, "ms"],
      ["vkmark_score", 0, ""], ["game_avg_fps", 1, "fps"], ["system_w_avg", 1, "W"], ["gpu_mhz_avg", 0, "MHz"], ["gpu_busy_avg", 0, "%"],
      ["cpu_temp_max_c", 0, "°C"], ["gpu_temp_max_c", 0, "°C"]].forEach(([k, nd, u]) => {
      if (mt[k] != null) tiles.append(tile(LABELS[k] || k, `${num(mt[k], nd)}${u ? " " + u : ""}`));
    });
    const charts = h("div", { class: "stack" });
    const files = h("div", { class: "btnrow" });
    const base = `/api/sessions/${enc(name)}/files/${enc(scen)}/${enc(run)}/`;
    ["metrics.json", "samples.csv", "snapshot.json"].forEach(f => files.append(h("a", { class: "btn", href: base + f + "?download=1" }, f)));
    if (m.mangohud_log) files.append(h("a", { class: "btn", href: base + m.mangohud_log.split("/").map(enc).join("/") + "?download=1" }, "MangoHud log"));
    (m.game_files || []).forEach(f => files.append(h("a", { class: "btn", href: base + f.split("/").map(enc).join("/") + "?download=1" }, "Game results")));
    const flags = [];
    if (m.status !== "ok") flags.push(`Run ${m.status}: ${m.error || ""}`);
    if (m.warning) flags.push(m.warning);
    if (mt.throttled) flags.push("Thermal throttling: " + Object.entries(mt.throttled).map(([k, v]) => `${k} state ${v}`).join(", "));
    if (m.cooldown && m.cooldown.timed_out) flags.push("Started before temperatures were back at the baseline");
    setPage(`${res.title || scen} ${run}`,
      h("div", { class: "pagehead" }, h("div", { class: "grow" },
        h("div", { class: "small" }, h("a", { href: "#/" }, "Sessions"), " / ", h("a", { href: `#/s/${enc(name)}` }, sess.summary.tag), " / "),
        h("h1", null, `${res.title || scen}: ${run}${m.warmup ? " (warm-up, discarded)" : ""}`),
        h("p", { class: "muted" }, `${when(m.started)} · cooldown ${num(m.cooldown && m.cooldown.waited_s, 0)} s` +
          (m.emulation ? ` · ${m.emulation.x86_emulated ? "x86 emulated: " + m.emulation.method : "native"}` : ""))), files),
      flags.length ? h("ul", { class: "flags" }, flags.map(f => h("li", null, icon("warn", "var(--warning)"), h("span", null, f)))) : null,
      tiles, charts);
    if (d.frames) {
      const fr = d.frames.t_mean_min_max;
      const el = h("div");
      charts.appendChild(card(el));
      C.lineChart(el, {
        title: "Frame time",
        subtitle: `${num(d.frames.count, 0)} frames; each point is the mean of ${d.frames.per_point} frame(s), the band their min-max. ` +
          "Shaded: trimmed, not counted (scaled to the counted part; loading spikes are cut off).",
        x: fr.map(p => p[0]), series: [{ name: "frame time", values: fr.map(p => p[1]), color: "var(--series-1)" }],
        band: { lo: fr.map(p => p[2]), hi: fr.map(p => p[3]), color: "var(--series-1)" }, bandName: "min - max",
        window: d.frames.window, windowLabel: "trimmed", yMin: 0, yFit: "window", unit: "ms", valueName: "mean", height: 240,
      });
    }
    if (d.samples) {
      const col = d.samples.columns, t = col.t || [];
      const grid = h("div", { class: "multiples" });
      charts.appendChild(card(h("div", { class: "cardhead" }, h("h2", null, "Hardware during the run"),
        h("span", { class: "muted small" }, "1 s samples; shaded = outside the measured window")), grid));
      const win = d.samples.window;
      const mk = (title, series, o) => { const el = h("div"); grid.appendChild(el);
        C.lineChart(el, Object.assign({ title, x: t, series, window: win, windowLabel: "not measured", height: 170 }, o)); };
      const pol = Object.keys(col).filter(k => /^policy\d+_mhz$/.test(k));
      const names = ["little", "mid", "prime"];
      mk("CPU clock per cluster", pol.map((k, i) => ({ name: `${names[i] || k} (${k.replace("_mhz", "")})`, values: col[k], color: `var(--series-${Math.min(3, i + 1)})` })),
        { yMin: 0, unit: "MHz" });
      if (col.gpu_mhz) mk("GPU clock", [{ name: "GPU", values: col.gpu_mhz, color: "var(--series-1)" }], { yMin: 0, unit: "MHz", valueName: "GPU" });
      const temps = [["cpu_temp_c", "CPU"], ["gpu_temp_c", "GPU"], ["mem_temp_c", "memory"]].filter(([k]) => col[k]);
      mk("Temperature (hottest sensor)", temps.map(([k, n], i) => ({ name: n, values: col[k], color: `var(--series-${i + 1})` })), { unit: "°C" });
      if (col.system_w) mk("System power", [{ name: "power", values: col.system_w, color: "var(--series-1)" }], { yMin: 0, unit: "W", valueName: "system" });
      const loads = [["cpu_load", "CPU (all cores)"], ["gpu_busy", "GPU busy"]].filter(([k]) => col[k] && col[k].some(v => v != null));
      if (loads.length) mk("Load", loads.map(([k, n], i) => ({ name: n, values: col[k], color: `var(--series-${i + 1})` })), { yMin: 0, yMax: 100, unit: "%" });
    }
    if (!d.frames && !d.samples) charts.appendChild(banner("This run has no frame or sensor data."));
  }

  // ---------------------------------------------------------------- compare
  async function pageCompare(q) {
    loading("sessions");
    let list;
    try { list = await api("sessions"); } catch (e) { return failed(e); }
    const opt = (sel) => list.map(s => h("option", { value: s.name, selected: s.name === sel }, `${s.tag} · ${sessionDevice(s)} · ${when(s.started)}`));
    const a = h("select", { "aria-label": "Session A" }, opt(q.get("a") || (list[1] || {}).name));
    const b = h("select", { "aria-label": "Session B" }, opt(q.get("b") || (list[0] || {}).name));
    const out = h("div");
    const go = () => { location.hash = `#/compare?a=${enc(a.value)}&b=${enc(b.value)}`; };
    setPage("Compare",
      h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Compare sessions"),
        h("p", { class: "muted" }, "Change = (B - A) / A, per scenario. Within the larger run-to-run CV counts as noise."))),
      card(h("div", { class: "form" }, h("label", { class: "field" }, "A (before)", a), h("label", { class: "field" }, "B (after)", b),
        h("div", { class: "btnrow" }, h("button", { class: "primary", onclick: go }, "Compare")))),
      out);
    if (!q.get("a") || !q.get("b")) { if (list.length < 2) out.appendChild(banner("You need at least two sessions.")); return; }
    let d;
    try { d = await api(`compare?a=${enc(q.get("a"))}&b=${enc(q.get("b"))}`); } catch (e) { out.appendChild(banner(e.message, "error")); return; }
    d.scenarios.forEach(sc => {
      if (sc.only) { out.appendChild(card(h("h2", null, sc.title), h("p", { class: "muted" }, `Only in ${sc.only.toUpperCase()}.`))); return; }
      const rows = sc.metrics.map(m => {
        const verdict = m.verdict === "better" ? status("good", "better") : m.verdict === "worse" ? status("bad", "worse")
          : m.verdict === "noise" ? status("neutral", "within noise") : h("span", { class: "muted small" }, "-");
        return { cells: [m.label, num(m.a), num(m.b), h("b", { class: "num" }, `${m.pct >= 0 ? "+" : ""}${m.pct.toFixed(1)}%`), verdict,
          h("span", { class: "muted" }, `±${num(m.noise_pct, 1)}%`), `${m.n_a}/${m.n_b}`] };
      });
      const flags = sc.flags_a.map(f => "A: " + f).concat(sc.flags_b.map(f => "B: " + f));
      out.appendChild(card(h("div", { class: "cardhead" }, h("h2", { class: "grow" }, sc.title), h("span", { class: "muted small" }, sc.name)),
        sc.tool_a !== sc.tool_b ? banner(`Proton differs: A ${sc.tool_a}, B ${sc.tool_b}`) : null,
        table(["Metric", `A · ${d.a.tag} (${devName(d.a.device)})`, `B · ${d.b.tag} (${devName(d.b.device)})`, "Change", "", "Noise", "Runs"], rows, { align: ["", "r", "r", "r", "", "r", "r"] }),
        flags.length ? h("ul", { class: "flags", style: null }, flags.map(f => h("li", null, icon("warn", "var(--warning)"), h("span", { class: "small" }, f)))) : null));
    });
    out.appendChild(card(h("h2", null, "System settings that differ"),
      d.config_diff.length ? table(["Setting", "A", "B"], d.config_diff.map(r => ({ cells: r.map(x => x == null ? "-" : String(x)) })))
        : h("p", null, "None: both sessions started with the same settings.")));
  }

  // ---------------------------------------------------------------- new run
  async function pageNewRun() {
    loading("matrices");
    let mats, jobs;
    try { [mats, jobs] = await Promise.all([api("matrices"), api("jobs")]); } catch (e) { return failed(e); }
    const remote = info && info.mode === "remote";
    const devs = remote ? info.devices : [];
    const matrix = h("select", { id: "matrix" }, mats.map(m => h("option", { value: m, selected: m === "first-light.yaml" }, m)));
    const tag = h("input", { type: "text", id: "tag", placeholder: "e.g. baseline, gpu-cap-550", maxlength: 60, required: true, autocomplete: "off" });
    const runs = h("input", { type: "number", min: 1, max: 50, placeholder: "from matrix" });
    const warm = h("input", { type: "number", min: 0, max: 10, placeholder: "from matrix" });
    const devBox = h("div", { class: "checks" }, devs.map(d => h("label", null,
      h("input", { type: "checkbox", name: "dev", value: d.id, checked: d.id === info.default_device, disabled: d.busy }),
      h("span", null, h("b", null, d.name), h("span", { class: "muted" }, ` · ${d.user}@${d.host}${d.busy ? " · busy" : ""}`)))));
    const scen = h("div", { class: "checks" }, h("span", { class: "muted small" }, "Check the matrix to list its scenarios."));
    const msg = h("div");
    const start = h("button", { class: "primary", type: "submit" }, "Start run");
    const pickedDevs = () => [...devBox.querySelectorAll("input:checked")].map(x => x.value);
    async function check() {
      scen.textContent = "";
      const dev = pickedDevs()[0] || info.default_device;
      scen.appendChild(h("span", { class: "muted small" }, remote ? `Checking on ${devName(dev)}...` : "Checking..."));
      try {
        const r = await api(`matrices/${enc(matrix.value)}/check`, { method: "POST", body: { device: dev } });
        scen.textContent = "";
        if (!r.ok) {
          const offline = /ssh .* failed \(255\)|Host is down|No route|timed out|Connection refused/i.test(r.error);
          scen.appendChild(banner(offline ? `Could not reach ${devName(dev)} to check the matrix: ${r.error.replace(/^.*?: /, "")}. ` +
            "Pick another device or check it under Devices." : r.error, "error"));
          return;
        }
        const sess = r.matrix.session;
        scen.appendChild(h("p", { class: "small muted" }, `${sess.runs} measured + ${sess.warmup} warm-up per scenario, cooldown to baseline +${sess.cooldown.tolerance_c} °C`));
        r.matrix.scenarios.forEach(s => scen.appendChild(h("label", null, h("input", { type: "checkbox", name: "scen", value: s.name, checked: true }),
          h("span", null, h("b", null, s.name), h("span", { class: "muted" }, ` · ${s.title} · ${s.kind}${s.proton ? " · " + s.proton : ""}`)))));
      } catch (e) { scen.textContent = ""; scen.appendChild(banner(e.message, "error")); }
    }
    matrix.addEventListener("change", check);
    const form = h("form", { onsubmit: async e => {
      e.preventDefault();
      msg.textContent = "";
      const boxes = [...scen.querySelectorAll("input[name=scen]")];
      if (boxes.length && boxes.every(b => !b.checked)) { msg.appendChild(banner("Pick at least one scenario.", "error")); return; }
      const only = boxes.some(b => !b.checked) ? boxes.filter(b => b.checked).map(b => b.value) : [];
      const targets = remote ? pickedDevs() : [null];
      if (!targets.length) { msg.appendChild(banner("Pick at least one device.", "error")); return; }
      start.disabled = true;
      const started = [];
      for (const dev of targets) {
        try {
          started.push(await api("jobs", { method: "POST", body: { kind: "run", matrix: matrix.value, tag: tag.value.trim(),
            runs: runs.value, warmup: warm.value, only, device: dev } }));
        } catch (err) { msg.appendChild(banner(`${dev ? devName(dev) + ": " : ""}${err.message}`, "error")); }
      }
      refreshInfo();
      if (started.length === 1 && started.length === targets.length) location.hash = `#/job/${enc(started[0].id)}`;
      else if (started.length) location.hash = "#/jobs";
      else start.disabled = false;
    } },
      h("div", { class: "form" },
        h("label", { class: "field", for: "matrix" }, "Matrix", matrix),
        h("label", { class: "field", for: "tag" }, "Tag (what is being tested)", tag),
        h("label", { class: "field" }, "Measured runs (optional)", runs),
        h("label", { class: "field" }, "Warm-up runs (optional)", warm)),
      remote ? h("h3", null, "Devices") : null,
      remote ? h("p", { class: "small muted" }, "Tick several to run the same matrix on each at the same time (A/B between devices). " +
        "Each device writes its own session with the same tag.") : null,
      remote ? devBox : null,
      h("h3", null, "Scenarios"), scen, msg,
      h("div", { class: "btnrow" }, start, h("button", { type: "button", onclick: check }, "Check matrix"),
        h("a", { class: "btn", href: "#/matrices" }, "Edit matrices"), remote ? h("a", { class: "btn", href: "#/devices" }, "Manage devices") : null));
    setPage("New run",
      h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "New run"),
        h("p", { class: "muted" }, remote
          ? "Runs on the devices you pick; results come back to this computer when each one finishes. Closing the browser does not stop them."
          : "Runs on this device."))),
      jobs.running.length ? banner(h("span", null, "Running now: ", jobs.running.map((j, i) =>
        h("span", null, i ? ", " : "", h("a", { href: `#/job/${enc(j.id)}` }, j.label))))) : null,
      remote && !devs.length ? banner(h("span", null, "No devices yet. ", h("a", { href: "#/devices" }, "Add one")), "error") : null,
      card(form));
    if (mats.length) check();
  }

  // ---------------------------------------------------------------- jobs
  async function pageJobs() {
    loading("jobs");
    let jobs;
    try { jobs = await api("jobs"); } catch (e) { return failed(e); }
    const rows = jobs.recent.map(j => ({ attrs: { class: "link", onclick: () => { location.hash = `#/job/${enc(j.id)}`; } },
      cells: [h("a", { href: `#/job/${enc(j.id)}` }, j.label), j.device ? devName(j.device) : "-", when(j.started),
        j.state === "running" ? status("warn", "running") : j.state === "done" ? status("good", "done")
          : j.state === "interrupted" ? status("neutral", "detached") : status("bad", j.state)] }));
    setPage("Jobs", h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Jobs"),
      h("p", { class: "muted" }, "Runs, fetches and updates started from this app."))),
      card(rows.length ? table(["Job", "Device", "Started", "State"], rows) : h("p", null, "No jobs yet.")));
    if (jobs.running.length) { const t = setTimeout(route, 5000); onLeave(() => clearTimeout(t)); }
  }

  // ---------------------------------------------------------------- job output
  function pageJob(id) {
    const con = h("pre", { class: "console", "aria-live": "off", tabindex: 0 });
    const state = h("div", { class: "btnrow" });
    const remote = info && info.mode === "remote";
    const cancel = h("button", { onclick: async () => {
      if (!confirm(remote ? "Detach from this run? It keeps running on the device; use Devices > Stop to end it."
        : "Interrupt this run? It stops after writing what was measured.")) return;
      try { await api(`jobs/${enc(id)}/cancel`, { method: "POST", body: {} }); } catch (e) { alert(e.message); }
    } }, remote ? "Detach" : "Interrupt");
    const links = h("div", { class: "btnrow" });
    setPage("Job", h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Job output"),
      h("p", { class: "muted mono small" }, id)), state), card(con), links);
    state.append(status("warn", "running"), cancel);
    let session = null, follow = true;
    con.addEventListener("scroll", () => { follow = con.scrollTop + con.clientHeight >= con.scrollHeight - 8; });
    const es = new EventSource(`/api/jobs/${enc(id)}/log`);
    onLeave(() => es.close());
    es.addEventListener("log", e => {
      e.data.split("\n").forEach(line => {
        const span = h("span", { class: /FLAG|failed|Traceback|Error/.test(line) ? "flag" : /pulled |report: /.test(line) ? "ok" : null }, line + "\n");
        con.appendChild(span);
        const m = /pulled (\S+) ->/.exec(line) || /results: \S*\/(\d{8}-\d{4}_[^\s/]+)\s*$/.exec(line);
        if (m) session = m[1];
      });
      if (follow) con.scrollTop = con.scrollHeight;
    });
    es.addEventListener("end", e => {
      es.close();
      let meta = {};
      try { meta = JSON.parse(e.data); } catch (err) { /* ignore */ }
      state.textContent = "";
      state.append(meta.state === "done" ? status("good", "finished") : meta.state === "interrupted" ? status("neutral", "detached / interrupted") : status("bad", `failed (exit ${meta.rc})`));
      if (session) links.append(h("a", { class: "btn", href: `#/s/${enc(session)}` }, "Open results"));
      refreshInfo();
    });
    es.onerror = () => { /* EventSource reconnects by itself */ };
  }

  // ---------------------------------------------------------------- devices
  const PROBLEM = {
    auth: "The device does not accept this computer's SSH key. On this computer run: ssh-copy-id ",
    hostkey: "The device's SSH host key changed since the last connection (reinstalled, or a different device at that address). " +
      "Check it, then remove the old key from ~/.ssh/known_hosts.",
    unreachable: "No connection. Check the address, that the device is on and awake, and that both are on the same network or tailnet.",
  };
  function probeView(p, dev) {
    if (!p.ok) {
      const hint = PROBLEM[p.problem] + (p.problem === "auth" ? `${dev.user}@${dev.host}` : "");
      return h("div", null, h("p", null, status("bad", { auth: "Key not accepted", hostkey: "Host key changed", unreachable: "Unreachable" }[p.problem])),
        h("p", { class: "small" }, hint), h("pre", { class: "console small" }, p.detail || ""));
    }
    const f = p.facts;
    const rows = [["Model", f.model], ["OS", f.os], ["Kernel", `${f.kernel} (${f.arch})`], ["User", f.user],
      ["Python", `${f.python}${f.pyyaml ? ", PyYAML " + f.pyyaml : ", no PyYAML"}`],
      ["Steam (Gaming Mode)", f.steam ? "running" : (f.steam_service || "not found")],
      ["Harness", f.harness ? "installed" : "not yet (installed on the first run)"], ["vkmark", f.vkmark ? "installed" : "not installed"]]
      .map(([k, v]) => ({ cells: [k, v || "-"] }));
    return h("div", null,
      h("p", null, f.can_run ? status("good", "Ready to run") : status("bad", `Missing: ${f.missing.join(", ")}`)),
      !f.steam ? h("p", { class: "small muted" }, "Steam scenarios need Steam running in Gaming Mode as this user; vkmark scenarios work without it.") : null,
      table(["", ""], rows));
  }

  function deviceForm(dev, onDone) {
    const f = {
      id: h("input", { type: "text", value: dev ? dev.id : "", placeholder: "e.g. thor, pbos-1", maxlength: 32, disabled: !!dev, autocomplete: "off" }),
      name: h("input", { type: "text", value: dev ? dev.name : "", placeholder: "e.g. AYN Thor", maxlength: 60 }),
      host: h("input", { type: "text", value: dev ? dev.host : "", placeholder: "IP address or host name", autocomplete: "off", spellcheck: "false" }),
      user: h("input", { type: "text", value: dev ? dev.user : "steamos", maxlength: 32, spellcheck: "false" }),
      port: h("input", { type: "number", value: dev ? dev.port : 22, min: 1, max: 65535 }),
      remote_dir: h("input", { type: "text", value: dev ? dev.remote_dir : "bench", spellcheck: "false" }),
    };
    const msg = h("div");
    const out = h("div");
    const save = h("button", { class: "primary", type: "submit" }, dev ? "Save" : "Add and test");
    const form = h("form", { onsubmit: async e => {
      e.preventDefault();
      msg.textContent = ""; out.textContent = "";
      const body = { id: f.id.value.trim(), name: f.name.value.trim(), host: f.host.value.trim(), user: f.user.value.trim(),
        port: f.port.value, remote_dir: f.remote_dir.value.trim() };
      save.disabled = true;
      try {
        if (dev) { await api(`devices/${enc(dev.id)}`, { method: "PUT", body }); msg.appendChild(banner("Saved.")); }
        else {
          msg.appendChild(banner("Added. Testing the connection..."));
          const r = await api("devices", { method: "POST", body });
          msg.textContent = "";
          out.appendChild(probeView(r.probe, body));
        }
        await refreshInfo();
        if (onDone) onDone();
      } catch (err) { msg.textContent = ""; msg.appendChild(banner(err.message, "error")); }
      save.disabled = false;
    } },
      h("div", { class: "form" },
        h("label", { class: "field" }, "Short id (used in session names)", f.id),
        h("label", { class: "field" }, "Name", f.name),
        h("label", { class: "field" }, "Address", f.host),
        h("label", { class: "field" }, "SSH user (the one running Steam)", f.user),
        h("label", { class: "field" }, "SSH port", f.port),
        h("label", { class: "field" }, "Harness folder (under ~)", f.remote_dir)),
      h("p", { class: "small muted" }, "This computer logs in with its SSH key (no passwords). If the device does not know the key yet, run ssh-copy-id USER@ADDRESS once. " +
        "A new device's host key is trusted on first contact; a changed key is refused."),
      msg, h("div", { class: "btnrow" }, save), out);
    return form;
  }

  async function pageDevices() {
    loading("devices");
    let dl;
    try { dl = await api("devices"); } catch (e) { return failed(e); }
    const list = h("div", { class: "grid2" });
    dl.devices.forEach(d => {
      const out = h("div");
      const test = h("button", { onclick: async () => {
        out.textContent = ""; out.appendChild(h("p", { class: "muted small" }, "Connecting..."));
        try { const p = await api(`devices/${enc(d.id)}/probe`, { method: "POST", body: {} }); out.textContent = ""; out.appendChild(probeView(p, d)); }
        catch (e) { out.textContent = ""; out.appendChild(banner(e.message, "error")); }
      } }, "Test connection");
      const isDef = d.id === dl.default;
      list.appendChild(card(
        h("div", { class: "cardhead" }, h("h2", { class: "grow" }, h("a", { href: `#/devices/${enc(d.id)}` }, d.name)),
          isDef ? h("span", { class: "chip" }, "default") : null, d.busy ? status("warn", "busy") : null),
        h("p", { class: "small ink-2 mono" }, `${d.id} · ${d.user}@${d.host}${d.port !== 22 ? ":" + d.port : ""} · ~/${d.remote_dir}` +
          (d.custom_ssh ? " · custom ssh options" : "")),
        h("div", { class: "btnrow" }, h("a", { class: "btn", href: `#/devices/${enc(d.id)}` }, "Open"), test,
          !isDef ? h("button", { onclick: async () => { try { await api(`devices/${enc(d.id)}/default`, { method: "POST", body: {} }); await refreshInfo(); route(); } catch (e) { alert(e.message); } } }, "Make default") : null,
          h("button", { class: "danger", onclick: async () => {
            if (!confirm(`Remove ${d.name} from this list? Its sessions stay; nothing on the device is touched.`)) return;
            try { await api(`devices/${enc(d.id)}`, { method: "DELETE" }); await refreshInfo(); route(); } catch (e) { alert(e.message); }
          } }, "Remove")),
        out));
    });
    setPage("Devices",
      h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Devices"),
        h("p", { class: "muted" }, "The devices this computer can benchmark. Add one by IP address or host name (LAN or tailnet)."))),
      dl.devices.length ? list : banner("No devices yet: add one below."),
      card(h("h2", null, "Add a device"), deviceForm(null, () => setTimeout(route, 1500))));
  }

  async function pageDevice(id) {
    let dl;
    try { dl = await api("devices"); } catch (e) { return failed(e); }
    const dev = dl.devices.find(d => d.id === id);
    if (!dev) return setPage("Devices", banner(`No device ${id}.`, "error"));
    const statusBox = h("div", null, h("p", { class: "muted" }, "Connecting..."));
    const actions = h("div", { class: "btnrow" });
    const snapBox = h("div", null, h("p", { class: "muted small" }, "Clocks, governors, scheduler, memory and power settings right now."));
    const live = h("div");
    const liveBtn = h("button", null, "Start live sensors");
    const editCard = card(h("h2", null, "Edit device"), deviceForm(dev, () => setTimeout(route, 800)));
    editCard.classList.add("hidden");
    setPage(dev.name,
      h("div", { class: "pagehead" }, h("div", { class: "grow" },
        h("div", { class: "small" }, h("a", { href: "#/devices" }, "Devices"), " / "),
        h("h1", null, dev.name),
        h("p", { class: "muted mono small" }, `${dev.id} · ${dev.user}@${dev.host}${dev.port !== 22 ? ":" + dev.port : ""}`)),
        h("div", { class: "btnrow" }, h("button", { onclick: () => editCard.classList.toggle("hidden") }, "Edit"),
          h("a", { class: "btn", href: "#/run" }, "New run"))),
      editCard,
      h("div", { class: "grid2" }, card(h("h2", null, "Status"), statusBox, actions),
        card(h("div", { class: "cardhead" }, h("h2", { class: "grow" }, "Current settings"),
          h("button", { onclick: loadSnap }, "Read settings")), snapBox)),
      card(h("div", { class: "cardhead" }, h("h2", { class: "grow" }, "Live sensors"), liveBtn), live));
    const job = async (kind, ask) => {
      if (ask && !confirm(ask)) return;
      try { const j = await api("jobs", { method: "POST", body: { kind, device: id } }); refreshInfo(); location.hash = `#/job/${enc(j.id)}`; }
      catch (e) { alert(e.message); }
    };
    try {
      const s = await api(`devices/${enc(id)}/status`);
      statusBox.textContent = "";
      statusBox.appendChild(probeView(s.probe, dev));
      if (s.online) {
        statusBox.append(
          h("p", null, s.running.length ? status("warn", `Running on the device: ${s.running.join(", ")}`) : status("neutral", "No run in progress")),
          h("p", { class: "small" }, s.unpulled.length ? `Finished, not copied here yet: ${s.unpulled.join(", ")}` : "Nothing waiting to be copied."));
        if (s.running.length) actions.append(h("button", { class: "primary", onclick: () => job("attach") }, "Follow and fetch"),
          h("button", { class: "danger", onclick: () => job("stop", `Stop the run on ${dev.name}? It keeps what it measured so far.`) }, "Stop run"));
        if (s.unpulled.length) actions.append(h("button", { class: "primary", onclick: () => job("pull") }, "Fetch results"));
        actions.append(h("button", { onclick: () => job("deploy") }, "Install / update the harness"),
          h("button", { onclick: () => job("setup") }, "Install vkmark"));
      }
    } catch (e) { statusBox.textContent = ""; statusBox.appendChild(banner(e.message, "error")); }

    async function loadSnap() {
      snapBox.textContent = "";
      snapBox.appendChild(h("p", { class: "muted" }, "Reading..."));
      try {
        const snap = await api(`devices/${enc(id)}/snapshot`);
        snapBox.textContent = "";
        const rows = [];
        (snap.cpufreq || []).forEach(p => rows.push({ cells: [`CPU ${p.policy}`, `${p.governor}, ${p.min_mhz}-${p.max_mhz} MHz (hw ${p.hw_min_mhz}-${p.hw_max_mhz})`] }));
        (snap.devfreq || []).forEach(x => rows.push({ cells: [x.name, `${x.governor}, ${x.min_mhz}-${x.max_mhz} MHz`] }));
        rows.push({ cells: ["Scheduler", `sched_ext ${snap.scheduler.sched_ext}, boostd ${snap.scheduler.boostd ? "on" : "off"}`] });
        rows.push({ cells: ["Memory", `swappiness ${snap.memory.vm.swappiness}, THP ${snap.memory.thp}`] });
        rows.push({ cells: ["Power", `${powerText(snap.power.power_source)}, battery ${snap.power.battery_pct}%, ${num(snap.power.system_w, 1)} W`] });
        rows.push({ cells: ["Temps", Object.entries(snap.temps_c || {}).map(([k, v]) => `${k} ${v} °C`).join(", ")] });
        rows.push({ cells: ["Kernel", snap.system.kernel.release] });
        rows.push({ cells: ["GPU driver", snap.system.gpu.driver_info || "-"] });
        snapBox.appendChild(table(["Setting", "Value"], rows));
      } catch (e) { snapBox.textContent = ""; snapBox.appendChild(banner(e.message.includes("bench") ? "Install the harness first (Status > Install / update)." : e.message, "error")); }
    }

    let es = null;
    function stopLive() { if (es) { es.close(); es = null; } liveBtn.textContent = "Start live sensors"; }
    onLeave(stopLive);
    liveBtn.addEventListener("click", () => {
      if (es) return stopLive();
      const data = [];
      const tilesEl = h("div", { class: "tiles" });
      const grid = h("div", { class: "multiples" });
      const c1 = h("div"), c2 = h("div"), c3 = h("div");
      grid.append(c1, c2, c3);
      live.textContent = "";
      live.append(tilesEl, grid, h("p", { class: "muted small" }, "Last 2 minutes, 1 s samples. Stops after 5 minutes or when you leave this page."));
      liveBtn.textContent = "Stop live sensors";
      es = new EventSource(`/api/devices/${enc(id)}/sample?seconds=300`);
      es.addEventListener("sample", e => {
        let r; try { r = JSON.parse(e.data); } catch (err) { return; }
        data.push(r);
        if (data.length > 120) data.shift();
        const pol = Object.keys(r).filter(k => /^policy\d+_mhz$/.test(k));
        tilesEl.textContent = "";
        tilesEl.append(tile("CPU load", `${num(r.cpu_load, 0)}%`), tile("CPU clocks", pol.map(k => r[k]).join(" / "), "MHz per cluster"),
          tile("GPU clock", `${num(r.gpu_mhz, 0)} MHz`), tile("CPU / GPU", `${num(r.cpu_temp_c, 0)} / ${num(r.gpu_temp_c, 0)} °C`),
          tile("Power", `${num(r.system_w, 1)} W`, powerText(r.power_source)), tile("Battery", `${r.battery_pct}%`, r.battery_status));
        const t = data.map(x => x.t);
        const names = ["little", "mid", "prime"];
        C.lineChart(c1, { title: "CPU clock per cluster", x: t, yMin: 0, unit: "MHz", height: 150,
          series: pol.map((k, i) => ({ name: names[i] || k, values: data.map(x => x[k]), color: `var(--series-${Math.min(3, i + 1)})` })) });
        C.lineChart(c2, { title: "Temperature", x: t, unit: "°C", height: 150,
          series: [["cpu_temp_c", "CPU"], ["gpu_temp_c", "GPU"], ["mem_temp_c", "memory"]].map(([k, n], i) => ({ name: n, values: data.map(x => x[k]), color: `var(--series-${i + 1})` })) });
        C.lineChart(c3, { title: "System power", x: t, yMin: 0, unit: "W", height: 150, valueName: "system",
          series: [{ name: "power", values: data.map(x => x.system_w), color: "var(--series-1)" }] });
      });
      es.addEventListener("end", stopLive);
    });
  }

  // ---------------------------------------------------------------- matrices
  async function pageMatrices(q) {
    loading("matrices");
    let mats;
    try { mats = await api("matrices"); } catch (e) { return failed(e); }
    const sel = h("select", { "aria-label": "Matrix file" }, mats.map(m => h("option", { value: m }, m)));
    const text = h("textarea", { rows: 28, spellcheck: "false", "aria-label": "Matrix YAML" });
    const msg = h("div");
    let dirty = false;
    const setDirty = v => { dirty = v; text.dataset.dirty = v ? "1" : ""; };
    text.addEventListener("input", () => setDirty(true));
    text.addEventListener("keydown", e => {
      if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); const s = text.selectionStart;
        text.setRangeText("  ", s, text.selectionEnd, "end"); setDirty(true); }
    });
    async function load() {
      msg.textContent = "";
      if (!sel.value) { text.value = ""; return; }
      try { const r = await api(`matrices/${enc(sel.value)}`); text.value = r.text; setDirty(false); } catch (e) { msg.appendChild(banner(e.message, "error")); }
    }
    async function save(name) {
      msg.textContent = "";
      try { await api(`matrices/${enc(name)}`, { method: "PUT", body: { text: text.value } }); setDirty(false);
        msg.appendChild(banner(`Saved ${name}.`)); return true; } catch (e) { msg.appendChild(banner(e.message, "error")); return false; }
    }
    let current = sel.value;
    sel.addEventListener("change", () => {
      if (dirty && !confirm("Discard unsaved changes?")) { sel.value = current; return; }
      current = sel.value; load();
    });
    const bSave = h("button", { class: "primary", onclick: () => sel.value && save(sel.value) }, "Save");
    const bCheck = h("button", { onclick: async () => {
      if (dirty && !(await save(sel.value))) return;
      msg.textContent = ""; msg.appendChild(banner("Checking on the device..."));
      try {
        const r = await api(`matrices/${enc(sel.value)}/check`, { method: "POST", body: {} });
        msg.textContent = "";
        msg.appendChild(r.ok ? banner(`Valid: ${r.matrix.scenarios.length} scenario(s): ${r.matrix.scenarios.map(s => s.name).join(", ")}`)
          : banner(r.error, "error"));
      } catch (e) { msg.textContent = ""; msg.appendChild(banner(e.message, "error")); }
    } }, "Check");
    const bNew = h("button", { onclick: async () => {
      let name = prompt("New matrix file name (letters, digits, . _ -), ending in .yaml:", "my-matrix.yaml");
      if (!name) return;
      if (!/\.ya?ml$/.test(name)) name += ".yaml";
      if (mats.includes(name)) { alert("That file exists already."); return; }
      text.value = `# ${name}\nsession:\n  runs: 3\n  warmup: 1\n\nscenarios:\n  - name: vkmark-1080p\n    title: vkmark\n    kind: vkmark\n    vkmark:\n      winsys: headless\n      size: 1920x1080\n      benchmarks: [vertex:duration=10, shading:shading=phong:duration=10]\n`;
      if (await save(name)) { mats.push(name); sel.appendChild(h("option", { value: name, selected: true }, name)); }
    } }, "New file");
    setPage("Matrices",
      h("div", { class: "pagehead" }, h("div", { class: "grow" }, h("h1", null, "Matrices"),
        h("p", { class: "muted" }, "Scenario files in matrices/. The README lists every option."))),
      card(h("div", { class: "btnrow" }, sel, bSave, bCheck, bNew), h("div", { style: null }, msg), text));
    load();
  }
  window.addEventListener("beforeunload", e => {
    const t = main.querySelector("textarea");
    if (t && t.dataset.dirty === "1") { e.preventDefault(); e.returnValue = ""; }
  });

  // ---------------------------------------------------------------- start
  function showError(msg) {
    const b = banner("Something went wrong in the page: " + msg, "error");
    main.insertBefore(b, main.firstChild);
  }
  window.addEventListener("error", e => showError(e.message || String(e.error)));
  window.addEventListener("unhandledrejection", e => showError((e.reason && e.reason.message) || String(e.reason)));
  refreshInfo().then(route);
})();
