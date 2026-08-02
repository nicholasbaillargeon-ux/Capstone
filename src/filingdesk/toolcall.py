"""The seam. Tuesday's finding: this is the only stochastic link left, and a
wrong-but-valid tool call is invisible to the grounding guard because the
numbers it returns are real.

So validity checking is not enough. Three layers:

  1. STRUCTURAL — does the call name a real tool with parseable arguments?
  2. SCHEMA     — required params present, types coercible, enums respected?
  3. SEMANTIC   — does the call match the question that was actually asked?

Layer 3 is the one that matters and the one a JSON-schema validator will not
give you. `fd_compute_metric(ticker="AMD")` for a question about NVDA is a
perfectly valid call that produces a confidently wrong report.

Every rejection produces a message aimed at the model, not at a human, because
it goes straight back into the conversation as a retry prompt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


class ToolCallError(Exception):
    def __init__(self, kind: str, message: str, hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint

    def for_model(self) -> str:
        return json.dumps({"error": self.message, "hint": self.hint,
                           "instruction": "Correct the call and try again."})


@dataclass
class Validated:
    name: str
    args: dict


def _coerce(value, jtype: str, key: str):
    try:
        if jtype == "integer":
            return int(value)
        if jtype == "number":
            return float(value)
        if jtype == "string":
            return str(value)
        if jtype == "boolean":
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes"}
            return bool(value)
    except (TypeError, ValueError) as e:
        raise ToolCallError(
            "bad_type", f"Argument {key!r} must be a {jtype}, got {value!r}.",
            f"Send {key} as a {jtype}.") from e
    return value


def validate(name: str, raw_args, schemas: dict[str, dict],
             expected: set[str], universe: dict[str, int]
             ) -> tuple[Validated, list[str]]:
    """Returns (validated call, argument names that were dropped as unknown)."""
    # --- layer 1: structural -------------------------------------------
    if name not in schemas:
        raise ToolCallError(
            "unknown_tool", f"No tool named {name!r}.",
            f"Available tools: {', '.join(sorted(schemas))}.")

    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            raise ToolCallError("bad_json",
                                f"Arguments were not valid JSON: {e}.",
                                "Send arguments as a JSON object.") from e
    if not isinstance(raw_args, dict):
        raise ToolCallError("bad_args",
                            f"Arguments must be an object, got {type(raw_args).__name__}.",
                            "Send arguments as a JSON object.")

    # --- layer 2: schema -----------------------------------------------
    schema = schemas[name] or {}
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    missing = [k for k in required if k not in raw_args]
    if missing:
        raise ToolCallError(
            "missing_args", f"Missing required argument(s): {', '.join(missing)}.",
            f"{name} requires: {', '.join(required)}.")

    args, dropped = {}, []
    for k, v in raw_args.items():
        if k not in props:
            dropped.append(k)
            continue
        spec = props[k]
        jtype = spec.get("type")
        if isinstance(jtype, list):
            jtype = next((t for t in jtype if t != "null"), None)
        args[k] = _coerce(v, jtype, k) if jtype else v
        enum = spec.get("enum")
        if enum and args[k] not in enum:
            raise ToolCallError(
                "bad_enum", f"Argument {k!r}={args[k]!r} is not allowed.",
                f"{k} must be one of: {', '.join(map(str, enum))}.")

    # --- layer 3: semantic ---------------------------------------------
    if "ticker" in args:
        t = str(args["ticker"]).upper().strip()
        if t not in universe:
            # The hint names the request's own company rather than listing the
            # universe: it is ~10,400 tickers long, and the model's actual
            # mistake is almost always a typo or a hallucinated symbol.
            raise ToolCallError(
                "unknown_entity", f"{t!r} is not an SEC-registered ticker.",
                f"This request is about {', '.join(sorted(expected))}."
                if expected else "Use a listed ticker symbol.")
        if t not in expected:
            raise ToolCallError(
                "entity_mismatch",
                f"You called for {t}, but this request is about "
                f"{', '.join(sorted(expected)) or 'another company'}.",
                "Use only the company this request is about.")
        args["ticker"] = t

    return Validated(name=name, args=args), dropped
