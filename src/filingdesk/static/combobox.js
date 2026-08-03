/* Company type-ahead over every SEC registrant.
 *
 * Both pages that take a company need this: the dashboard, where picking one
 * loads it, and the ask page, where the picked ticker is what the form
 * submits. It lives here rather than twice because the two copies would have
 * drifted — this repo has already shipped one bug that was exactly that, a
 * stand-in whose signature stopped matching the thing it stood in for.
 *
 * No framework, no fetch on every keystroke (130ms of quiet first), and the
 * company name goes in through textContent: it comes from the SEC's file, and
 * the SEC's file is not markup this app gets to trust.
 */
(function () {
  "use strict";

  /* opts:
   *   input       the <input> inside a .combo
   *   list        the <ul class="results"> to fill
   *   base        mount prefix, "" or "/filing-desk"
   *   onPick      (ticker, hit) => void
   *   clearOnPick leave the box empty after a pick (the dashboard) or put the
   *               ticker in it (the ask form, where it IS the submitted value)
   */
  window.FDCombobox = function (opts) {
    const input = opts.input;
    const list = opts.list;
    const base = opts.base || "";
    const onPick = opts.onPick || function () {};
    let timer = null, hits = [], active = -1;

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      active = -1;
    }

    function render() {
      list.replaceChildren();
      hits.forEach(function (h, i) {
        const li = document.createElement("li");
        li.role = "option";
        li.className = "result" + (i === active ? " is-active" : "");
        li.setAttribute("aria-selected", i === active ? "true" : "false");
        const t = document.createElement("b");
        t.textContent = h.ticker;
        const n = document.createElement("span");
        n.textContent = h.name;                     // untrusted -> textContent
        li.append(t, n);
        li.addEventListener("mousedown", function (ev) {
          ev.preventDefault();                      // pick before blur closes
          pick(h);
        });
        list.appendChild(li);
      });
      list.hidden = !hits.length;
      input.setAttribute("aria-expanded", hits.length ? "true" : "false");
    }

    function pick(hit) {
      input.value = opts.clearOnPick ? "" : hit.ticker;
      hits = [];
      close();
      onPick(hit.ticker, hit);
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { hits = []; render(); return; }
      timer = setTimeout(function () {
        fetch(base + "/api/companies/search?q=" + encodeURIComponent(q)
              + "&limit=10")
          .then(r => r.json())
          .then(function (j) { hits = j.results || []; active = -1; render(); })
          .catch(() => {});
      }, 130);
    });

    input.addEventListener("keydown", function (ev) {
      if (list.hidden) return;
      if (ev.key === "ArrowDown") {
        active = Math.min(active + 1, hits.length - 1); render();
        ev.preventDefault();
      } else if (ev.key === "ArrowUp") {
        active = Math.max(active - 1, 0); render();
        ev.preventDefault();
      } else if (ev.key === "Enter" && active >= 0) {
        // Only swallows Enter when a row is highlighted, so on the ask form a
        // plain Enter still submits the question.
        pick(hits[active]);
        ev.preventDefault();
      } else if (ev.key === "Escape") {
        close();
      }
    });

    // Late enough that a click on a row lands first.
    input.addEventListener("blur", () => setTimeout(close, 120));

    return { pick: pick, close: close };
  };
})();
