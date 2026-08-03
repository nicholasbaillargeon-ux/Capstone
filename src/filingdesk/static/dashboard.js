/* Filing Desk dashboard controller.
 *
 * State lives in one object and every change goes through render(), so the
 * KPI tiles, the charts and the table view are always drawn from one payload
 * and can never disagree about what a quarter was.
 */
(function () {
  "use strict";

  const C = window.FDCharts;

  /* Where this instance is mounted, stamped on <body> by the template from
   * FD_BASE_PATH. Empty when the app owns the root; "/filing-desk" when a
   * proxy serves it under a prefix, which every URL built below has to carry
   * or it resolves against whatever else lives at that root. */
  const BASE = document.body.dataset.base || "";
  const state = {
    ticker: document.body.dataset.ticker || "NVDA",
    period: "quarterly",
    limit: 12,
    concept: "Revenues",
    data: null,
    busy: false,
  };

  const $ = sel => document.querySelector(sel);
  const host = $("#company");

  /* A blank "Loading…" that never resolves is the worst failure mode this
   * page has: it looks like the server is down when the real cause is a
   * script error. Surface it in the page instead of only the console. */
  function fatal(what, err) {
    if (!host) return;
    host.replaceChildren();
    const box = document.createElement("section");
    box.className = "panel refusal";
    const head = document.createElement("div");
    head.className = "panel-head";
    const lab = document.createElement("p");
    lab.className = "label";
    lab.textContent = "Page error";
    head.appendChild(lab);
    box.appendChild(head);
    const body = document.createElement("div");
    body.className = "panel-body";
    const p = document.createElement("p");
    p.className = "refusal-text";
    p.textContent = what + ": " + (err && err.message ? err.message : err);
    body.appendChild(p);
    const n = document.createElement("p");
    n.className = "note";
    n.textContent = "The API still works — try " + BASE + "/api/health or "
      + BASE + "/api/company/NVDA directly to confirm the server is fine.";
    body.appendChild(n);
    box.appendChild(body);
    host.appendChild(box);
  }
  window.addEventListener("error", e => fatal("Script error", e.error || e.message));
  window.addEventListener("unhandledrejection", e => fatal("Request failed", e.reason));

  /* ---- theme -------------------------------------------------------
   * theme.js owns the toggle (both pages share it). Charts sample their
   * colours from CSS at draw time, so they have to be redrawn on a change. */
  window.addEventListener("fd:themechange", function () { render(); });

  /* ---- health ------------------------------------------------------ */
  function health() {
    fetch(BASE + "/api/health/html")
      .then(r => r.text())
      .then(html => { $("#health").innerHTML = html; })   // our own markup
      .catch(() => {});
  }
  health();
  setInterval(health, 30000);

  /* ---- company search --------------------------------------------- */
  const input = $("#q"), results = $("#results");
  let searchTimer = null, hits = [], active = -1;

  function closeResults() {
    results.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  }

  function renderResults() {
    results.replaceChildren();
    hits.forEach(function (h, i) {
      const li = document.createElement("li");
      li.role = "option";
      li.className = "result" + (i === active ? " is-active" : "");
      li.setAttribute("aria-selected", i === active ? "true" : "false");
      const t = document.createElement("b");
      t.textContent = h.ticker;
      const n = document.createElement("span");
      n.textContent = h.name;                       // untrusted -> textContent
      li.append(t, n);
      li.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        pick(h.ticker);
      });
      results.appendChild(li);
    });
    results.hidden = !hits.length;
    input.setAttribute("aria-expanded", hits.length ? "true" : "false");
  }

  function pick(ticker) {
    state.ticker = ticker;
    state.concept = "Revenues";
    input.value = "";
    closeResults();
    load();
  }

  input.addEventListener("input", function () {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (!q) { hits = []; renderResults(); return; }
    searchTimer = setTimeout(function () {
      fetch(BASE + "/api/companies/search?q=" + encodeURIComponent(q) + "&limit=10")
        .then(r => r.json())
        .then(function (j) { hits = j.results || []; active = -1; renderResults(); })
        .catch(() => {});
    }, 130);
  });

  input.addEventListener("keydown", function (ev) {
    if (results.hidden) return;
    if (ev.key === "ArrowDown") { active = Math.min(active + 1, hits.length - 1); renderResults(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { active = Math.max(active - 1, 0); renderResults(); ev.preventDefault(); }
    else if (ev.key === "Enter" && active >= 0) { pick(hits[active].ticker); ev.preventDefault(); }
    else if (ev.key === "Escape") closeResults();
  });
  input.addEventListener("blur", () => setTimeout(closeResults, 120));

  document.querySelectorAll("[data-jump]").forEach(function (b) {
    b.addEventListener("click", () => pick(b.dataset.jump));
  });

  /* ---- filters ----------------------------------------------------- */
  function segmented(attr, apply) {
    document.querySelectorAll("[" + attr + "]").forEach(function (b) {
      b.addEventListener("click", function () {
        const group = b.closest(".segmented");
        group.querySelectorAll("button").forEach(function (o) {
          o.classList.toggle("is-on", o === b);
          o.setAttribute("aria-checked", o === b ? "true" : "false");
        });
        apply(b.getAttribute(attr));
        load();
      });
    });
  }
  segmented("data-period", v => { state.period = v; });
  segmented("data-limit", v => { state.limit = Number(v); });

  $("#refresh").addEventListener("click", function () {
    const btn = $("#refresh");
    btn.disabled = true;
    btn.textContent = "↻ Syncing…";
    fetch(BASE + "/api/company/" + encodeURIComponent(state.ticker) + "/refresh",
      { method: "POST" })
      .then(r => r.json())
      .then(function () { load(); })
      .catch(() => {})
      .finally(function () {
        btn.disabled = false;
        btn.textContent = "↻ Force re-sync";
        health();
      });
  });

  /* ---- load & render ----------------------------------------------- */
  function load() {
    state.busy = true;
    host.classList.add("is-loading");    // hold the frame, no skeleton flash
    const url = BASE + "/api/company/" + encodeURIComponent(state.ticker)
      + "?period=" + state.period + "&limit=" + state.limit
      + "&concept=" + encodeURIComponent(state.concept);
    fetch(url)
      .then(r => r.json())
      .then(function (j) {
        state.data = j;
        if (j.ok) {
          state.concept = j.concept;
          history.replaceState(null, "", "/?ticker=" + j.ticker);
          document.title = j.ticker + " — Filing Desk";
        }
        render();
      })
      .catch(function (e) {
        state.data = { ok: false, error: String(e), error_kind: "network" };
        render();
      })
      .finally(function () {
        state.busy = false;
        host.classList.remove("is-loading");
      });
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function deltaNode(d, unit) {
    if (!d) return el("span", "delta delta-flat", "no prior year");
    const up = d.pct >= 0;
    const n = el("span", "delta " + (up ? "delta-up" : "delta-down"));
    // Arrow + sign + label: direction never rests on colour alone.
    n.textContent = (up ? "▲ +" : "▼ ") + (d.pct * 100).toFixed(1) + "% "
      + (d.lag === 4 ? "vs year ago" : "vs prior period");
    return n;
  }

  function ago(iso) {
    const secs = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(secs)) return "";
    if (secs < 90) return "just now";
    if (secs < 5400) return Math.round(secs / 60) + " min ago";
    if (secs < 172800) return Math.round(secs / 3600) + " h ago";
    return Math.round(secs / 86400) + " d ago";
  }

  /* Says whether these figures are current, so the reader never has to press
   * a button to find out. The page syncs itself on load; the button is an
   * override, not a step. */
  function syncStatus(d) {
    const line = el("p", "sync");
    line.appendChild(el("span", "sync-dot"));
    const when = d.loaded.refreshed ? ago(d.loaded.refreshed) : "unknown";
    if (d.offline) {
      line.classList.add("is-stale");
      line.appendChild(document.createTextNode(
        "EDGAR unreachable — showing cached filings from " + when));
    } else if (d.synced_now) {
      line.appendChild(document.createTextNode(
        "Synced with SEC EDGAR just now"));
    } else {
      line.appendChild(document.createTextNode(
        "Synced with SEC EDGAR " + when + " — refreshes automatically"));
    }
    return line;
  }

  function kpiRow(data) {
    const row = el("div", "kpis");
    data.kpis.forEach(function (k) {
      const card = el("div", "kpi");
      card.appendChild(el("p", "kpi-label", k.label));
      const v = el("p", "kpi-value", C.fmt(k.value, k.unit));
      card.appendChild(v);
      const meta = el("div", "kpi-meta");
      meta.appendChild(deltaNode(k.delta, k.unit));
      card.appendChild(meta);
      const foot = el("div", "kpi-foot");
      const per = el("span", "kpi-period",
        C.periodLabel(k.end, data.period) + " · " + k.end
        + (k.derived ? " · derived" : ""));
      foot.appendChild(per);
      const spark = el("span", "kpi-spark");
      foot.appendChild(spark);
      card.appendChild(foot);
      row.appendChild(card);
      C.sparkline(spark, k.spark);
    });
    return row;
  }

  /* A chart card: title, legend, chart, table-view toggle. */
  function chartCard(opts) {
    const card = el("section", "panel chart-card");
    const head = el("div", "panel-head");
    head.appendChild(el("p", "label", opts.title));
    if (opts.note) head.appendChild(el("span", "chip", opts.note));
    const spacer = el("span", "spacer");
    head.appendChild(spacer);
    const legendBox = el("div", "legend");
    head.appendChild(legendBox);
    const toggle = el("button", "ghost small", "Table view");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    head.appendChild(toggle);
    card.appendChild(head);

    if (opts.select) card.appendChild(opts.select);

    const body = el("div", "chart-body");
    card.appendChild(body);
    const tableBox = el("div", "table-scroll table-view");
    tableBox.hidden = true;
    card.appendChild(tableBox);

    if (opts.caption) card.appendChild(el("p", "chart-caption", opts.caption));

    toggle.addEventListener("click", function () {
      const showing = !tableBox.hidden;
      tableBox.hidden = showing;
      body.hidden = !showing;
      toggle.textContent = showing ? "Table view" : "Chart view";
      toggle.setAttribute("aria-expanded", showing ? "false" : "true");
    });

    // Deferred so clientWidth is real when the chart measures itself.
    requestAnimationFrame(function () {
      C.draw(body, opts);
      C.legend(legendBox, opts.series);
      C.table(tableBox, opts.series, opts.period);
    });
    card._redraw = function () {
      C.draw(body, opts);
      C.legend(legendBox, opts.series);
    };
    return card;
  }

  function conceptPicker(data) {
    const wrap = el("div", "concept-picker");
    const lab = el("label", "label", "Line item");
    lab.htmlFor = "concept-select";
    wrap.appendChild(lab);
    const sel = document.createElement("select");
    sel.id = "concept-select";
    sel.className = "select";
    const groups = {};
    data.concepts.forEach(function (c) {
      (groups[c.group] = groups[c.group] || []).push(c);
    });
    Object.keys(groups).forEach(function (g) {
      const og = document.createElement("optgroup");
      og.label = g;
      groups[g].forEach(function (c) {
        const o = document.createElement("option");
        o.value = c.key;
        o.textContent = c.label;
        if (c.key === data.concept) o.selected = true;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    sel.addEventListener("change", function () {
      state.concept = sel.value;
      load();
    });
    wrap.appendChild(sel);
    return wrap;
  }

  let cards = [];

  function render() {
    const d = state.data;
    host.replaceChildren();
    cards = [];
    if (!d) return;

    if (!d.ok) {
      const box = el("section", "panel refusal");
      const head = el("div", "panel-head");
      head.appendChild(el("p", "label", "No data"));
      box.appendChild(head);
      const body = el("div", "panel-body");
      body.appendChild(el("p", "refusal-text", d.error || "Unknown error."));
      if (d.did_you_mean && d.did_you_mean.length) {
        const p = el("p", "note");
        p.append("Did you mean: ");
        d.did_you_mean.forEach(function (h) {
          const b = el("button", "pill-btn", h.ticker);
          b.type = "button";
          b.addEventListener("click", () => pick(h.ticker));
          p.append(b, " ");
        });
        body.appendChild(p);
      }
      box.appendChild(body);
      host.appendChild(box);
      return;
    }

    // ---- company header
    const hd = el("section", "company-head");
    const left = el("div");
    left.appendChild(el("h2", "company-name", d.name || d.ticker));
    const sub = el("p", "company-sub");
    sub.append(el("span", "ticker-badge", d.ticker));
    sub.append(" CIK " + String(d.cik).padStart(10, "0") + " · "
      + (d.loaded.n_facts || 0).toLocaleString() + " XBRL facts");
    left.appendChild(sub);
    left.appendChild(syncStatus(d));
    hd.appendChild(left);
    host.appendChild(hd);

    // ---- KPI row
    if (d.kpis.length) host.appendChild(kpiRow(d));

    // ---- primary series, driven by the concept picker
    if (d.primary) {
      const derived = d.primary.points.filter(p => p.derived).length;
      cards.push(chartCard({
        title: d.primary.label,
        note: d.period === "annual" ? "fiscal years" : "quarters",
        type: "bar",
        period: d.period,
        series: [d.primary],
        select: conceptPicker(d),
        height: 300,
        title_: d.primary.label,
        caption: derived
          ? derived + " period(s) shown with a dashed outline were reconstructed, "
            + "not filed — no company files a 10-Q for Q4, and cash-flow "
            + "statements are filed year to date."
          : "Every bar traces to a filing. Switch to the table view for "
            + "accession numbers.",
      }));
    }

    // ---- margins
    if (d.margins.length) {
      cards.push(chartCard({
        title: "Margin profile",
        type: "line",
        period: d.period,
        series: d.margins,
        height: 280,
        caption: "Ratios are computed in Python from the filed inputs, never "
          + "by the model. Formulas: "
          + d.margins.map(m => m.formula).join("; ") + ".",
      }));
    }

    // ---- cash
    if (d.cash.length) {
      cards.push(chartCard({
        title: "Cash generation",
        type: "line",
        period: d.period,
        series: d.cash,
        height: 280,
        caption: "Free cash flow is operating cash flow minus capital "
          + "expenditure. Cash-flow statements are filed year to date, so "
          + "most quarters here are differenced from the cumulative figure.",
      }));
    }

    cards.forEach(c => host.appendChild(c));
  }

  let resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      cards.forEach(c => c._redraw && c._redraw());
    }, 150);
  });

  // Deep link: /?ticker=AAPL
  const qs = new URLSearchParams(location.search);
  if (qs.get("ticker")) state.ticker = qs.get("ticker").toUpperCase();
  load();
})();
