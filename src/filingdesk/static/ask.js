/* The ask page's company field.
 *
 * One job: turn the ticker input into the same type-ahead the dashboard has.
 * The field is still what the form posts, so a pick writes the ticker into it
 * rather than clearing it, and everything else on this page — the run, the
 * stages, the report — is htmx and server-rendered HTML.
 *
 * Nothing here is required for the page to work. With JavaScript off the same
 * input is a plain text box, the form posts normally, and the server resolves
 * whatever was typed.
 */
(function () {
  "use strict";

  const input = document.getElementById("ask-ticker");
  const list = document.getElementById("ask-results");
  if (!input || !list || !window.FDCombobox) return;

  FDCombobox({
    input: input,
    list: list,
    base: document.body.dataset.base || "",
    clearOnPick: false,          // the box IS the submitted ticker
    onPick: function () {
      // Straight to the question: picking a company is never the last thing
      // anyone wants to do on this page.
      const q = document.querySelector('.ask input[name="question"]');
      if (q && !q.value) q.focus();
    },
  });

  // An example button carries its own ticker. Keep the field showing what the
  // request actually ran with, or the page contradicts itself.
  document.querySelectorAll(".example[hx-vals]").forEach(function (b) {
    b.addEventListener("click", function () {
      try {
        const vals = JSON.parse(b.getAttribute("hx-vals"));
        if (vals.ticker) input.value = vals.ticker;
        const q = document.querySelector('.ask input[name="question"]');
        if (q && vals.question) q.value = vals.question;
      } catch (e) { /* the button still works; htmx sends the values */ }
    });
  });
})();
