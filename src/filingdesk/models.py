"""List and check the models the configured provider actually offers.

    python -m filingdesk.models          # what can this key see?
    python -m filingdesk.models --check  # is FD_CHAT_MODEL real and reachable?

Exists because a wrong model ID is the most likely setup failure and the
least legible one: the provider answers 404 or "model not found" deep inside
a tool-calling loop, which the app can only report as "the model could not be
reached". Hosted catalogues also churn faster than any default committed to
this repo, so the authority has to be the account, not the source.
"""
from __future__ import annotations

import sys

import requests

from . import config, llm


def available() -> list[str]:
    if config.LLM_PROVIDER == "openai":
        r = requests.get(f"{config.LLM_BASE_URL.rstrip('/')}/models",
                         headers={"Authorization": f"Bearer {config.LLM_API_KEY}"}
                         if config.LLM_API_KEY else {},
                         timeout=15)
        if r.status_code == 401:
            raise SystemExit("The provider rejected the API key "
                             "(FD_LLM_API_KEY).")
        r.raise_for_status()
        return sorted(m.get("id", "") for m in (r.json().get("data") or []))

    r = requests.get(f"{config.OLLAMA}/api/tags", timeout=10)
    r.raise_for_status()
    return sorted(m.get("name", "") for m in (r.json().get("models") or []))


def check() -> int:
    ok, why = llm.ping()
    endpoint = (config.LLM_BASE_URL if config.LLM_PROVIDER == "openai"
                else config.OLLAMA)
    print(f"provider   : {config.LLM_PROVIDER}")
    print(f"endpoint   : {endpoint}")
    print(f"chat model : {config.CHAT_MODEL}")
    print(f"embed model: {config.EMBED_MODEL}")
    print(f"reachable  : {ok} ({why})")
    if not ok:
        return 1

    try:
        names = available()
    except Exception as e:  # noqa: BLE001 — a listing failure is not fatal
        print(f"(could not list models: {e})")
        names = []

    if names and config.CHAT_MODEL not in names:
        print(f"\nWARNING: {config.CHAT_MODEL!r} is not in this account's "
              "model list. Tool calls will fail with a model-not-found error.")
        near = [n for n in names
                if config.CHAT_MODEL.split("/")[-1].split("-")[0] in n]
        if near:
            print("Closest available:")
            for n in near[:8]:
                print(f"  {n}")
        return 1

    # A real round trip. Reachable and listed still does not prove the model
    # answers, and this app additionally requires tool calling.
    try:
        msg = llm.chat([{"role": "user", "content": "Reply with the word OK."}])
        print(f"test reply : {(msg.get('content') or '')[:60]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"\nFAILED on a real request: {e}")
        return 1
    print("\nOK — the model answers. Tool calling is exercised on a real "
          "question at /ask.")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check()
    try:
        names = available()
    except Exception as e:  # noqa: BLE001
        print(f"Could not list models: {e}", file=sys.stderr)
        return 1
    if not names:
        print("No models returned.")
        return 1
    print(f"{len(names)} model(s) visible to this configuration:\n")
    for n in names:
        mark = "  <- FD_CHAT_MODEL" if n == config.CHAT_MODEL else ""
        print(f"  {n}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
