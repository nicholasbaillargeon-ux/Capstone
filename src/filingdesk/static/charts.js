/* Filing Desk charts — SVG, no dependencies.
 *
 * Written rather than imported for the same reason the rest of the stack is
 * local: a chart library from a CDN is a network call at request time, and
 * this app's whole claim is that it makes none.
 *
 * Conventions held across every chart here:
 *   - one y-axis, ever (a second scale invents a correlation that is not in
 *     the data)
 *   - categorical hues assigned by series identity in fixed order, capped at
 *     three, never generated or cycled
 *   - 2px lines, <=24px bars with a 4px rounded data-end, hairline solid grid
 *   - a legend whenever there are two or more series, plus selective direct
 *     labels; never a value on every point
 *   - a table view twin, so no value is reachable only by hovering
 */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const svgEl = (name, attrs) => {
    const el = document.createElementNS(NS, name);
    for (const k in attrs || {}) el.setAttribute(k, attrs[k]);
    return el;
  };

  /* ---- formatting ------------------------------------------------- */

  function money(v, digits) {
    const a = Math.abs(v);
    const sign = v < 0 ? "-" : "";
    if (a >= 1e12) return sign + "$" + (a / 1e12).toFixed(digits ?? 2) + "T";
    if (a >= 1e9) return sign + "$" + (a / 1e9).toFixed(digits ?? 1) + "B";
    if (a >= 1e6) return sign + "$" + (a / 1e6).toFixed(digits ?? 1) + "M";
    if (a >= 1e3) return sign + "$" + (a / 1e3).toFixed(digits ?? 1) + "K";
    return sign + "$" + a.toFixed(2);
  }

  function count(v) {
    const a = Math.abs(v), sign = v < 0 ? "-" : "";
    if (a >= 1e9) return sign + (a / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return sign + (a / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return sign + (a / 1e3).toFixed(1) + "K";
    return sign + a.toFixed(0);
  }

  function fmt(v, unit) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    if (unit === "ratio") return (v * 100).toFixed(1) + "%";
    if (unit === "x") return v.toFixed(2) + "×";
    if (unit === "USD/shares") return "$" + v.toFixed(2);
    if (unit === "shares") return count(v);
    return money(v);
  }
  function fmtAxis(v, unit) {
    if (unit === "ratio") return (v * 100).toFixed(0) + "%";
    if (unit === "x") return v.toFixed(1);
    if (unit === "USD/shares") return "$" + v.toFixed(2);
    if (unit === "shares") return count(v);
    return money(v, v && Math.abs(v) >= 1e9 ? 0 : 1);
  }

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  /* Period label. Deliberately NOT "Q1 FY26".
   *
   * A fiscal quarter number cannot be derived from a period end date without
   * the filer's fiscal calendar, and guessing from the calendar quarter is
   * wrong for every off-calendar filer — NVDA's quarter ending 2026-04-26 is
   * its fiscal Q1, but the calendar says Q2. Naming the month the period
   * ended in is the thing we actually know. */
  function periodLabel(end, period) {
    const [y, m] = end.split("-").map(Number);
    if (period === "annual") return "FY" + String(y).slice(2);
    return MONTHS[m - 1] + " " + String(y).slice(2);
  }

  /* ---- scales ------------------------------------------------------ */

  function niceTicks(min, max, target) {
    if (min === max) { min = min - 1; max = max + 1; }
    const span = max - min;
    const raw = span / (target || 4);
    const mag = Math.pow(10, Math.floor(Math.log10(Math.abs(raw) || 1)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(v);
    return out;
  }

  /* ---- tooltip ----------------------------------------------------- */

  let tipEl = null;
  function tooltip() {
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "chart-tip";
      tipEl.setAttribute("role", "status");
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }

  function showTip(html, x, y) {
    const t = tooltip();
    // Rows are built as nodes by the caller; never innerHTML with data.
    t.replaceChildren(html);
    t.style.display = "block";
    const r = t.getBoundingClientRect();
    let left = x + 14, top = y - r.height - 12;
    if (left + r.width > window.innerWidth - 8)
      left = x - r.width - 14;
    if (top < 8) top = y + 18;
    t.style.left = Math.max(8, left) + "px";
    t.style.top = top + "px";
  }
  function hideTip() { if (tipEl) tipEl.style.display = "none"; }

  function tipNode(title, rows) {
    const box = document.createElement("div");
    const h = document.createElement("div");
    h.className = "tip-head";
    h.textContent = title;                       // untrusted -> textContent
    box.appendChild(h);
    rows.forEach(function (r) {
      const line = document.createElement("div");
      line.className = "tip-row";
      const key = document.createElement("span");
      key.className = "tip-key";
      key.style.background = r.color || "transparent";
      line.appendChild(key);
      const val = document.createElement("b");    // value leads
      val.textContent = r.value;
      line.appendChild(val);
      const name = document.createElement("span");
      name.className = "tip-name";
      name.textContent = r.label;
      line.appendChild(name);
      if (r.note) {
        const note = document.createElement("em");
        note.className = "tip-note";
        note.textContent = r.note;
        line.appendChild(note);
      }
      box.appendChild(line);
    });
    return box;
  }

  /* ---- palette ------------------------------------------------------
   * Read from CSS so light/dark and the theme toggle stay in one place. */
  function seriesColor(i) {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue("--series-" + ((i % 3) + 1));
    return (v || "#2a78d6").trim();
  }
  function ink(name) {
    return getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
  }

  /* ---- the chart --------------------------------------------------- */

  const PAD = { top: 18, right: 20, bottom: 34, left: 62 };

  /* opts: {type:'bar'|'line', series:[{label,unit,points:[{end,value,...}]}],
   *        period, height, emphasisLast} */
  function draw(host, opts) {
    host.replaceChildren();
    const series = (opts.series || []).filter(s => s && s.points && s.points.length);
    if (!series.length) {
      const p = document.createElement("p");
      p.className = "chart-empty";
      p.textContent = opts.emptyText || "No data reported for this selection.";
      host.appendChild(p);
      return;
    }

    const unit = series[0].unit;
    const W = host.clientWidth || 640;
    const H = opts.height || 260;
    const iw = Math.max(80, W - PAD.left - PAD.right);
    const ih = Math.max(60, H - PAD.top - PAD.bottom);

    // X domain: the union of every period present, in order.
    const ends = Array.from(new Set(
      series.flatMap(s => s.points.map(p => p.end)))).sort();
    const xAt = i => PAD.left + (ends.length === 1 ? iw / 2
      : (i * iw) / (ends.length - (opts.type === "bar" ? 0 : 1))
        + (opts.type === "bar" ? iw / ends.length / 2 : 0));

    const vals = series.flatMap(s => s.points.map(p => p.value));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (opts.type === "bar") lo = Math.min(0, lo);      // bars grow from zero
    if (unit === "ratio") { lo = Math.min(lo, 0); }
    const ticks = niceTicks(lo, hi, 4);
    const yMin = ticks[0], yMax = ticks[ticks.length - 1];
    const yAt = v => PAD.top + ih - ((v - yMin) / (yMax - yMin || 1)) * ih;

    const svg = svgEl("svg", {
      viewBox: `0 0 ${W} ${H}`, width: "100%", height: H,
      role: "img", class: "chart-svg",
    });
    const gridC = ink("--gridline"), axisC = ink("--baseline"),
      mutedC = ink("--text-muted"), surfaceC = ink("--surface-1");

    // gridlines: solid hairlines, recessive
    ticks.forEach(function (t) {
      svg.appendChild(svgEl("line", {
        x1: PAD.left, x2: PAD.left + iw, y1: yAt(t), y2: yAt(t),
        stroke: t === 0 ? axisC : gridC, "stroke-width": 1,
      }));
      const lab = svgEl("text", {
        x: PAD.left - 8, y: yAt(t) + 4, "text-anchor": "end",
        class: "axis-text", fill: mutedC,
      });
      lab.textContent = fmtAxis(t, unit);
      svg.appendChild(lab);
    });

    // x labels — thinned so they never collide
    const every = Math.ceil(ends.length / Math.max(2, Math.floor(iw / 58)));
    ends.forEach(function (e, i) {
      if (i % every && i !== ends.length - 1) return;
      const lab = svgEl("text", {
        x: xAt(i), y: PAD.top + ih + 20, "text-anchor": "middle",
        class: "axis-text", fill: mutedC,
      });
      lab.textContent = periodLabel(e, opts.period);
      svg.appendChild(lab);
    });

    const hit = [];   // {x, y, end, rows} for the hover layer

    if (opts.type === "bar") {
      const band = iw / ends.length;
      const n = series.length;
      const barW = Math.min(24, Math.max(4, (band - 10) / n - (n > 1 ? 2 : 0)));
      series.forEach(function (s, si) {
        const color = seriesColor(si);
        const byEnd = new Map(s.points.map(p => [p.end, p]));
        ends.forEach(function (e, i) {
          const p = byEnd.get(e);
          if (!p) return;
          const zero = yAt(Math.max(yMin, 0));
          const y = yAt(p.value);
          const h = Math.abs(zero - y);
          const groupW = barW * n + (n - 1) * 2;
          const x = xAt(i) - groupW / 2 + si * (barW + 2);
          const up = p.value >= 0;
          const r = Math.min(4, barW / 2, h);
          // 4px rounded data-end, square at the baseline
          const top = up ? y : zero;
          const path = h < 0.5 ? null : svgEl("path", {
            d: up
              ? `M${x},${top + h} L${x},${top + r} Q${x},${top} ${x + r},${top}
                 L${x + barW - r},${top} Q${x + barW},${top} ${x + barW},${top + r}
                 L${x + barW},${top + h} Z`
              : `M${x},${top} L${x + barW},${top} L${x + barW},${top + h - r}
                 Q${x + barW},${top + h} ${x + barW - r},${top + h}
                 L${x + r},${top + h} Q${x},${top + h} ${x},${top + h - r} Z`,
            fill: color,
            "fill-opacity": p.derived ? 0.55 : 1,
          });
          if (path) {
            if (p.derived) {
              // Derived quarters are not filed. A lighter fill plus a dashed
              // outline marks them without hiding them.
              path.setAttribute("stroke", color);
              path.setAttribute("stroke-width", "1.5");
              path.setAttribute("stroke-dasharray", "3 2");
            }
            svg.appendChild(path);
          }
          hit.push({ x: xAt(i), y: y, end: e });
        });
      });
    } else {
      series.forEach(function (s, si) {
        const color = seriesColor(si);
        const pts = ends.map((e, i) => {
          const p = s.points.find(q => q.end === e);
          return p ? { x: xAt(i), y: yAt(p.value), p: p } : null;
        }).filter(Boolean);
        if (!pts.length) return;
        const d = pts.map((q, i) => (i ? "L" : "M") + q.x + "," + q.y).join(" ");
        svg.appendChild(svgEl("path", {
          d: d, fill: "none", stroke: color, "stroke-width": 2,
          "stroke-linejoin": "round", "stroke-linecap": "round",
        }));
        // end marker: >=8px with a 2px surface ring so crossings stay legible
        const last = pts[pts.length - 1];
        svg.appendChild(svgEl("circle", {
          cx: last.x, cy: last.y, r: 4.5, fill: color,
          stroke: surfaceC, "stroke-width": 2,
        }));
        // selective direct label: the endpoint only
        const lab = svgEl("text", {
          x: Math.min(last.x + 8, PAD.left + iw), y: last.y - 9,
          "text-anchor": last.x > PAD.left + iw - 40 ? "end" : "start",
          class: "axis-text end-label", fill: ink("--text-secondary"),
        });
        lab.textContent = fmt(last.p.value, unit);
        svg.appendChild(lab);
        pts.forEach(q => hit.push({ x: q.x, y: q.y, end: q.p.end }));
      });
    }

    // ---- hover layer: crosshair snapped to the nearest period --------
    const cross = svgEl("line", {
      y1: PAD.top, y2: PAD.top + ih, stroke: axisC, "stroke-width": 1,
      opacity: 0, class: "crosshair",
    });
    svg.appendChild(cross);
    const focusDots = svgEl("g", { opacity: 0 });
    svg.appendChild(focusDots);

    const overlay = svgEl("rect", {
      x: PAD.left, y: PAD.top, width: iw, height: ih,
      fill: "transparent", tabindex: "0", role: "application",
      "aria-label": (opts.title || "chart") + " — arrow keys move between periods",
    });
    svg.appendChild(overlay);

    let focusIdx = -1;
    function highlight(i, clientX, clientY) {
      if (i < 0 || i >= ends.length) return;
      focusIdx = i;
      const e = ends[i];
      const x = xAt(i);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x);
      cross.setAttribute("opacity", 0.6);
      focusDots.replaceChildren();
      const rows = [];
      series.forEach(function (s, si) {
        const p = s.points.find(q => q.end === e);
        if (!p) return;
        const color = seriesColor(si);
        focusDots.appendChild(svgEl("circle", {
          cx: x, cy: yAt(p.value), r: 4.5, fill: color,
          stroke: surfaceC, "stroke-width": 2,
        }));
        rows.push({
          color: color, label: s.label, value: fmt(p.value, s.unit),
          note: p.derived ? "derived" : (p.restated ? "restated" : ""),
        });
      });
      focusDots.setAttribute("opacity", 1);
      const box = svg.getBoundingClientRect();
      showTip(tipNode(e + "  ·  " + periodLabel(e, opts.period), rows),
        clientX !== undefined ? clientX : box.left + x,
        clientY !== undefined ? clientY : box.top + PAD.top + ih / 2);
    }
    function clear() {
      cross.setAttribute("opacity", 0);
      focusDots.setAttribute("opacity", 0);
      hideTip();
    }

    overlay.addEventListener("pointermove", function (ev) {
      const box = svg.getBoundingClientRect();
      const rel = (ev.clientX - box.left) * (W / box.width);
      let bi = 0, bd = Infinity;
      ends.forEach(function (e, i) {
        const d = Math.abs(xAt(i) - rel);
        if (d < bd) { bd = d; bi = i; }
      });
      highlight(bi, ev.clientX, ev.clientY);
    });
    overlay.addEventListener("pointerleave", clear);
    overlay.addEventListener("blur", clear);
    overlay.addEventListener("focus", function () {
      highlight(focusIdx < 0 ? ends.length - 1 : focusIdx);
    });
    overlay.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowRight") { highlight(Math.min(focusIdx + 1, ends.length - 1)); ev.preventDefault(); }
      else if (ev.key === "ArrowLeft") { highlight(Math.max(focusIdx - 1, 0)); ev.preventDefault(); }
      else if (ev.key === "Escape") clear();
    });

    host.appendChild(svg);
  }

  /* ---- sparkline (stat tiles) -------------------------------------- */
  function sparkline(host, values) {
    host.replaceChildren();
    if (!values || values.length < 2) return;
    const W = 104, H = 28, lo = Math.min(...values), hi = Math.max(...values);
    const x = i => (i / (values.length - 1)) * (W - 4) + 2;
    const y = v => H - 3 - ((v - lo) / ((hi - lo) || 1)) * (H - 8);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H,
      class: "spark", "aria-hidden": "true" });
    svg.appendChild(svgEl("path", {
      d: values.map((v, i) => (i ? "L" : "M") + x(i) + "," + y(v)).join(" "),
      fill: "none", stroke: ink("--text-muted"), "stroke-width": 1.5,
      "stroke-linejoin": "round", "stroke-linecap": "round",
    }));
    // current period in the accent, the rest de-emphasised
    svg.appendChild(svgEl("circle", {
      cx: x(values.length - 1), cy: y(values[values.length - 1]), r: 2.5,
      fill: seriesColor(0),
    }));
    host.appendChild(svg);
  }

  /* ---- table view twin --------------------------------------------- */
  function table(host, series, period) {
    host.replaceChildren();
    const list = (series || []).filter(s => s && s.points && s.points.length);
    if (!list.length) return;
    const ends = Array.from(new Set(
      list.flatMap(s => s.points.map(p => p.end)))).sort();
    const t = document.createElement("table");
    t.className = "chart-table";
    const thead = document.createElement("thead");
    const hr = document.createElement("tr");
    ["Period", ...list.map(s => s.label), "Source"].forEach(function (h) {
      const th = document.createElement("th");
      th.textContent = h;
      th.scope = "col";
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    t.appendChild(thead);
    const tb = document.createElement("tbody");
    ends.forEach(function (e) {
      const tr = document.createElement("tr");
      const td0 = document.createElement("th");
      td0.scope = "row";
      td0.textContent = periodLabel(e, period) + " · " + e;
      tr.appendChild(td0);
      let accn = "", derived = false;
      list.forEach(function (s) {
        const p = s.points.find(q => q.end === e);
        const td = document.createElement("td");
        td.className = "num";
        td.textContent = p ? fmt(p.value, s.unit) : "—";
        if (p && p.derived) { td.classList.add("is-derived"); derived = true; }
        if (p && p.accn) accn = p.accn;
        tr.appendChild(td);
      });
      const tds = document.createElement("td");
      tds.className = "src";
      tds.textContent = derived ? (accn ? accn + " (derived)" : "derived") : accn;
      tr.appendChild(tds);
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    host.appendChild(t);
  }

  function legend(host, series) {
    host.replaceChildren();
    const list = (series || []).filter(s => s && s.points && s.points.length);
    if (list.length < 2) return;      // one series: the title already names it
    list.forEach(function (s, i) {
      const item = document.createElement("span");
      item.className = "legend-item";
      const key = document.createElement("span");
      key.className = "legend-key";
      key.style.background = seriesColor(i);
      item.appendChild(key);
      const name = document.createElement("span");
      name.textContent = s.label;
      item.appendChild(name);
      host.appendChild(item);
    });
  }

  global.FDCharts = { draw, sparkline, table, legend, fmt, fmtAxis,
                      periodLabel, seriesColor };
})(window);
