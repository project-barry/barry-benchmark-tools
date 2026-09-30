/* Minimal Markdown -> DOM for the harness's own reports: headings, lists (with
   continuation lines), pipe tables (with alignment), paragraphs, `code`,
   **bold**. Builds nodes with textContent only, so report text is never HTML. */
(function () {
  "use strict";

  function inline(parent, text) {
    const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      const el = document.createElement(tok[0] === "`" ? "code" : "strong");
      el.textContent = tok[0] === "`" ? tok.slice(1, -1) : tok.slice(2, -2);
      parent.appendChild(el);
      last = m.index + tok.length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    return parent;
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "";
    for (let i = 0; i < s.length; i++) {
      if (s[i] === "\\" && s[i + 1] === "|") { cur += "|"; i++; continue; }
      if (s[i] === "|") { cells.push(cur.trim()); cur = ""; continue; }
      cur += s[i];
    }
    cells.push(cur.trim());
    return cells;
  }

  function render(md) {
    const root = document.createElement("div");
    root.className = "md";
    const lines = md.replace(/\r/g, "").split("\n");
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) {
        root.appendChild(inline(document.createElement("h" + Math.min(4, h[1].length)), h[2]));
        i++; continue;
      }
      if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
        const head = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map(c => (c.endsWith(":") ? "r" : ""));
        const wrap = document.createElement("div");
        wrap.className = "tablewrap";
        const t = document.createElement("table");
        const thead = t.createTHead().insertRow();
        head.forEach((c, j) => { const th = document.createElement("th"); if (aligns[j]) th.className = "r"; inline(th, c); thead.appendChild(th); });
        const tb = t.createTBody();
        i += 2;
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          const tr = tb.insertRow();
          splitRow(lines[i]).forEach((c, j) => { const td = tr.insertCell(); if (aligns[j]) td.className = "r"; inline(td, c); });
          i++;
        }
        wrap.appendChild(t);
        root.appendChild(wrap);
        continue;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        const ul = document.createElement("ul");
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          let text = lines[i].replace(/^\s*[-*]\s+/, "");
          i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*[-*]\s+/.test(lines[i])) {
            text += " " + lines[i].trim();
            i++;
          }
          ul.appendChild(inline(document.createElement("li"), text));
        }
        root.appendChild(ul);
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*[-*]\s|\s*\|)/.test(lines[i])) {
        para.push(lines[i].trim());
        i++;
      }
      if (!para.length) { para.push(line.trim()); i++; }
      root.appendChild(inline(document.createElement("p"), para.join(" ")));
    }
    return root;
  }

  window.BBTMarkdown = { render };
})();
