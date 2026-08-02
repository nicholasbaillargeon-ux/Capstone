"""Model client. Two providers behind one function signature.

  ollama   local, CPU, no network — the original target
  openai   any OpenAI-compatible endpoint: Fireworks, Together, Groq,
           vLLM, llama.cpp's server, LM Studio, OpenRouter, or OpenAI

The rest of the app only ever sees the Ollama-shaped message dict
(`{"role", "content", "tool_calls"}`), because that is what `agent.py` and
`toolcall.py` were written against. Normalising here rather than teaching
those modules about two formats keeps the seam in one file.

Note what changing provider costs: a hosted endpoint means the question and
the retrieved figures leave the machine, and "no network call at request
time" stops being true. The grounding guarantee is unaffected — the guard
runs locally against locally retrieved facts either way, so a hosted model
still cannot introduce a number that is not in a filing. Choose accordingly.
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


# ---- Ollama --------------------------------------------------------------

def _ollama_chat(messages: list[dict], tools: list[dict] | None) -> dict:
    body = {"model": config.CHAT_MODEL, "messages": messages,
            "stream": False, "options": {"temperature": 0}}
    if tools:
        body["tools"] = tools
    r = requests.post(f"{config.OLLAMA}/api/chat", json=body, timeout=600)
    r.raise_for_status()
    return r.json()["message"]


def _ollama_embed(texts: list[str]) -> list[list[float]]:
    r = requests.post(f"{config.OLLAMA}/api/embed",
                      json={"model": config.EMBED_MODEL, "input": texts},
                      timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]


# ---- OpenAI-compatible ---------------------------------------------------

def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        h["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    return h


def _openai_chat(messages: list[dict], tools: list[dict] | None,
                 effort: str | None = None) -> dict:
    # Ollama lets tool results carry a bare `name`; the OpenAI schema wants a
    # tool_call_id on every tool message and rejects the request without one.
    # The agent's retry loop also emits tool messages for calls it REJECTED,
    # which by definition have no id — so ids are synthesised in order.
    out_msgs, pending_ids = [], []
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
                        # OpenAI requires a JSON *string*; Ollama emits a dict.
                        "arguments": args if isinstance(args, str)
                        else json.dumps(args or {})}})
            out_msgs.append({"role": "assistant",
                             "content": m.get("content") or "",
                             "tool_calls": calls})
            continue
        out_msgs.append({"role": role, "content": m.get("content", "")})

    body = {"model": config.CHAT_MODEL, "messages": out_msgs,
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

    # Back to the Ollama shape the agent expects. `arguments` stays a string;
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


def _openai_embed(texts: list[str]) -> list[list[float]]:
    r = requests.post(f"{config.LLM_BASE_URL.rstrip('/')}/embeddings",
                      json={"model": config.EMBED_MODEL, "input": texts},
                      headers=_headers(), timeout=300)
    if r.status_code >= 400:
        raise LLMError(f"{r.status_code} from embeddings: {r.text[:300]}")
    data = r.json().get("data") or []
    # The API does not promise input order; `index` does.
    return [row["embedding"] for row in sorted(data, key=lambda x: x["index"])]


# ---- dispatch ------------------------------------------------------------

NO_MODEL = ("No model is configured (FD_LLM_PROVIDER=none). The dashboard "
            "does not use one; only the narrated answers at /ask do.")

NO_MODEL_NAME = (
    "A model endpoint is configured but no model name was chosen "
    "(FD_CHAT_MODEL is empty). Run `python -m filingdesk.models` to list what "
    "this endpoint serves, then set FD_CHAT_MODEL to one of them.")


def _guard_config() -> None:
    if not config.model_enabled():
        raise LLMError(NO_MODEL)
    if not config.CHAT_MODEL:
        raise LLMError(NO_MODEL_NAME)


def chat(messages: list[dict], tools: list[dict] | None = None,
         effort: str | None = None) -> dict:
    """`effort` overrides the reasoning budget for this one call.

    Planning and drafting are both chat calls but want different budgets —
    see config.REASONING_EFFORT. Ollama has no equivalent knob, so the
    argument is simply ignored on that path.
    """
    _guard_config()
    if config.LLM_PROVIDER == "openai":
        return _openai_chat(messages, tools, effort)
    return _ollama_chat(messages, tools)


def embed(texts: list[str]) -> list[list[float]]:
    _guard_config()
    if config.LLM_PROVIDER == "openai":
        return _openai_embed(texts)
    return _ollama_embed(texts)


def ping() -> tuple[bool, str]:
    """Is the configured model actually reachable? Used by the healthcheck.

    Cheap on purpose — a models listing, not a generation.
    """
    if not config.model_enabled():
        return False, "not configured"
    try:
        if config.LLM_PROVIDER == "openai":
            r = requests.get(f"{config.LLM_BASE_URL.rstrip('/')}/models",
                             headers=_headers(), timeout=4)
            if r.status_code == 401:
                return False, "rejected the API key"
            return r.status_code < 400, f"HTTP {r.status_code}"
        r = requests.get(f"{config.OLLAMA}/api/tags", timeout=2)
        return r.status_code == 200, f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return False, type(e).__name__


def describe() -> str:
    if not config.model_enabled():
        return "no model"
    if config.LLM_PROVIDER == "openai":
        host = config.LLM_BASE_URL.split("//")[-1].split("/")[0]
        return f"{config.CHAT_MODEL or 'no model chosen'} @ {host}"
    return f"{config.CHAT_MODEL} @ ollama"
