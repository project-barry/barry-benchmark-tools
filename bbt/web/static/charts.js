/* SVG charts for the web app: lineChart (time series, crosshair tooltip) and
   dotPlot (one value per run). No dependencies. Colors come from CSS tokens
   (--series-N, --grid, --axis, ...), so light/dark follow the page theme.
   Marks: 2px lines, >= 8px dots with a 2px surface ring, hairline solid grid,
   one y-axis per chart, legend for >= 2 series, values in text tokens. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs, style) {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs || {}) el.setAttribute(k, attrs[k]);
    for (const k in style || {}) el.style[k] = style[k];
    return el;
  }
  function div(cls, text) {
    const d = document.createElement("div");
    if (cls) d.className = cls;
    if (text != null) d.textContent = text;
    return d;
  }

  function niceStep(span, count) {
    const raw = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n >= 7 ? 10 : n >= 3 ? 5 : n >= 1.5 ? 2 : 1) * mag;
  }
  function ticks(min, max, count) {
    if (!isFinite(min) || !isFinite(max)) return { min: 0, max: 1, ticks: [0, 1] };
    if (min === max) { min -= 1; max += 1; }
    const step = niceStep(max - min, count);
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return { min: lo, max: hi, ticks: out, step };
  }
  function tickFmt(step) {
    const dec = Math.max(0, Math.min(3, -Math.floor(Math.log10(step) + 1e-9)));
    return v => Math.abs(v) >= 10000 ? Math.round(v).toLocaleString() : v.toFixed(dec);
  }
  function fmtDefault(v) {
    if (v == null || !isFinite(v)) return "-";
    const a = Math.abs(v);
    if (a >= 10000) return Math.round(v).toLocaleString();
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }

  function frame(container, opts) {
    container.textContent = "";
    const root = div("chart");
    if (opts.title) root.appendChild(div("title", opts.title));
    if (opts.subtitle) root.appendChild(div("subtitle", opts.subtitle));
    if (opts.legend && opts.legend.length > 1) {
      const lg = div("legend");
      opts.legend.forEach(s => {
        const sp = document.createElement("span");
        const key = document.createElement("i");
        key.style.background = s.color;
        if (s.shape === "dot") { key.style.width = key.style.height = "8px"; key.style.borderRadius = "50%"; }
        sp.appendChild(key);
        sp.appendChild(document.createTextNode(s.name));
        lg.appendChild(sp);
      });
      root.appendChild(lg);
    }
    const plot = div("");
    plot.style.position = "relative";
    root.appendChild(plot);
    const tip = div("tooltip hidden");
    plot.appendChild(tip);
    container.appendChild(root);
    return { root, plot, tip };
  }

  function placeTip(plot, tip, x, y) {
    tip.classList.remove("hidden");
    const pw = plot.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = x + 14;
    if (left + tw > pw) left = x - tw - 14;
    if (left < 0) left = Math.max(0, Math.min(pw - tw, x - tw / 2));
    tip.style.left = left + "px";
    tip.style.top = Math.max(0, y - th / 2) + "px";
  }
  function tipRow(tip, color, value, name) {
    const row = div("tt-row");
    if (color) { const k = document.createElement("i"); k.style.background = color; row.appendChild(k); }
    const b = document.createElement("b"); b.textContent = value; row.appendChild(b);
    if (name) { const s = document.createElement("span"); s.textContent = name; row.appendChild(s); }
    tip.appendChild(row);
  }

  function observe(el, fn) {
    let raf = 0, lastW = 0;
    const run = () => { const w = el.clientWidth; if (w && w !== lastW) { lastW = w; fn(w); } };
    if (window.ResizeObserver) {
      const ro = new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(run); });
      ro.observe(el);
    } else {
      window.addEventListener("resize", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(run); });
    }
    requestAnimationFrame(run);
  }

  /* opts: {title, subtitle, x:[..], series:[{name, values, color}], band:{lo, hi, color},
            window:[x0,x1], windowLabel, height, yMin, yMax, fmtY, fmtX, xLabel, unit} */
  function lineChart(container, opts) {
    const series = opts.series.filter(s => s.values && s.values.some(v => v != null));
    const f = frame(container, { title: opts.title, subtitle: opts.subtitle,
      legend: series.map(s => ({ name: s.name, color: s.color })) });
    const fmtY = opts.fmtY || fmtDefault;
    const fmtX = opts.fmtX || (v => v.toFixed(1) + " s");
    const unit = opts.unit ? " " + opts.unit : "";
    const H = opts.height || 200, M = { l: 46, r: 10, t: 8, b: 24 };
    const xs = opts.x;
    if (!xs || !xs.length || !series.length) { f.plot.appendChild(div("muted small", "No data.")); return; }
    let lo = Infinity, hi = -Infinity;
    // yFit "window": scale to the points inside opts.window (loading spikes outside it are clipped)
    const inWin = i => !(opts.yFit === "window" && opts.window) || (xs[i] >= opts.window[0] && xs[i] <= opts.window[1]);
    const scan = arr => arr && arr.forEach((v, i) => { if (v != null && isFinite(v) && inWin(i)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    series.forEach(s => scan(s.values));
    if (opts.band) { scan(opts.band.lo); scan(opts.band.hi); }
    if (opts.yMin != null) lo = Math.min(lo, opts.yMin);
    if (opts.yMax != null) hi = Math.max(hi, opts.yMax);
    const yt = ticks(opts.yMin != null ? opts.yMin : lo, hi, 4);
    const yTick = tickFmt(yt.step);
    const x0 = xs[0], x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];

    function draw(W) {
      [...f.plot.querySelectorAll("svg")].forEach(n => n.remove());
      const iw = W - M.l - M.r, ih = H - M.t - M.b;
      const X = v => M.l + (v - x0) / (x1 - x0) * iw;
      const Y = v => M.t + ih - (v - yt.min) / (yt.max - yt.min) * ih;
      const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
        "aria-label": (opts.title || "chart") + (opts.subtitle ? ". " + opts.subtitle : "") });
      const clipId = "clip" + Math.random().toString(36).slice(2);
      const cp = svgEl("clipPath", { id: clipId });
      cp.appendChild(svgEl("rect", { x: M.l, y: M.t - 2, width: iw, height: ih + 4 }));
      svg.appendChild(cp);
      // window: wash outside the captured range
      if (opts.window) {
        const [w0, w1] = opts.window;
        [[x0, Math.max(x0, w0)], [Math.min(x1, w1), x1]].forEach(([a, b]) => {
          if (b > a) svg.appendChild(svgEl("rect", { x: X(a), y: M.t, width: Math.max(0, X(b) - X(a)), height: ih },
            { fill: "var(--surface-2)" }));
        });
        if (opts.windowLabel) {
          const need = opts.windowLabel.length * 5.6 + 8;
          const spot = [[x0, w0], [w1, x1]].find(([a, b]) => b > a && X(Math.min(b, x1)) - X(Math.max(a, x0)) >= need);
          if (spot) {
            const t = svgEl("text", { x: X(Math.max(spot[0], x0)) + 4, y: M.t + 12, "font-size": 10 }, { fill: "var(--muted)" });
            t.textContent = opts.windowLabel;
            svg.appendChild(t);
          }
        }
      }
      // grid + y ticks
      yt.ticks.forEach(v => {
        svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), "stroke-width": 1, "shape-rendering": "crispEdges" },
          { stroke: v === yt.min ? "var(--axis)" : "var(--grid)" }));
        const t = svgEl("text", { x: M.l - 6, y: Y(v) + 3.5, "text-anchor": "end", "font-size": 11 },
          { fill: "var(--muted)", fontVariantNumeric: "tabular-nums" });
        t.textContent = yTick(v);
        svg.appendChild(t);
      });
      // x ticks (every ~70 px, labels that fit inside the plot)
      const xt = ticks(x0, x1, Math.max(2, Math.floor(iw / 70)));
      const xTick = opts.fmtXTick || (v => tickFmt(xt.step)(v) + " s");
      xt.ticks.filter(v => v >= x0 && v <= x1 && X(v) > M.l + 12 && X(v) < W - M.r - 12).forEach(v => {
        const t = svgEl("text", { x: X(v), y: H - 6, "text-anchor": "middle", "font-size": 11 },
          { fill: "var(--muted)", fontVariantNumeric: "tabular-nums" });
        t.textContent = xTick(v);
        svg.appendChild(t);
      });
      // band (min-max envelope of the first series)
      if (opts.band) {
        let d = "", back = "";
        xs.forEach((x, i) => {
          if (opts.band.hi[i] == null) return;
          d += (d ? "L" : "M") + X(x).toFixed(1) + "," + Y(opts.band.hi[i]).toFixed(1);
        });
        for (let i = xs.length - 1; i >= 0; i--) {
          if (opts.band.lo[i] == null) continue;
          back += "L" + X(xs[i]).toFixed(1) + "," + Y(opts.band.lo[i]).toFixed(1);
        }
        if (d) svg.appendChild(svgEl("path", { d: d + back + "Z", "clip-path": `url(#${clipId})` }, { fill: opts.band.color, opacity: 0.14 }));
      }
      // lines
      series.forEach(s => {
        let d = "", pen = false;
        s.values.forEach((v, i) => {
          if (v == null || !isFinite(v)) { pen = false; return; }
          d += (pen ? "L" : "M") + X(xs[i]).toFixed(1) + "," + Y(v).toFixed(1);
          pen = true;
        });
        svg.appendChild(svgEl("path", { d, fill: "none", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
          "clip-path": `url(#${clipId})` }, { stroke: s.color }));
      });
      // hover layer
      const cross = svgEl("line", { y1: M.t, y2: M.t + ih, "stroke-width": 1, visibility: "hidden" }, { stroke: "var(--ink-2)" });
      svg.appendChild(cross);
      const dots = series.map(s => {
        const c = svgEl("circle", { r: 4, "stroke-width": 2, visibility: "hidden" }, { fill: s.color, stroke: "var(--surface)" });
        svg.appendChild(c);
        return c;
      });
      const hit = svgEl("rect", { x: M.l, y: M.t, width: iw, height: ih, fill: "transparent", tabindex: 0,
        "aria-label": "Chart values: use left and right arrow keys" });
      svg.appendChild(hit);
      let idx = -1;
      function show(i) {
        idx = Math.max(0, Math.min(xs.length - 1, i));
        const px = X(xs[idx]);
        cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
        f.tip.textContent = "";
        f.tip.appendChild(div("tt-x", fmtX(xs[idx])));
        let ty = null;
        series.forEach((s, j) => {
          const v = s.values[idx];
          if (v == null) { dots[j].setAttribute("visibility", "hidden"); return; }
          const cy = Math.max(M.t, Math.min(M.t + ih, Y(v)));
          dots[j].setAttribute("cx", px); dots[j].setAttribute("cy", cy); dots[j].setAttribute("visibility", "visible");
          ty = ty == null ? cy : ty;
          tipRow(f.tip, s.color, fmtY(v) + unit, series.length > 1 ? s.name : (opts.valueName || ""));
        });
        if (opts.band && opts.band.hi[idx] != null) {
          tipRow(f.tip, null, fmtY(opts.band.lo[idx]) + " - " + fmtY(opts.band.hi[idx]) + unit, opts.bandName || "range");
        }
        placeTip(f.plot, f.tip, px, ty == null ? M.t + ih / 2 : ty);
      }
      function hide() {
        cross.setAttribute("visibility", "hidden");
        dots.forEach(d => d.setAttribute("visibility", "hidden"));
        f.tip.classList.add("hidden");
      }
      function nearest(clientX) {
        const r = svg.getBoundingClientRect();
        const vx = (clientX - r.left) * (W / r.width);
        const xv = x0 + (vx - M.l) / iw * (x1 - x0);
        let a = 0, b = xs.length - 1;
        while (b - a > 1) { const m = (a + b) >> 1; if (xs[m] < xv) a = m; else b = m; }
        return Math.abs(xs[a] - xv) <= Math.abs(xs[b] - xv) ? a : b;
      }
      hit.addEventListener("pointermove", e => show(nearest(e.clientX)));
      hit.addEventListener("pointerdown", e => show(nearest(e.clientX)));
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("blur", hide);
      hit.addEventListener("focus", () => show(idx < 0 ? xs.length - 1 : idx));
      hit.addEventListener("keydown", e => {
        const step = e.shiftKey ? 10 : 1;
        if (e.key === "ArrowLeft") { show(idx - step); e.preventDefault(); }
        if (e.key === "ArrowRight") { show(idx + step); e.preventDefault(); }
        if (e.key === "Escape") hide();
      });
      f.plot.insertBefore(svg, f.tip);
    }
    observe(f.plot, draw);
  }

  /* opts: {title, subtitle, items:[{label, value, muted, note}], unit, fmt, median, height} */
  function dotPlot(container, opts) {
    const items = opts.items.filter(it => it.value != null);
    const legend = [{ name: "measured run", color: "var(--series-1)", shape: "dot" }];
    if (items.some(it => it.muted)) legend.push({ name: "warm-up (discarded)", color: "var(--deemph)", shape: "dot" });
    const f = frame(container, { title: opts.title, subtitle: opts.subtitle, legend });
    if (!items.length) { f.plot.appendChild(div("muted small", "No data.")); return; }
    const fmt = opts.fmt || fmtDefault;
    const unit = opts.unit ? " " + opts.unit : "";
    const H = opts.height || 170, M = { l: 52, r: 12, t: 12, b: 24 };
    const vals = items.map(i => i.value);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.25, Math.abs(hi) * 0.01, 1e-9);
    const yt = ticks(lo - pad, hi + pad, 4);

    function draw(W) {
      [...f.plot.querySelectorAll("svg")].forEach(n => n.remove());
      const iw = W - M.l - M.r, ih = H - M.t - M.b;
      const band = iw / items.length;
      const X = i => M.l + band * (i + 0.5);
      const Y = v => M.t + ih - (v - yt.min) / (yt.max - yt.min) * ih;
      const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
        "aria-label": (opts.title || "values per run") });
      yt.ticks.forEach(v => {
        svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), "stroke-width": 1, "shape-rendering": "crispEdges" },
          { stroke: v === yt.min ? "var(--axis)" : "var(--grid)" }));
        const t = svgEl("text", { x: M.l - 6, y: Y(v) + 3.5, "text-anchor": "end", "font-size": 11 },
          { fill: "var(--muted)", fontVariantNumeric: "tabular-nums" });
        t.textContent = tickFmt(yt.step)(v);
        svg.appendChild(t);
      });
      if (opts.median != null) {
        svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: Y(opts.median), y2: Y(opts.median), "stroke-width": 1 },
          { stroke: "var(--ink-2)" }));
      }
      items.forEach((it, i) => {
        const t = svgEl("text", { x: X(i), y: H - 6, "text-anchor": "middle", "font-size": 11 }, { fill: "var(--muted)" });
        t.textContent = it.label;
        svg.appendChild(t);
        const g = svgEl("g", { tabindex: 0, role: "img", "aria-label": `${it.label}: ${fmt(it.value)}${unit}${it.note ? ", " + it.note : ""}` },
          { cursor: "default", outline: "none" });
        g.appendChild(svgEl("circle", { cx: X(i), cy: Y(it.value), r: 14, fill: "transparent" }));
        const dot = svgEl("circle", { cx: X(i), cy: Y(it.value), r: 5, "stroke-width": 2 },
          { fill: it.muted ? "var(--deemph)" : "var(--series-1)", stroke: "var(--surface)" });
        g.appendChild(dot);
        const on = () => {
          dot.setAttribute("r", 6.5);
          f.tip.textContent = "";
          f.tip.appendChild(div("tt-x", it.label + (it.muted ? " (warm-up, discarded)" : "")));
          tipRow(f.tip, null, fmt(it.value) + unit, opts.valueName || "");
          if (it.note) f.tip.appendChild(div("tt-x", it.note));
          placeTip(f.plot, f.tip, X(i), Y(it.value));
        };
        const off = () => { dot.setAttribute("r", 5); f.tip.classList.add("hidden"); };
        g.addEventListener("pointerenter", on); g.addEventListener("pointerleave", off);
        g.addEventListener("focus", on); g.addEventListener("blur", off);
        svg.appendChild(g);
      });
      f.plot.insertBefore(svg, f.tip);
    }
    observe(f.plot, draw);
  }

  window.BBTCharts = { lineChart, dotPlot, fmt: fmtDefault };
})();
