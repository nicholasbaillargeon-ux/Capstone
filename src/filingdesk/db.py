"""Database access, with DuckDB's concurrency rules made explicit.

DuckDB is in-process and has two constraints that pull against each other,
and this app manages to hit both:

  1. WITHIN a process, every connection to a file must use the SAME
     configuration. Opening one `read_only=True` alongside one
     `read_only=False` raises

         Can't open a connection to same database file with a different
         configuration than existing connections

     which is what took the dashboard down: chart queries opened read-only
     while a company auto-syncing opened read-write.

  2. ACROSS processes, a read-write connection takes an EXCLUSIVE lock, but
     any number of read-only connections can coexist. The MCP tool server
     runs as a subprocess and reads the same file, so the web process cannot
     simply hold a read-write handle open — that locks the tools out
     entirely, and their queries come back empty rather than failing loudly.

So: readers use a shared read-only connection, writers take a brief exclusive
window, and a process-wide lock guarantees the two never overlap. Writes are
rare (a company sync) and reads are milliseconds, so serialising them inside
one process costs nothing measurable and removes a whole class of bug.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import duckdb

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    cik BIGINT, entity VARCHAR, concept VARCHAR, unit VARCHAR,
    start DATE, "end" DATE, val DOUBLE, fy INTEGER, fp VARCHAR,
    form VARCHAR, accn VARCHAR, filed DATE, frame VARCHAR
);
CREATE TABLE IF NOT EXISTS loaded (
    cik BIGINT PRIMARY KEY, ticker VARCHAR, entity VARCHAR,
    n_facts BIGINT, refreshed TIMESTAMP
);
"""

# Reentrant: a write path may call something that reads.
_lock = threading.RLock()
_readers: dict[str, duckdb.DuckDBPyConnection] = {}


def _path() -> str:
    return str(config.DUCK)


def _close_readers() -> None:
    for con in _readers.values():
        try:
            con.close()
        except Exception:  # noqa: BLE001 — closing must never raise
            pass
    _readers.clear()


def _create_if_missing(path: str) -> None:
    """A read-only connection cannot create the file, so a cold start needs
    one write first."""
    con = duckdb.connect(path)
    try:
        con.execute(SCHEMA)
    finally:
        con.close()


def _reader(path: str) -> duckdb.DuckDBPyConnection:
    con = _readers.get(path)
    if con is None:
        try:
            con = duckdb.connect(path, read_only=True)
        except duckdb.Error:
            _create_if_missing(path)
            con = duckdb.connect(path, read_only=True)
        _readers[path] = con
    return con


@contextmanager
def reading():
    """A read-only cursor. Held under the lock so a writer cannot close the
    underlying connection mid-query."""
    with _lock:
        cur = _reader(_path()).cursor()
        try:
            yield cur
        finally:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass


@contextmanager
def writing():
    """An exclusive read-write connection.

    Cached readers are closed first and reopened lazily afterwards: their
    read-only handles would otherwise both violate rule 1 and hold the file
    against rule 2's exclusive lock.
    """
    with _lock:
        _close_readers()
        con = duckdb.connect(_path())
        try:
            con.execute(SCHEMA)
            yield con
        finally:
            con.close()


def ensure_schema() -> None:
    with _lock:
        _create_if_missing(_path())


def close_all() -> None:
    """Drop every cached handle — for tests, for stub isolation, and so a
    shutdown does not leave a stale lock behind."""
    with _lock:
        _close_readers()
