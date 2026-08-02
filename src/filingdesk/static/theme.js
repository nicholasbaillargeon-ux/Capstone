/* Theme toggle, shared by both pages.
 *
 * Loaded WITHOUT defer and before the stylesheet's first paint, so the saved
 * choice is stamped on <html> before anything renders — deferring it means a
 * light flash on every load for anyone using dark.
 *
 * Charts read their colours from CSS custom properties at draw time, so a
 * change has to tell them to redraw; hence the event rather than a direct
 * call, which keeps this file unaware of whether a chart exists.
 */
(function () {
  "use strict";

  var KEY = "fd-theme";

  try {
    var saved = localStorage.getItem(KEY);
    if (saved === "dark" || saved === "light") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (e) {
    /* private mode / storage disabled — fall back to the OS preference */
  }

  function current() {
    return document.documentElement.dataset.theme
      || (window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark" : "light");
  }

  function wire() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.setAttribute("aria-pressed", current() === "dark" ? "true" : "false");
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      btn.setAttribute("aria-pressed", next === "dark" ? "true" : "false");
      try {
        localStorage.setItem(KEY, next);
      } catch (e) { /* not fatal — the page still switches */ }
      window.dispatchEvent(new CustomEvent("fd:themechange",
        { detail: { theme: next } }));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
