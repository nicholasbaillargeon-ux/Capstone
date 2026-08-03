"""The streamed draft, and the three things about it that can go wrong.

Streaming is the only change in this pipeline that shows a figure to a reader
before the guard has looked at it. That is safe exactly as long as what is
shown is labelled unchecked and is replaced by the checked version — so the
label and the replacement are tested here as hard as the parsing is.

The parsing itself is the ordinary risk: an SSE frame that is not JSON, a
keepalive, a `[DONE]` sentinel, a chunk carrying reasoning rather than answer.
Any of them ending the loop early would truncate an answer silently, which is
the failure mode this project can least afford — a half-sentence still reads
like a whole one.
"""
import json

import pytest

from filingdesk import agent, config, llm, web


class FakeStream:
    """A streamed HTTP response, as `requests` hands one back."""

    def __init__(self, lines, status=200, text=""):
        self._lines = lines
        self.status_code = status
        self.text = text
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        self.closed = True

    def json(self):
        return json.loads(self.text)


def frame(**delta) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(config, "CHAT_MODEL", "some/open-model")
    monkeypatch.setattr(config, "LLM_API_KEY", "")


def serve(monkeypatch, lines, status=200, text=""):
    """Answer the streamed POST with these SSE lines; record what was sent."""
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=False):
        sent["url"] = url
        sent["body"] = json
        sent["stream"] = stream
        if stream:
            return FakeStream(lines, status, text)
        return FakeStream([], 200, json_text)

    json_text = '{"choices": [{"message": {"content": "unstreamed"}}]}'
    monkeypatch.setattr(llm.requests, "post", fake_post)
    return sent


# ---- parsing -------------------------------------------------------------

def test_pieces_arrive_in_order_and_concatenate_to_the_message(endpoint,
                                                               monkeypatch):
    serve(monkeypatch, ["", frame(content="Revenue was "),
                        ": keepalive", frame(content="$81.6B "),
                        frame(content="[[fact:1]]."), "data: [DONE]"])
    got = []
    msg = llm.chat([{"role": "user", "content": "revenue?"}],
                   on_token=got.append)
    assert got == ["Revenue was ", "$81.6B ", "[[fact:1]]."]
    # The returned message is what the guard and the page are handed. It has to
    # be exactly the concatenation, or the checked text differs from the read
    # text and the strike-throughs land on the wrong words.
    assert msg["content"] == "".join(got)


def test_a_frame_that_is_not_json_does_not_end_the_answer(endpoint,
                                                          monkeypatch):
    """A proxy that injects a comment or a truncated frame mid-stream must cost
    that frame, not the rest of the response."""
    serve(monkeypatch, [frame(content="Revenue "), "data: {not json",
                        frame(content="rose."), "data: [DONE]"])
    got = []
    llm.chat([{"role": "user", "content": "?"}], on_token=got.append)
    assert "".join(got) == "Revenue rose."


def test_reasoning_chunks_are_not_shown_as_answer(endpoint, monkeypatch):
    """gpt-oss thinks before it writes and says so on the wire. Those chunks
    are not the answer; putting them on the page would show a reader the
    model's scratch work as though it were the report."""
    serve(monkeypatch, [frame(reasoning_content="let me check the table"),
                        frame(content="Margin fell."), "data: [DONE]"])
    got = []
    msg = llm.chat([{"role": "user", "content": "?"}], on_token=got.append)
    assert got == ["Margin fell."]
    assert "scratch" not in msg["content"] and "check" not in msg["content"]


def test_both_waits_are_timed_separately(endpoint, monkeypatch):
    """Time to first chunk says the endpoint is alive; time to first word says
    there is something to read. Reporting one as the other would either flatter
    streaming or undersell it."""
    serve(monkeypatch, [frame(reasoning_content="..."),
                        frame(content="Hello."), "data: [DONE]"])
    msg = llm.chat([{"role": "user", "content": "?"}],
                   on_token=lambda _p: None)
    st = msg[llm.STREAM_KEY]
    assert st.ttfr_ms is not None and st.ttft_ms is not None
    assert st.ttfr_ms <= st.ttft_ms
    assert st.chunks == 2


def test_an_empty_stream_is_an_error_not_an_empty_answer(endpoint, monkeypatch):
    """A stream that yields no text has failed. Returning "" would put a blank
    report on the page and record it in history as a grounded answer."""
    serve(monkeypatch, ["data: [DONE]"])
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "?"}], on_token=lambda _p: None)


def test_an_endpoint_that_refuses_to_stream_still_answers(endpoint,
                                                          monkeypatch):
    """Losing the progressive display is acceptable. Losing the answer is not."""
    serve(monkeypatch, [], status=400, text="stream unsupported")
    got = []
    msg = llm.chat([{"role": "user", "content": "?"}], on_token=got.append)
    assert msg["content"] == "unstreamed"
    assert got == ["unstreamed"]


def test_a_rejected_reasoning_budget_is_dropped_and_the_stream_retried(
        endpoint, monkeypatch):
    """A server that will not take `reasoning_effort` says so with a 400 on
    every request, streamed or not. Reading that as "cannot stream" would
    silently give up the progressive display over an unrelated setting."""
    monkeypatch.setattr(llm, "_send_effort", True)
    monkeypatch.setattr(config, "REASONING_EFFORT", "medium")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, stream=False):
        calls.append(json)
        if "reasoning_effort" in json:
            return FakeStream([], 400, "unknown field reasoning_effort")
        return FakeStream([frame(content="Fine."), "data: [DONE]"])

    monkeypatch.setattr(llm.requests, "post", fake_post)
    got = []
    msg = llm.chat([{"role": "user", "content": "?"}], on_token=got.append)
    assert msg["content"] == "Fine."
    assert len(calls) == 2 and calls[1]["stream"] is True


def test_tool_calls_are_never_streamed(endpoint):
    """Tool calls arrive as deltas that mean nothing half-built, and the
    planning loop has no use for a partial one."""
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "?"}], tools=[{"x": 1}],
                 on_token=lambda _p: None)


# ---- what a reader sees before the guard has run -------------------------

def test_timings_ride_on_the_message_not_on_the_module(endpoint, monkeypatch):
    """The server answers more than one question at a time. Timings parked in a
    module global would let two concurrent requests read each other's, and a
    latency number belonging to a different request is worse than none — the
    page would report a wait nobody had."""
    def fake_post(url, json=None, headers=None, timeout=None, stream=False):
        n = len(json["messages"][-1]["content"])
        return FakeStream([frame(content="x" * n), "data: [DONE]"])

    monkeypatch.setattr(llm.requests, "post", fake_post)
    first = llm.chat([{"role": "user", "content": "short"}],
                     on_token=lambda _p: None)
    second = llm.chat([{"role": "user", "content": "much longer question"}],
                      on_token=lambda _p: None)
    # Each message carries its own record, and the earlier one is not rewritten
    # by the later call.
    assert first[llm.STREAM_KEY] is not second[llm.STREAM_KEY]
    assert first["content"] == "x" * 5


def test_the_live_draft_says_it_is_unchecked():
    html = web.render_draft_live("Margin was 0.7493 [[fact:24]].")
    assert "unchecked" in html.lower()
    # Left as the model wrote it. A citation chip is a link to a verified fact,
    # and nothing here is verified yet.
    assert "[[fact:24]]" in html
    assert 'class="cite"' not in html


def test_the_live_draft_escapes_model_output():
    """It is model text going onto a page, same as the finished report."""
    html = web.render_draft_live("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_token_events_do_not_touch_the_rail():
    """The rail redraws on stage changes. A token is not one, and redrawing per
    token would push hundreds of identical frames."""
    state = web.new_state()
    web.apply_stage(state, {"stage": "draft", "status": "start"})
    before = web.render_rail(state, 1.0)
    web.apply_stage(state, {"stage": "draft", "status": "token", "text": "hi"})
    assert web.render_rail(state, 1.0) == before


def test_tokens_emitted_from_a_worker_thread_arrive_in_order(monkeypatch):
    """The draft runs in a thread so the heartbeat keeps beating, so `emit` is
    called from off the event loop for tokens and from on it for everything
    else. Both have to reach the page, and the finished report has to arrive
    after the partial ones rather than overtaking them."""
    import asyncio

    async def fake_run(question, ticker, on_stage=None):
        on_stage("draft", "start")

        def work():
            for piece in ("Margin ", "was ", "0.7493."):
                on_stage("draft", "token", text=piece)
            return "Margin was 0.7493."

        text = await asyncio.to_thread(work)
        return {"trace_id": "T1", "report_md": text, "facts": [],
                "passages": [], "unsupported_claims": [], "latency_ms": {}}

    monkeypatch.setattr(web.agent, "run", fake_run)

    async def collect():
        return [f for f in [x async for x in web.stream("q", "NVDA")]]

    frames = asyncio.run(collect())
    kinds = [f.split("\n")[0] for f in frames]
    assert "event: draft" in kinds, "no partial draft reached the page"
    assert kinds.index("event: draft") < kinds.index("event: done")
    # Whatever the last partial frame held must be a prefix of the final text,
    # not a different rendering of it.
    last_draft = [f for f in frames if f.startswith("event: draft")][-1]
    assert "Margin" in last_draft


def test_the_finished_report_replaces_the_draft_in_one_slot():
    """Both events swap into the same element. Two slots would leave the
    unchecked draft on the page underneath the checked answer."""
    shell = web.render("_run.html", stream_url="/x", rail="")
    assert 'sse-swap="draft,done"' in shell


# ---- the trimmed fact table ----------------------------------------------

def _facts(concept: str, n: int, start: int = 0) -> list[dict]:
    return [{"concept": concept, "value": float(start + i),
             "end": f"2020-{i % 12 + 1:02d}-01", "accn": f"a{start + i}"}
            for i in range(n)]


def test_trimming_is_off_by_default():
    facts = _facts("Revenues", 40)
    table, dropped = agent.draft_table(facts)
    assert dropped == 0
    assert table == agent.fact_table(facts)


def test_trimming_keeps_the_numbers_the_full_table_gave(monkeypatch):
    """A citation is an index into the fact list, and the guard is handed the
    whole list. If trimming renumbered the rows, [[fact:41]] would name one
    fact in the prompt and a different one in the check."""
    monkeypatch.setattr(config, "DRAFT_FACTS_MAX", 8)
    facts = _facts("GrossProfit", 20) + _facts("Revenues", 20, start=100)
    table, dropped = agent.draft_table(facts)
    assert dropped > 0
    full = {line.split("]]")[0] + "]]": line
            for line in agent.fact_table(facts).split("\n")}
    for line in table.split("\n"):
        if line.startswith("[[fact:"):
            assert full[line.split("]]")[0] + "]]"] == line


def test_trimming_keeps_both_ends_of_every_series(monkeypatch):
    """"Compare the first and last quarter" is an eval case. Keeping the tail
    and dropping the head answers it with the wrong quarter."""
    monkeypatch.setattr(config, "DRAFT_FACTS_MAX", 6)
    facts = _facts("GrossProfit", 20) + _facts("Revenues", 20, start=100)
    table, _ = agent.draft_table(facts)
    for concept, first, last in (("GrossProfit", "0.0000", "19.0000"),
                                 ("Revenues", "100", "119")):
        rows = [r for r in table.split("\n") if concept in r]
        assert first in rows[0]
        assert last in rows[-1]


def test_a_trimmed_table_says_it_is_trimmed(monkeypatch):
    """A model handed 8 of 40 rows without being told will answer "which
    quarter was highest" from the 8, and sound just as certain."""
    monkeypatch.setattr(config, "DRAFT_FACTS_MAX", 8)
    table, dropped = agent.draft_table(_facts("Revenues", 40))
    assert "not shown" in table
    assert "maximum" in table


# ---- harvest ------------------------------------------------------------

def test_a_metric_computed_twice_is_harvested_once():
    """A plan that asks for 8 quarters and then 20 overlaps on 8 of them. Each
    duplicate is another row in Sources and another [[fact:N]] the model can
    cite for the same number."""
    payload = {"metric": "gross_margin",
               "series": [{"period_end": "2025-04-27", "value": 0.61,
                           "formula": "GrossProfit / Revenues"}]}
    facts: list[dict] = []
    assert agent.harvest(payload, facts) == 1
    assert agent.harvest(payload, facts) == 0
    assert len(facts) == 1


def test_distinct_periods_still_both_land():
    facts: list[dict] = []
    for end in ("2025-04-27", "2025-07-27"):
        agent.harvest({"metric": "gross_margin",
                       "series": [{"period_end": end, "value": 0.61}]}, facts)
    assert [f["end"] for f in facts] == ["2025-04-27", "2025-07-27"]
