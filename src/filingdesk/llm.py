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
          effort: str | None = None, model: str | None = None) -> dict:
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
         effort: str | None = None, model: str | None = None) -> dict:
    """`effort` and `model` override the budget and the model for one call.

    Planning and drafting are both chat calls but want different settings —
    see config.REASONING_EFFORT and config.PLAN_CHAT_MODEL.
    """
    _guard_config()
    return _chat(messages, tools, effort, model)


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
