"""Tests for the model endpoint seam.

The agent and the tool-call validator are written against a flatter message
shape than the OpenAI schema the endpoint speaks. The translation happens in
llm.py, and it is the kind of code that fails silently — a dropped
tool_call_id produces a 400 from the API, and a dict where a JSON string was
expected produces a tool call that never runs.
"""
import json

import pytest

from filingdesk import config, llm


class FakeResponse:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("should not be reached in these tests")


@pytest.fixture
def openai_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "CHAT_MODEL", "some/open-model")
    monkeypatch.setattr(config, "EMBED_MODEL", "some/embed-model")


def capture(monkeypatch, payload, status=200):
    """Record the request body the client would send."""
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["url"] = url
        sent["body"] = json
        sent["headers"] = headers
        return FakeResponse(payload, status)

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return sent


REPLY_WITH_TOOL = {"choices": [{"message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_abc", "type": "function", "function": {
        "name": "fd_get_concept",
        "arguments": '{"ticker": "NVDA", "concept": "Revenues"}'}}]}}]}

PLAIN_REPLY = {"choices": [{"message": {
    "role": "assistant", "content": "Revenue was $81.6B [[fact:1]]."}}]}


def test_routes_to_the_configured_base_url(openai_mode, monkeypatch):
    sent = capture(monkeypatch, PLAIN_REPLY)
    llm.chat([{"role": "user", "content": "hi"}])
    assert sent["url"] == "https://example.test/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"


def test_response_is_normalised_to_the_agents_shape(openai_mode, monkeypatch):
    capture(monkeypatch, REPLY_WITH_TOOL)
    msg = llm.chat([{"role": "user", "content": "revenue?"}], tools=[{"x": 1}])
    assert msg["role"] == "assistant"
    call = msg["tool_calls"][0]
    # The agent reads call["function"]["name"]; no OpenAI nesting leaks out.
    assert call["function"]["name"] == "fd_get_concept"
    assert json.loads(call["function"]["arguments"])["ticker"] == "NVDA"


def test_tool_arguments_are_sent_as_a_json_string(openai_mode, monkeypatch):
    """The agent emits arguments as a dict; the schema requires a string."""
    sent = capture(monkeypatch, PLAIN_REPLY)
    llm.chat([
        {"role": "user", "content": "revenue?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "fd_get_concept",
                                      "arguments": {"ticker": "NVDA"}}}]},
        {"role": "tool", "name": "fd_get_concept", "content": "{}"},
    ])
    call = sent["body"]["messages"][1]["tool_calls"][0]
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"ticker": "NVDA"}


def test_every_tool_message_gets_a_tool_call_id(openai_mode, monkeypatch):
    """The OpenAI schema rejects a tool message without one, and the shape
    the agent hands over has no id at all."""
    sent = capture(monkeypatch, PLAIN_REPLY)
    llm.chat([
        {"role": "user", "content": "revenue?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "f", "arguments": {}}}]},
        {"role": "tool", "name": "f", "content": "{}"},
    ])
    msgs = sent["body"]["messages"]
    assert msgs[1]["tool_calls"][0]["id"] == msgs[2]["tool_call_id"]


def test_a_rejected_tool_call_still_produces_a_valid_tool_message(
        openai_mode, monkeypatch):
    """The retry loop appends a tool message for calls the validator REJECTED,
    which never had an id. It must still be schema-valid."""
    sent = capture(monkeypatch, PLAIN_REPLY)
    llm.chat([
        {"role": "user", "content": "revenue?"},
        {"role": "tool", "name": "unknown", "content": '{"error": "no"}'},
    ])
    tool_msg = sent["body"]["messages"][1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"]          # present and non-empty


def test_http_error_raises_llm_error_not_a_bare_keyerror(
        openai_mode, monkeypatch):
    capture(monkeypatch, {"error": {"message": "bad key"}}, status=401)
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "hi"}])
    assert "401" in str(e.value)


def test_a_response_with_no_choices_is_an_llm_error(openai_mode, monkeypatch):
    capture(monkeypatch, {"choices": []})
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}])


def test_embeddings_are_returned_in_input_order(openai_mode, monkeypatch):
    """The API does not promise ordering; `index` does. Getting this wrong
    pairs every vault chunk with the wrong vector."""
    capture(monkeypatch, {"data": [
        {"index": 2, "embedding": [3.0]},
        {"index": 0, "embedding": [1.0]},
        {"index": 1, "embedding": [2.0]},
    ]})
    assert llm.embed(["a", "b", "c"]) == [[1.0], [2.0], [3.0]]


def test_the_proxy_is_the_default_endpoint():
    """There is one endpoint and no provider switch. A second path existed for
    local inference and is gone; what remains must default to something that
    answers, not to nothing."""
    assert config.LLM_BASE_URL == config.LITELLM_URL
    assert config.CHAT_MODEL          # a verified name for THAT endpoint
    assert config.model_enabled() is True


def test_an_endpoint_with_no_model_name_says_so(monkeypatch):
    """Point the app at another proxy and its model names are its own, so
    there is no safe default. Guessing one produces a confident 404 from
    inside the tool loop; this names the actual problem instead."""
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://other.test/v1")
    monkeypatch.setattr(config, "CHAT_MODEL", "")
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "hi"}])
    assert "FD_CHAT_MODEL" in str(e.value)
    assert config.model_enabled() is False


def test_describe_names_the_host_being_used(openai_mode):
    assert "example.test" in llm.describe()
    assert "some/open-model" in llm.describe()


# ---- dashboard-only mode -------------------------------------------------

@pytest.fixture
def no_model(monkeypatch):
    """Dashboard-only: an endpoint may be set, but no model is chosen."""
    monkeypatch.setattr(config, "CHAT_MODEL", "")


def test_no_model_is_a_configuration_not_a_failure(no_model):
    assert config.model_enabled() is False
    assert llm.describe() == "no model"
    ok, why = llm.ping()
    assert (ok, why) == (False, "not configured")


def test_chat_refuses_clearly_with_no_model(no_model):
    with pytest.raises(llm.LLMError) as e:
        llm.chat([{"role": "user", "content": "hi"}])
    assert "FD_CHAT_MODEL" in str(e.value)


def test_no_model_means_no_network_call(no_model, monkeypatch):
    """The point of the check is to short-circuit before any request."""
    def boom(*a, **k):
        raise AssertionError("should not reach the network")

    monkeypatch.setattr(llm.requests, "post", boom)
    monkeypatch.setattr(llm.requests, "get", boom)
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(llm.LLMError):
        llm.embed(["x"])

    assert llm.ping() == (False, "not configured")


def test_the_disabled_flag_cannot_drift_from_the_settings(monkeypatch):
    """model_enabled() is derived, not stored. A cached copy could say a
    model is configured while the app refuses to use one."""
    monkeypatch.setattr(config, "CHAT_MODEL", "")
    assert config.model_enabled() is False
    monkeypatch.setattr(config, "CHAT_MODEL", "gpt-oss-120b")
    assert config.model_enabled() is True
    monkeypatch.setattr(config, "LLM_BASE_URL", "")
    assert config.model_enabled() is False


def test_agent_refuses_without_starting_the_mcp_subprocess(no_model, monkeypatch):
    """With no model there is nothing to narrate, so spawning the data layer
    first would report a configuration choice as a runtime failure."""
    import asyncio

    from filingdesk import agent

    def boom(*a, **k):
        raise AssertionError("MCP subprocess should not be started")

    monkeypatch.setattr(agent, "stdio_client", boom)
    res = asyncio.run(agent.run("How has gross margin moved?", "NVDA"))
    assert res["refusal_kind"] == "model_unavailable"
    assert res["report_md"] is None


# ---- switching embedding model invalidates the vault index ---------------

def test_a_stale_vault_index_degrades_instead_of_crashing(tmp_path, monkeypatch):
    """Changing embedding provider changes vector width, and the index on
    disk still holds the old one. Unhandled, this is a numpy matmul error in
    a worker thread. Notes are framing only, so the answer should continue
    without them rather than fail."""
    from filingdesk import vault

    monkeypatch.setattr(config, "VAULT_DB", tmp_path / "v.db")
    monkeypatch.setattr(config, "EMBED_MODEL", "new/model")

    # Index with a 4-dim model...
    vault.index(str(_notes(tmp_path)), embed_fn=lambda ts: [[1.0] * 4 for _ in ts])
    # ...then query with an 8-dim one.
    got = vault.retrieve("anything", k=3, embed_fn=lambda ts: [[1.0] * 8])
    assert got == []


def test_vault_retrieval_works_when_dimensions_agree(tmp_path, monkeypatch):
    from filingdesk import vault

    monkeypatch.setattr(config, "VAULT_DB", tmp_path / "v2.db")
    vault.index(str(_notes(tmp_path)), embed_fn=lambda ts: [[1.0] * 4 for _ in ts])
    got = vault.retrieve("anything", k=2, embed_fn=lambda ts: [[1.0] * 4])
    assert got and "score" in got[0]


def _notes(tmp_path):
    d = tmp_path / "notes"
    d.mkdir(exist_ok=True)
    (d / "a.md").write_text("Margin moves are mostly mix.\n\nTreat one quarter "
                            "as noise.\n", encoding="utf-8")
    return d


# ---- the stand-ins must be callable the way the real things are ----------

def test_the_stubs_match_the_signatures_they_replace():
    """stub.install() rebinds llm.chat and llm.embed. When the planning loop
    grew `effort=`, the fake did not, and every synthetic-mode question died
    with TypeError inside the MCP session — surfaced to the user as "the
    filings database could not be reached", which is a confident wrong
    diagnosis of a mode that never touches a database.

    Comparing signatures rather than calling: the point is that the fake can
    be called however the real one can, not what it returns."""
    import inspect

    from filingdesk import stub

    for real, fake in ((llm.chat, stub.fake_chat), (llm.embed, stub.fake_embed)):
        want = inspect.signature(real).parameters
        got = inspect.signature(fake).parameters
        assert list(got) == list(want), f"{fake.__name__} vs {real.__name__}"
        for name, p in want.items():
            assert got[name].default == p.default, f"{fake.__name__}({name})"
