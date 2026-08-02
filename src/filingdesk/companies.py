"""The company universe — every ticker the SEC publishes, not a hardcoded three.

The skeleton shipped `config.TICKERS = {"NVDA": ..., "AMD": ..., "AAPL": ...}`
and README listed that as the top known limitation. This replaces it with the
authoritative list: `company_tickers.json`, ~10,400 registrants, refreshed
daily and cached on disk.

Two things are deliberate.

**It behaves like the dict it replaced.** `Universe` implements the Mapping
protocol, so `config.TICKERS.get(t)`, `t in config.TICKERS` and
`sorted(config.TICKERS)` all still work at every existing call site. Swapping
the data source should not mean rewriting the consumers.

**It degrades instead of failing.** A cold start with no network falls back to
the cached copy, and failing that to a small built-in set. A company lookup is
not worth a hard boot failure — the app can still answer for what it has.
"""
from __future__ import annotations

import json
import threading
import time

import requests

from . import config

URL = "https://www.sec.gov/files/company_tickers.json"
CACHE = config.ROOT / "company_tickers.json"
TTL = 24 * 60 * 60  # the SEC regenerates this file daily

# Last-resort seed so a cold start with no network still resolves something.
FALLBACK = {
    "AAPL": (320193, "Apple Inc."),
    "MSFT": (789019, "MICROSOFT CORP"),
    "NVDA": (1045810, "NVIDIA CORP"),
    "AMZN": (1018724, "AMAZON COM INC"),
    "GOOGL": (1652044, "Alphabet Inc."),
    "META": (1326801, "Meta Platforms, Inc."),
    "TSLA": (1318605, "Tesla, Inc."),
    "AMD": (2488, "ADVANCED MICRO DEVICES INC"),
    "INTC": (50863, "INTEL CORP"),
    "JPM": (19617, "JPMORGAN CHASE & CO"),
}

_lock = threading.Lock()
_cache: dict | None = None


def _download() -> dict[str, tuple[int, str]]:
    if not config.SEC_UA:
        raise RuntimeError("SEC_USER_AGENT not set")
    r = requests.get(URL, headers={"User-Agent": config.SEC_UA}, timeout=30)
    r.raise_for_status()
    out = {}
    for row in r.json().values():
        t = str(row.get("ticker", "")).upper().strip()
        if t:
            out[t] = (int(row["cik_str"]), str(row.get("title", "")).strip())
    if not out:
        raise RuntimeError("company_tickers.json parsed to zero rows")
    return out


def _read_cache() -> dict[str, tuple[int, str]] | None:
    try:
        raw = json.loads(CACHE.read_text(encoding="utf-8"))
        return {t: (int(c), n) for t, (c, n) in raw.items()}
    except Exception:  # noqa: BLE001 — a bad cache is a cache miss
        return None


def _write_cache(data: dict[str, tuple[int, str]]) -> None:
    try:
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({t: [c, n] for t, (c, n) in data.items()}),
                       encoding="utf-8")
        tmp.replace(CACHE)  # atomic: a reader never sees a half-written file
    except OSError:
        pass


def load(force: bool = False) -> dict[str, tuple[int, str]]:
    """ticker -> (cik, company name). Cached in memory and on disk."""
    global _cache
    with _lock:
        fresh = (CACHE.exists()
                 and time.time() - CACHE.stat().st_mtime < TTL)
        if _cache is not None and fresh and not force:
            return _cache

        if not force and fresh:
            got = _read_cache()
            if got:
                _cache = got
                return _cache

        try:
            got = _download()
            _write_cache(got)
        except Exception:  # noqa: BLE001 — network is optional at boot
            got = _read_cache() or dict(FALLBACK)

        _cache = got
        return _cache


def _variants(ticker: str) -> list[str]:
    """Ticker spellings that mean the same company.

    Class shares are the reason: the SEC writes Berkshire's B shares as
    BRK-B, every financial site writes BRK.B, and people type both.
    """
    t = str(ticker or "").upper().strip()
    out = [t]
    if "." in t:
        out.append(t.replace(".", "-"))
    if "-" in t:
        out.append(t.replace("-", "."))
    return out


def _lookup(ticker: str) -> tuple[int, str] | None:
    data = load()
    for v in _variants(ticker):
        if v in data:
            return data[v]
    # A bare number is a CIK. Registrants without a ticker are unreachable
    # otherwise, and they are not rare: after a holding-company
    # reorganisation the ticker points at the new shell while the operating
    # history stays under the predecessor's CIK, which has no ticker at all.
    raw = str(ticker or "").strip().lstrip("0")
    if raw.isdigit():
        return (int(raw), f"CIK {int(raw):010d}")
    return None


class Universe:
    """Mapping of ticker -> CIK, backed by the SEC list. Lazy: nothing is
    fetched until something actually asks for a company."""

    def _data(self) -> dict[str, tuple[int, str]]:
        return load()

    def __getitem__(self, ticker: str) -> int:
        row = _lookup(ticker)
        if row is None:
            raise KeyError(ticker)
        return row[0]

    def __contains__(self, ticker: object) -> bool:
        return _lookup(str(ticker)) is not None

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def get(self, ticker: str, default=None):
        row = _lookup(ticker)
        return row[0] if row else default

    def name(self, ticker: str, default: str = "") -> str:
        row = _lookup(ticker)
        return row[1] if row else default

    def keys(self):
        return self._data().keys()


def search(q: str, limit: int = 12) -> list[dict]:
    """Ticker or company-name search, ranked most-exact first.

    Ranking matters more than it looks: typing "A" should surface Agilent
    before 400 companies whose name merely contains an "a".
    """
    q = (q or "").strip().upper()
    if not q:
        return []
    data = load()

    # A bare CIK resolves directly, whether or not it carries a ticker.
    if q.lstrip("0").isdigit():
        cik = int(q.lstrip("0"))
        named = next(((t, n) for t, (c, n) in data.items() if c == cik), None)
        return [{"ticker": named[0] if named else str(cik),
                 "cik": cik,
                 "name": named[1] if named else f"CIK {cik:010d}"}]

    scored: list[tuple[int, str, int, str]] = []
    for tic, (cik, name) in data.items():
        up = name.upper()
        if tic == q:
            rank = 0
        elif tic.startswith(q):
            rank = 1
        elif up.startswith(q):
            rank = 2
        elif q in tic:
            rank = 3
        elif q in up:
            rank = 4
        else:
            continue
        scored.append((rank, tic, cik, name))
    scored.sort(key=lambda r: (r[0], len(r[1]), r[1]))
    return [{"ticker": t, "cik": c, "name": n}
            for _, t, c, n in scored[:limit]]


def resolve(ticker: str) -> int | None:
    return Universe().get(ticker)


def name_of(ticker: str) -> str:
    return Universe().name(ticker, str(ticker).upper())


def count() -> int:
    return len(load())
