"""Tests for the presentation layer.

The report is model output rendered as HTML, which makes escaping a security
property rather than a formatting detail. The striking tests exist because the
obvious implementation — replace claim text everywhere — corrupts the citation
anchors when a claim happens to look like a fact id.
"""
import asyncio

import pytest

from filingdesk import web

CLAIM = {"claim": "81.2%", "reason": "no matching fact", "sentence": "..."}


def test_citation_marker_becomes_an_anchor():
    out = str(web.render_report("Margin was 73.9% [[fact:24]]."))
    assert 'href="#fact-24"' in out
    assert 'data-fact="24"' in out
    assert "[[fact:24]]" not in out


def test_model_output_is_escaped():
    """report_md comes from a language model. It is never trusted as markup."""
    out = str(web.render_report('<script>alert(1)</script> & "quoted"'))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out and "&quot;" in out


def test_unsupported_claim_is_struck():
    out = str(web.render_report("It peaked at 81.2% mid-period.", [CLAIM]))
    assert '<del class="struck"' in out
    assert "81.2%" in out  # struck and flagged, not deleted


def test_striking_does_not_corrupt_a_marker_matching_its_own_id():
    """A claim of "24" must not rewrite the anchor for [[fact:24]]."""
    out = str(web.render_report(
        "Revenue rose 24 [[fact:24]].",
        [{"claim": "24", "reason": "no matching fact", "sentence": "..."}]))
    assert 'href="#fact-24"' in out
    assert 'data-fact="24"' in out


def test_paragraphs_are_preserved():
    out = str(web.render_report("First line.\n\nSecond line."))
    assert out.count("<p>") == 2


def test_empty_report_renders_nothing():
    assert str(web.render_report("")) == ""
    assert str(web.render_report(None)) == ""


def test_fmt_value_matches_the_fact_table():
    """The UI and the model must show a figure identically or the guard's
    tolerance check and the reader's eye disagree."""
    from filingdesk import agent
    for v in (0.739, 16720000000.0, 45.5):
        facts = [{"concept": "x", "value": v, "end": "2025-01-01", "accn": "a"}]
        assert web.fmt_value(v) in agent.fact_table(facts)


def test_fact_rows_expose_flags_and_computed():
    rows = web.fact_rows([
        {"concept": "GrossProfit", "value": 1.0, "end": "2025-01-26",
         "accn": "0001-25-1", "form": "10-K", "derived": True, "restated": True},
        {"concept": "gross_margin", "value": 0.73, "end": "2025-01-26",
         "accn": "computed", "formula": "GrossProfit / Revenues", "derived": True},
    ])
    assert rows[0]["flags"] == ["derived", "restated"]
    assert rows[0]["computed"] is False
    assert rows[1]["computed"] is True
    assert rows[1]["formula"] == "GrossProfit / Revenues"
    assert [r["id"] for r in rows] == [1, 2]


def _collect(gen):
    async def drain():
        return [chunk async for chunk in gen]
    return asyncio.run(drain())


def test_stream_emits_rail_frames_then_the_rendered_report(monkeypatch):
    async def fake_run(question, ticker, on_stage=None):
        on_stage("tools", "start")
        on_stage("tools", "done", facts=1, ms=12)
        return {"trace_id": "T1", "report_md": "Margin was 73.9% [[fact:1]].",
                "facts": [{"concept": "gross_margin", "value": 0.739,
                           "end": "2025-01-26", "accn": "computed"}],
                "passages": [], "unsupported_claims": [],
                "latency_ms": {"total": 12}}

    monkeypatch.setattr(web.agent, "run", fake_run)
    body = "".join(_collect(web.stream("q", "NVDA")))

    # initial frame + one per stage event + the closing "Done" frame
    assert body.count("event: stage") == 4
    assert body.count("event: done") == 1
    assert body.count("event: close") == 1
    assert body.index("event: done") < body.index("event: close")
    assert "class=\"rail-step is-done\"" in body
    assert 'href="#fact-1"' in body
    # the finished report carries an out-of-band refresh for the history panel
    assert 'hx-swap-oob="innerHTML"' in body


def test_sse_frames_put_every_html_line_in_its_own_data_field():
    """Multi-line HTML in a single data: field silently truncates the fragment."""
    frame = web.sse("stage", "<div>\n  <p>hi</p>\n</div>")
    assert frame.startswith("event: stage\n")
    assert frame.endswith("\n\n")
    data = [ln for ln in frame.split("\n") if ln.startswith("data: ")]
    assert len(data) == 3
    assert data[1] == "data:   <p>hi</p>"


def test_stream_reports_a_crash_instead_of_hanging(monkeypatch):
    async def boom(question, ticker, on_stage=None):
        raise RuntimeError("data layer exploded")

    monkeypatch.setattr(web.agent, "run", boom)
    body = "".join(_collect(web.stream("q", "NVDA")))
    # Delivered as `done`, then closed, so the browser does not reconnect and
    # silently re-run a request that just crashed.
    assert "event: done" in body
    assert "event: close" in body
    assert "data layer exploded" in body
    assert "Request failed" in body


@pytest.mark.parametrize("refused", [True, False])
def test_report_html_covers_both_verdicts(refused):
    result = {"trace_id": "T", "latency_ms": {"total": 1},
              "facts": [], "passages": [], "unsupported_claims": []}
    if refused:
        result["refused"] = "No facts could be retrieved for this question."
        result["report_md"] = None
    else:
        result["report_md"] = "All quiet."
    html = web.report_html(result)
    assert ("Refused" in html) is refused


def test_rail_marks_a_repair_against_the_guard_step():
    """A repair is a second pass through the guard, not a fifth stage."""
    state = web.new_state()
    web.apply_stage(state, {"stage": "guard", "status": "start"})
    web.apply_stage(state, {"stage": "repair", "status": "start",
                            "claims": ["81.2%"]})
    assert state["guard"]["detail"] == "1 to repair"
    assert "repair" not in state

    web.apply_stage(state, {"stage": "guard", "status": "done",
                            "unsupported": 0})
    assert state["guard"]["status"] == "done"
    assert state["guard"]["detail"] == "all figures traced"


def test_rail_renders_a_class_per_status():
    state = web.new_state()
    web.apply_stage(state, {"stage": "tools", "status": "done",
                            "ms": 12, "facts": 3})
    web.apply_stage(state, {"stage": "retrieve", "status": "start"})
    html = web.render_rail(state, 1.25)
    assert "is-done" in html and "is-active" in html
    assert "12 ms · 3 facts" in html
    assert "1.2s" in html


def test_unknown_stages_do_not_break_the_rail():
    """`request` start/done are emitted but have no row of their own."""
    state = web.new_state()
    web.apply_stage(state, {"stage": "request", "status": "start"})
    web.apply_stage(state, {"stage": "nonsense", "status": "done"})
    assert all(s["status"] == "pending" for s in state.values())
