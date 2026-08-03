"""Model client. One OpenAI-compatible endpoint, the LiteLLM proxy.

There was a second path — Ollama on the app host — and it is gone. It bought
local inference at 7B when the proxy serves 120B on hardware built for it, and
it cost every call site a shape to reason about: Ollama takes tool results with
a bare `name` and emits tool arguments as a dict, the OpenAI schema demands a
tool_call_id on each and arguments as a JSON string.

The rest of the app still sees the flatter message dict
(`{"role", "content", "tool_calls"}`) that `agent.py` and `toolcall.py` were
written against, so the translation stayed — it is just one-way now, and lives
only in `_to_openai` and the tail of `chat`.

What this deployment means, stated plainly: the question and the retrieved
figures leave this machine for the proxy. The grounding guarantee is
unaffected — the guard runs locally against locally retrieved facts, so no
model, wherever it runs, can introduce a number that is not in a filing.
"""
from __future__ import annotations

import json
import time
from typing import NamedTuple

import requests

from . import config


class LLMError(RuntimeError):
    """The model could not be reached or replied with something unusable."""


# Set once at import from config, then latched off if the endpoint turns out
# to reject the parameter. Process-wide rather than per-call: the answer is a
# property of the endpoint, and re-learning it on every question would cost a
# doubled round trip each time.
_send_effort = bool(config.REASONING_EFFORT)


def _disable_effort(detail: str) -> None:
    global _send_effort
    _send_effort = False
    print(f"[llm] endpoint rejected reasoning_effort="
          f"{config.REASONING_EFFORT!r}, continuing without it ({detail})")


# ---- the endpoint --------------------------------------------------------

def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        h["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    return h


def _chat(messages: list[dict], tools: list[dict] | None,
          effort: str | None = None, model: str | None = None,
          on_token=None) -> dict:
    # The agent speaks the flatter shape: a tool result carries a bare `name`.
    # The OpenAI schema wants a tool_call_id on every tool message and rejects
    # the request without one. The agent's retry loop also emits tool messages
    # for calls it REJECTED, which by definition have no id — so ids are
    # synthesised in order.
    out_msgs: list[dict] = []
    pending_ids: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            call_id = m.get("tool_call_id") or (
                pending_ids.pop(0) if pending_ids else "call_0")
            out_msgs.append({"role": "tool", "tool_call_id": call_id,
                             "content": str(m.get("content", ""))})
            continue
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for i, c in enumerate(m["tool_calls"]):
                fn = c.get("function", {})
                cid = c.get("id") or f"call_{i}"
                pending_ids.append(cid)
                args = fn.get("arguments")
                calls.append({
                    "id": cid, "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        # OpenAI requires a JSON *string*; the agent emits a dict.
                        "arguments": args if isinstance(args, str)
                        else json.dumps(args or {})}})
            out_msgs.append({"role": "assistant",
                             "content": m.get("content") or "",
                             "tool_calls": calls})
            continue
        out_msgs.append({"role": role, "content": m.get("content", "")})

    body = {"model": model or config.CHAT_MODEL, "messages": out_msgs,
            "temperature": 0, "stream": False}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    effort = config.REASONING_EFFORT if effort is None else effort
    if _send_effort and effort:
        body["reasoning_effort"] = effort

    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    if on_token is not None:
        return _stream(url, body, on_token)
    r = requests.post(url, json=body, headers=_headers(), timeout=600)
    if r.status_code == 400 and _send_effort:
        # Not every OpenAI-compatible server tolerates an unknown body field,
        # and the ones that reject it do so on every request — which would
        # turn a latency setting into a total outage against, say, a strict
        # vLLM build. Drop it for the rest of the process and retry once. A
        # 400 for any other reason simply 400s again below, with its own
        # message intact.
        _disable_effort(r.text[:200])
        body.pop("reasoning_effort", None)
        r = requests.post(url, json=body, headers=_headers(), timeout=600)
    if r.status_code >= 400:
        raise LLMError(f"{r.status_code} from {config.LLM_BASE_URL}: "
                       f"{r.text[:300]}")
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"No choices in response: {str(data)[:300]}")
    msg = choices[0].get("message") or {}

    # Back to the shape the agent expects. `arguments` stays a string;
    # toolcall.validate already parses either form.
    out = {"role": "assistant", "content": msg.get("content") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {"id": c.get("id"),
             "function": {"name": (c.get("function") or {}).get("name", ""),
                          "arguments": (c.get("function") or {}).get(
                              "arguments", "{}")}}
            for c in msg["tool_calls"]]
    return out


# ---- streaming -----------------------------------------------------------
# Only the draft is streamed, and only because it is the stage a person is
# waiting on: it runs last, it is the longest single call in the pipeline, and
# until it returns the page has nothing to show. Streaming does not make the
# answer arrive sooner — it makes the wait legible, which is a different and
# more honest claim.
#
# The planning loop is deliberately NOT streamed. Its output is tool calls,
# which are assembled from deltas across chunks and are of no interest to a
# reader half-built; the complexity would buy nothing a spinner does not.
#
# A reasoning model spends most of a draft call thinking before it emits a
# single visible character. Both moments are timed — the first chunk of any
# kind (`ttfr`, the endpoint is alive and reasoning) and the first chunk of
# answer text (`ttft`, there is something to read) — because reporting only
# the second would credit streaming with less than it does, and reporting only
# the first would credit it with more.

class Stream(NamedTuple):
    """What one streamed call measured.

    Handed back on the message rather than parked in a module global, because
    the server answers more than one question at a time: a global would let two
    concurrent requests read each other's timings, and the wrong number here is
    worse than none — it is a latency claim about a request that never made it.
    """

    ttfr_ms: int | None
    ttft_ms: int | None
    chunks: int


# The key the timings ride on. Underscored because it is not part of the
# message shape the agent and toolcall.validate are written against; anything
# reading a message for its content or its tool calls should never see it.
STREAM_KEY = "_stream"


def _stream(url: str, body: dict, on_token) -> dict:
    """POST with `stream: true`, feeding text to `on_token` as it lands.

    Falls back to a single non-streamed call if the endpoint refuses to
    stream: a proxy that cannot is a reason to lose the progressive display,
    never a reason to lose the answer.
    """
    body = dict(body, stream=True)
    t0 = time.monotonic()
    try:
        r = requests.post(url, json=body, headers=_headers(),
                          timeout=600, stream=True)
    except requests.RequestException as exc:
        raise LLMError(f"stream to {config.LLM_BASE_URL} failed: {exc}") from exc

    if r.status_code == 400 and _send_effort and "reasoning_effort" in body:
        # Same latch the unstreamed path has, and it has to be here too: a
        # strict server rejects the unknown field whichever way the request was
        # made, and without this the retry below would send it again, 400
        # again, and report "cannot stream" for a request that streams fine.
        _disable_effort(r.text[:200])
        r.close()
        body.pop("reasoning_effort", None)
        r = requests.post(url, json=body, headers=_headers(),
                          timeout=600, stream=True)

    if r.status_code >= 400:
        detail = r.text[:200]
        r.close()
        # An endpoint that rejects `stream` rejects it every time; the caller
        # still wants an answer, so take the unstreamed one and say why once.
        print(f"[llm] endpoint refused a streamed request ({r.status_code}: "
              f"{detail}); falling back to a single call")
        body.pop("stream", None)
        r2 = requests.post(url, json={**body, "stream": False},
                           headers=_headers(), timeout=600)
        if r2.status_code >= 400:
            raise LLMError(f"{r2.status_code} from {config.LLM_BASE_URL}: "
                           f"{r2.text[:300]}")
        msg = ((r2.json().get("choices") or [{}])[0].get("message") or {})
        text = msg.get("content") or ""
        if text:
            on_token(text)
        return {"role": "assistant", "content": text,
                STREAM_KEY: Stream(None, None, 0)}

    parts: list[str] = []
    ttfr = ttft = None
    chunks = 0
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        chunks += 1
        if ttfr is None:
            ttfr = int((time.monotonic() - t0) * 1000)
        delta = ((data.get("choices") or [{}])[0].get("delta") or {})
        piece = delta.get("content")
        if not piece:
            continue
        if ttft is None:
            ttft = int((time.monotonic() - t0) * 1000)
        parts.append(piece)
        on_token(piece)

    if not parts:
        raise LLMError("stream produced no content")
    return {"role": "assistant", "content": "".join(parts),
            STREAM_KEY: Stream(ttfr, ttft, chunks)}


def _embed(texts: list[str]) -> list[list[float]]:
    r = requests.post(f"{config.LLM_BASE_URL.rstrip('/')}/embeddings",
                      json={"model": config.EMBED_MODEL, "input": texts},
                      headers=_headers(), timeout=300)
    if r.status_code >= 400:
        raise LLMError(f"{r.status_code} from embeddings: {r.text[:300]}")
    data = r.json().get("data") or []
    # The API does not promise input order; `index` does.
    return [row["embedding"] for row in sorted(data, key=lambda x: x["index"])]


# ---- dispatch ------------------------------------------------------------

NO_MODEL = (
    "No model is configured: FD_CHAT_MODEL is empty, or FD_LLM_BASE_URL is. "
    "The dashboard does not use one; only the narrated answers at /ask do. "
    "Run `python -m filingdesk.models` to list what the endpoint serves.")

NO_EMBED_MODEL = (
    "No embedding model is configured (FD_EMBED_MODEL is empty), so there is "
    "nothing to index the vault with. The endpoint this app defaults to serves "
    "chat models only.")


def _guard_config() -> None:
    if not config.model_enabled():
        raise LLMError(NO_MODEL)


def chat(messages: list[dict], tools: list[dict] | None = None,
         effort: str | None = None, model: str | None = None,
         on_token=None) -> dict:
    """`effort` and `model` override the budget and the model for one call.

    Planning and drafting are both chat calls but want different settings —
    see config.REASONING_EFFORT and config.PLAN_CHAT_MODEL.

    Passing `on_token` streams the reply, calling it with each piece of text as
    it arrives. Not for tool-calling requests: tool calls arrive as deltas that
    have to be reassembled before they mean anything, and a half-built one is
    of no use to anybody watching.
    """
    _guard_config()
    if on_token is not None and tools:
        raise LLMError("streaming is not supported for tool-calling requests")
    return _chat(messages, tools, effort, model, on_token)


def embed(texts: list[str]) -> list[list[float]]:
    _guard_config()
    if not config.EMBED_MODEL:
        raise LLMError(NO_EMBED_MODEL)
    return _embed(texts)


def ping() -> tuple[bool, str]:
    """Is the configured model actually reachable? Used by the healthcheck.

    Cheap on purpose — a models listing, not a generation.
    """
    if not config.model_enabled():
        return False, "not configured"
    try:
        r = requests.get(f"{config.LLM_BASE_URL.rstrip('/')}/models",
                         headers=_headers(), timeout=4)
        if r.status_code == 401:
            return False, "rejected the API key"
        return r.status_code < 400, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, type(e).__name__


def describe() -> str:
    if not config.model_enabled():
        return "no model"
    host = config.LLM_BASE_URL.split("//")[-1].split("/")[0]
    return f"{config.CHAT_MODEL} @ {host}"
