"""Measure the serving side, so a serving-side recommendation is not a guess.

    python -m evals.probe_llm

Two of the things worth doing to make this app faster are not in this app. They
are settings on whatever serves the model behind the LiteLLM proxy, and before
asking anyone to change a flag on another machine it is worth knowing what that
flag would be worth here. This measures it from the client, which is the only
side available:

**Prefix reuse.** Every request this app makes begins with the same system
prompt and the same four tool schemas, and the planning loop re-sends its whole
history each step — so most of what is sent has been sent before. A server with
prefix caching on skips re-computing it. The test sends one long prefix twice
and then a different one of the same length: if reuse is happening, the second
is much faster to first chunk than the first, and the third looks like the
first again. If all three match, nothing is being reused.

**Decode rate.** Tokens per second once generation is underway, which is what
speculative decoding would multiply. The draft call is one to three hundred
tokens of answer on top of however much the model thinks first, so this number
times that count is the floor under every answer.

Neither is a benchmark of the model's quality and neither touches the eval
suite. They exist to turn "ask the homelab to enable X" into "ask the homelab
to enable X, it is worth N seconds a question".
"""
from __future__ import annotations

import json
import statistics as st
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from filingdesk import config, llm  # noqa: E402

URL = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"

# Long enough that prefill is measurable against network jitter, and built from
# a repeated sentence rather than random tokens so that two prefixes of the
# same length are the same work to process.
FILLER = ("The registrant filed its quarterly report for the period, and the "
          "figures below are drawn from the exhibits attached to it. ")


def prefix(n_words: int, salt: str) -> str:
    reps = max(1, n_words // len(FILLER.split()))
    return f"[{salt}] " + FILLER * reps


def timed_stream(messages: list[dict], max_tokens: int = 24) -> dict:
    """One streamed call, timed. Returns first-chunk, first-text and total."""
    body = {"model": config.CHAT_MODEL, "messages": messages,
            "temperature": 0, "stream": True, "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}}
    if config.REASONING_EFFORT:
        body["reasoning_effort"] = config.REASONING_EFFORT

    t0 = time.monotonic()
    ttfr = ttft = None
    text: list[str] = []
    usage = {}
    r = requests.post(URL, json=body, headers=llm._headers(),
                      timeout=300, stream=True)
    if r.status_code >= 400:
        raise SystemExit(f"{r.status_code} from the endpoint: {r.text[:300]}")
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
        if ttfr is None:
            ttfr = time.monotonic() - t0
        if data.get("usage"):
            usage = data["usage"]
        delta = ((data.get("choices") or [{}])[0].get("delta") or {})
        if delta.get("content"):
            if ttft is None:
                ttft = time.monotonic() - t0
            text.append(delta["content"])
    return {"ttfr": ttfr, "ttft": ttft, "total": time.monotonic() - t0,
            "text": "".join(text), "usage": usage}


def probe_prefix_reuse(words: int = 1400, repeats: int = 3) -> None:
    """Three conditions, because two cannot tell the interesting apart.

    Comparing a repeated preamble against an unseen one measures whether the
    server reuses prefixes. It does NOT measure whether that is worth having —
    if prefill is cheap next to the fixed cost of getting a request in and out,
    then a perfect cache and no cache look the same, and both look like the
    round trip. So a short prompt goes in as the control: whatever it costs is
    overhead, and only what the long ones cost ABOVE it is prefill.
    """
    print(f"\nprefix reuse — {words} words of identical preamble, "
          f"{repeats} samples per condition, plus a short control")

    tiny = [timed_stream([{"role": "user", "content": "Reply with OK."}])
            for _ in range(repeats)]
    fixed = prefix(words, "fixed")
    # A different salt at the very START of each, so these share no prefix with
    # one another or with the fixed one — putting it at the end would leave the
    # whole preamble cacheable and measure nothing.
    cold = [timed_stream([{"role": "user", "content":
                           prefix(words, f"cold{i}") + "\nReply with OK."}])
            for i in range(repeats)]
    # First call with the fixed prefix populates the cache; the ones after it
    # are the measurement.
    timed_stream([{"role": "user", "content": fixed + "\nReply with OK."}])
    warm = [timed_stream([{"role": "user", "content":
                           fixed + f"\nReply with OK. ({i})"}])
            for i in range(repeats)]

    t = st.median(x["ttfr"] for x in tiny)
    c = st.median(x["ttfr"] for x in cold)
    w = st.median(x["ttfr"] for x in warm)
    print(f"  short prompt     : {t * 1000:6.0f} ms to first chunk (overhead)")
    print(f"  unseen preamble  : {c * 1000:6.0f} ms")
    print(f"  repeated preamble: {w * 1000:6.0f} ms")

    prefill = c - t
    if prefill <= 0.15:
        print(f"  -> prefill of {words} words costs {prefill * 1000:.0f} ms "
              f"over the {t * 1000:.0f} ms it takes to ask anything at all. "
              "Caching it, however well, cannot buy back time that is not "
              "being spent. Not worth asking for.")
        return
    saved = c - w
    if saved / prefill > 0.5:
        print(f"  -> prefix reuse is active: {saved * 1000:.0f} ms of the "
              f"{prefill * 1000:.0f} ms prefill is already skipped.")
    else:
        print(f"  -> prefill is {prefill * 1000:.0f} ms and is recomputed "
              "every call. Enabling prefix caching on the serving backend is "
              "worth about that much per model call at this prompt size.")


# The median draft call and the median wait for its first word, from the eval
# suite, so the projection below lands on this app's numbers rather than on a
# round one. Update them when the suite is re-run.
DRAFT_MEDIAN_S = 17.2
DRAFT_TTFT_MEDIAN_S = 12.9


def probe_decode_rate(samples: int = 3, want: int = 400) -> None:
    """Tokens per second across the WHOLE generation, reasoning included.

    The obvious version of this measurement is wrong in a way that flatters the
    endpoint by an order of magnitude: `completion_tokens` counts the reasoning
    tokens a gpt-oss model spends before it writes anything, so dividing it by
    the time from first WORD to last divides every token by the fraction of the
    time that produced only some of them. The first pass at this reported 195
    tokens/s and a 1-second draft, for a call the eval suite times at 17.
    Generation starts at the first chunk of any kind, so that is where the
    clock starts.
    """
    print(f"\ndecode rate — up to {want} tokens, {samples} samples")
    rates, splits = [], []
    for i in range(samples):
        res = timed_stream(
            [{"role": "user", "content":
              "Name three things a 10-Q contains and say why each matters. "
              f"Run {i}."}],
            max_tokens=want)
        u = res.get("usage") or {}
        out = u.get("completion_tokens")
        if not out:
            print("  endpoint reported no usage; measuring on characters")
            out = len(res["text"]) / 4
        detail = u.get("completion_tokens_details") or {}
        if detail.get("reasoning_tokens") is not None:
            splits.append((detail["reasoning_tokens"], out))
        span = res["total"] - (res["ttfr"] or 0)
        if span > 0:
            rates.append(out / span)
    if not rates:
        print("  could not measure")
        return
    rate = st.median(rates)
    print(f"  {rate:.1f} tokens/s across the whole generation")
    if splits:
        think, total = st.median(s[0] for s in splits), st.median(
            s[1] for s in splits)
        print(f"  {think:.0f} of {total:.0f} tokens were reasoning "
              f"({think / total * 100:.0f}%) — spent before the first word")
    print(f"  the suite's median draft call is {DRAFT_MEDIAN_S:.1f}s, of which "
          f"{DRAFT_TTFT_MEDIAN_S:.1f}s is before the first word")
    for factor, name in ((1.5, "conservative"), (2.2, "optimistic")):
        print(f"  -> speculative decoding at {factor}x ({name}): draft "
              f"{DRAFT_MEDIAN_S / factor:.1f}s, first word at "
              f"{DRAFT_TTFT_MEDIAN_S / factor:.1f}s "
              f"(-{DRAFT_MEDIAN_S - DRAFT_MEDIAN_S / factor:.1f}s a question, "
              "and the planning calls speed up by the same factor)")


def main() -> None:
    if not config.model_enabled():
        raise SystemExit(llm.NO_MODEL)
    print(f"endpoint: {config.LLM_BASE_URL}\nmodel   : {config.CHAT_MODEL}")
    probe_prefix_reuse()
    probe_decode_rate()
    print()


if __name__ == "__main__":
    main()
