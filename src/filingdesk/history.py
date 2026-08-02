"""Conversation history. Survives restarts because it lives on the volume."""
import json

from . import config

PATH = config.ROOT / "history" / "reports.jsonl"


def append(result: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in result.items() if k != "fact_table"}
    with PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(slim, default=str) + "\n")


def recent(n: int = 20) -> list[dict]:
    if not PATH.exists():
        return []
    lines = PATH.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(x) for x in lines[-n:]]
