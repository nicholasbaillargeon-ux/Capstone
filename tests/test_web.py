"""Tests for the presentation layer.

The report is model output rendered as HTML, which makes escaping a security
property rather than a formatting detail. The striking tests exist because the
obvious implementation — replace claim text everywhere — corrupts the citation
anchors when a claim happens to look like a fact id.
"""
import asyncio
import re

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


def _landing(**over) -> str:
    ctx = {"stub": False, "base": "", "app_url": "/app",
           "featured": ["NVDA", "AAPL"],
           "ready": True, "facts": 423357, "universe": 10432,
           "model_enabled": True, "stack": ["Python"],
           "stats": [{"label": "XBRL FACTS", "value": "423,357",
                      "note": "indexed and queryable"}]}
    return web.render("landing.html", **{**ctx, **over})


def test_landing_links_into_the_running_app():
    """Its CTAs are relative. The page shipped with a LAN address baked in,
    which only works from the machine it was captured on."""
    html = _landing()
    assert 'href="/app?ticker=NVDA"' in html
    assert 'href="/app?ticker=AAPL"' in html
    assert 'href="/ask"' in html
    assert "192.168" not in html


def test_landing_counts_come_from_the_instance():
    html = _landing(facts=12, universe=7)
    assert "12 facts" in html and "7 tickers" in html
    assert "423,357 facts" not in html


def test_landing_reports_an_empty_cache_rather_than_ready():
    assert "no data yet" in _landing(ready=False, facts=0)
    assert "no data yet" not in _landing()


def test_landing_leads_with_the_dashboard_when_there_is_no_model():
    """"Ask a question" as the primary action would advertise the one surface
    that needs a model the instance does not have."""
    html = _landing(model_enabled=False)
    assert 'class="btn btn-lg" href="/app?ticker=NVDA"' in html
    assert 'href="/ask"' not in html


# ---- base path -----------------------------------------------------------
# The app is mounted under a prefix in the combined showcase. The proxy strips
# it before the request lands, so routing is unaffected — what breaks without
# these is every URL the pages *emit*.

def _reload_config(monkeypatch, value):
    import importlib

    from filingdesk import config
    monkeypatch.setenv("FD_BASE_PATH", value)
    importlib.reload(config)
    return config


@pytest.fixture
def restore_config():
    import importlib
    import os

    from filingdesk import config
    yield
    os.environ.pop("FD_BASE_PATH", None)
    importlib.reload(config)


@pytest.mark.parametrize("given,expected", [
    ("", ""), ("/", ""), ("filing-desk", "/filing-desk"),
    ("/filing-desk", "/filing-desk"), ("/filing-desk/", "/filing-desk"),
    ("  /filing-desk/  ", "/filing-desk"),
])
def test_base_path_normalises_to_one_leading_slash(monkeypatch, restore_config,
                                                   given, expected):
    """Templates concatenate: {{ base }}/static/app.css. A trailing slash or a
    missing leading one produces a URL that 404s in a way nothing else does."""
    assert _reload_config(monkeypatch, given).BASE_PATH == expected


@pytest.fixture
def mounted(monkeypatch):
    from filingdesk import config
    monkeypatch.setattr(config, "BASE_PATH", "/filing-desk")


def test_dashboard_urls_carry_the_prefix(mounted):
    html = web.render("dashboard.html", base="/filing-desk", stub=False,
                      ticker="NVDA", model_enabled=True,
                      featured=["NVDA"], universe=10432)
    assert 'href="/filing-desk/static/app.css?v=' in html
    assert 'src="/filing-desk/static/dashboard.js?v=' in html
    assert 'href="/filing-desk/ask"' in html
    # the wordmark goes back to the landing page, which owns the mount point
    assert 'href="/filing-desk/"' in html
    # dashboard.js reads this and prefixes every fetch with it.
    assert 'data-base="/filing-desk"' in html


def test_ask_page_urls_carry_the_prefix():
    html = web.render("index.html", base="/filing-desk", stub=False,
                      model_enabled=True, examples=[], question="",
                      ticker="NVDA", result=None, report_fragment="")
    assert 'hx-get="/filing-desk/ui/run"' in html
    assert 'hx-get="/filing-desk/api/history/html"' in html
    assert 'action="/filing-desk/report"' in html


def test_the_way_out_appears_only_when_something_owns_the_root():
    """Standalone, Filing Desk IS the root — a "← Showcase" pill there would
    link the visitor to the page they are already on."""
    assert "← Showcase" not in _landing()
    mounted = _landing(base="/filing-desk", app_url="/filing-desk/app",
                       showcase_url="/")
    assert 'id="__showcase_link" href="/"' in mounted
    # Styled inline, like both siblings: a stale stylesheet cannot unstyle it.
    assert "position:fixed" in mounted
    assert "← Showcase" in mounted


def test_the_dashboard_offers_the_way_back_to_the_landing_page():
    html = web.render("dashboard.html", base="/filing-desk", stub=False,
                      ticker="NVDA", model_enabled=True, featured=["NVDA"],
                      universe=1)
    assert '<a class="ghost" href="/filing-desk/">← Back to the landing page</a>' \
        in html


def test_landing_urls_carry_the_prefix(mounted):
    html = _landing(base="/filing-desk", app_url="/filing-desk/app")
    assert 'href="/filing-desk/static/landing.css?v=' in html
    assert 'href="/filing-desk/app?ticker=NVDA"' in html
    assert 'href="/filing-desk/ask"' in html


def test_static_urls_are_keyed_to_their_contents():
    """A stylesheet and the markup needing it ship together but cache apart —
    the CDN in front of the deployed page served one against the other. The
    URL changes when the bytes do, so there is nothing stale to serve."""
    from filingdesk import web as w
    first = w.asset("landing.css")
    assert re.search(r"/static/landing\.css\?v=[0-9a-f]{12}$", first)
    assert w.asset("landing.css") == first          # stable within a process
    assert w.asset("app.css") != first              # and per file


def test_a_missing_asset_still_renders_the_page():
    """A broken asset is a 404 either way. Refusing to render over it would
    turn one missing file into a blank site."""
    from filingdesk import web as w
    assert w.asset("does-not-exist.css").endswith("?v=0")


def test_unmounted_urls_stay_at_the_root():
    """The default is no prefix, and it must not leave a stray slash behind."""
    html = web.render("dashboard.html", base="", stub=False, ticker="NVDA",
                      model_enabled=True, featured=["NVDA"], universe=1)
    assert 'href="/static/app.css?v=' in html
    assert "//static" not in html


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
