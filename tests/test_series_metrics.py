"""Tests for the series builder, the concept map and the metric registry.

Each of these is a regression: every case here is a bug that shipped in the
skeleton and was found by pointing the app at real filings rather than at
fixtures shaped like the happy path.
"""
import duckdb
import pytest

from filingdesk import agent, companies, config, metrics, series

CIK = 999999

# (concept, unit, start, end, val, form, accn, filed)
ROWS = [
    # Revenue under the LEGACY tag across the whole history, and under the
    # "modern" tag for a window in the middle only. This is NVDA's real
    # shape: picking one alias and stopping truncates the series.
    ("Revenues", "USD", "2024-01-01", "2024-03-31", 100.0, "10-Q", "a1", "2024-04-20"),
    ("Revenues", "USD", "2024-04-01", "2024-06-30", 110.0, "10-Q", "a2", "2024-07-20"),
    ("Revenues", "USD", "2024-07-01", "2024-09-30", 120.0, "10-Q", "a3", "2024-10-20"),
    ("Revenues", "USD", "2024-01-01", "2024-12-31", 500.0, "10-K", "a4", "2025-02-20"),
    ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD",
     "2024-01-01", "2024-03-31", 100.0, "10-Q", "a1", "2024-04-20"),

    ("GrossProfit", "USD", "2024-01-01", "2024-03-31", 60.0, "10-Q", "a1", "2024-04-20"),
    ("GrossProfit", "USD", "2024-04-01", "2024-06-30", 66.0, "10-Q", "a2", "2024-07-20"),
    ("GrossProfit", "USD", "2024-07-01", "2024-09-30", 72.0, "10-Q", "a3", "2024-10-20"),
    ("GrossProfit", "USD", "2024-01-01", "2024-12-31", 300.0, "10-K", "a4", "2025-02-20"),

    # Cash flow filed YEAR TO DATE — 3, 6, 9 and 12 month cumulative facts
    # sharing one start date. Only the first is a discrete quarter.
    ("NetCashProvidedByUsedInOperatingActivities", "USD",
     "2024-01-01", "2024-03-31", 40.0, "10-Q", "a1", "2024-04-20"),
    ("NetCashProvidedByUsedInOperatingActivities", "USD",
     "2024-01-01", "2024-06-30", 90.0, "10-Q", "a2", "2024-07-20"),
    ("NetCashProvidedByUsedInOperatingActivities", "USD",
     "2024-01-01", "2024-09-30", 150.0, "10-Q", "a3", "2024-10-20"),

    # Balance sheet: INSTANT facts, no start date at all.
    ("Assets", "USD", None, "2024-03-31", 1000.0, "10-Q", "a1", "2024-04-20"),
    ("Assets", "USD", None, "2024-06-30", 1100.0, "10-Q", "a2", "2024-07-20"),
    ("Assets", "USD", None, "2024-09-30", 1200.0, "10-Q", "a3", "2024-10-20"),

    # A restatement: same period, later filing, different value.
    ("NetIncomeLoss", "USD", "2024-01-01", "2024-03-31", 20.0, "10-Q", "a1", "2024-04-20"),
    ("NetIncomeLoss", "USD", "2024-01-01", "2024-03-31", 18.0, "10-K", "a4", "2025-02-20"),

    # Per-share data lives in USD/shares, which the USD-only filter dropped.
    ("EarningsPerShareDiluted", "USD/shares",
     "2024-01-01", "2024-03-31", 1.25, "10-Q", "a1", "2024-04-20"),
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE facts (
        cik BIGINT, entity VARCHAR, concept VARCHAR, unit VARCHAR,
        start DATE, "end" DATE, val DOUBLE, fy INTEGER, fp VARCHAR,
        form VARCHAR, accn VARCHAR, filed DATE, frame VARCHAR)""")
    con.executemany(
        "INSERT INTO facts VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?,?,NULL)",
        [(CIK, "TESTCO", c, u, s, e, v, form, accn, filed)
         for c, u, s, e, v, form, accn, filed in ROWS])
    con.close()
    monkeypatch.setattr(config, "DUCK", path)
    return path


# ---- alias merging -------------------------------------------------------

def test_series_merges_across_aliases(db):
    """The skeleton took the first alias with ANY data and stopped, which
    truncated the series to whatever window that tag happened to cover."""
    facts = series.quarterly(CIK, "Revenues", 8)
    assert [f.end for f in facts] == [
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]


def test_overlapping_aliases_do_not_double_count(db):
    """Both revenue tags report Q1. It must appear once, not twice."""
    facts = series.quarterly(CIK, "Revenues", 8)
    assert len([f for f in facts if f.end == "2024-03-31"]) == 1


# ---- derivation ----------------------------------------------------------

def test_q4_is_derived_from_the_fiscal_year(db):
    q4 = next(f for f in series.quarterly(CIK, "Revenues", 8)
              if f.end == "2024-12-31")
    assert q4.derived is True
    assert q4.value == pytest.approx(500.0 - (100 + 110 + 120))
    assert "minus Q1+Q2+Q3" in q4.derivation


def test_year_to_date_cash_flow_is_differenced_into_quarters(db):
    """Cash-flow statements are cumulative. Without differencing, a quarterly
    cash-flow chart shows one point a year."""
    facts = series.quarterly(CIK, "OperatingCashFlow", 8)
    by_end = {f.end: f for f in facts}
    assert by_end["2024-03-31"].value == pytest.approx(40.0)   # filed as-is
    assert by_end["2024-06-30"].value == pytest.approx(50.0)   # 90 - 40
    assert by_end["2024-09-30"].value == pytest.approx(60.0)   # 150 - 90
    assert by_end["2024-06-30"].derived is True
    assert by_end["2024-03-31"].derived is False


def test_a_derived_quarter_says_how_it_was_derived(db):
    q = next(f for f in series.quarterly(CIK, "OperatingCashFlow", 8)
             if f.end == "2024-06-30")
    assert "cumulative" in q.derivation


# ---- instant vs duration -------------------------------------------------

def test_balance_sheet_concepts_are_not_dropped(db):
    """Instant facts have no start date, so the duration path's
    `start IS NOT NULL` filter discarded every balance-sheet concept."""
    facts = series.quarterly(CIK, "Assets", 8)
    assert [f.value for f in facts] == [1000.0, 1100.0, 1200.0]
    assert all(not f.derived for f in facts)  # nothing to reconstruct


def test_annual_of_an_instant_concept_takes_the_last_snapshot(db):
    facts = series.annual(CIK, "Assets", 5)
    assert [f.end for f in facts] == ["2024-09-30"]


# ---- units ---------------------------------------------------------------

def test_per_share_data_survives_the_unit_filter(db):
    facts = series.quarterly(CIK, "EarningsPerShareDiluted", 8)
    assert [f.value for f in facts] == [1.25]
    assert facts[0].unit == "USD/shares"


# ---- restatement ---------------------------------------------------------

def test_latest_filing_wins_and_is_flagged_restated(db):
    facts = series.quarterly(CIK, "NetIncome", 8)
    q1 = next(f for f in facts if f.end == "2024-03-31")
    assert q1.value == pytest.approx(18.0)   # the 10-K supersedes the 10-Q
    assert q1.restated is True


# ---- metrics -------------------------------------------------------------

def test_metric_computes_from_filed_inputs(db):
    res = metrics.compute(CIK, "gross_margin", "quarterly", 8)
    assert res["unit"] == "ratio"
    by_end = {s["period_end"]: s["value"] for s in res["series"]}
    assert by_end["2024-03-31"] == pytest.approx(0.60)


def test_metric_returns_its_input_facts_for_provenance(db):
    res = metrics.compute(CIK, "gross_margin", "quarterly", 8)
    concepts = {f["concept"] for f in res["input_facts"]}
    assert "GrossProfit" in concepts and "Revenues" in concepts


def test_metric_names_the_concept_it_is_missing(db):
    res = metrics.compute(CIK, "return_on_equity", "quarterly", 8)
    assert res["error_kind"] == "no_concept_data"
    assert "StockholdersEquity" in res["missing"]


def test_unknown_metric_lists_the_known_ones(db):
    res = metrics.compute(CIK, "ebitda_margin", "quarterly", 8)
    assert res["error_kind"] == "no_such_metric"
    assert "gross_margin" in res["known"]


def test_only_computable_metrics_are_offered(db):
    keys = {m["key"] for m in metrics.available(CIK)}
    assert "gross_margin" in keys
    assert "return_on_equity" not in keys   # no equity in the fixture


# ---- concept catalog -----------------------------------------------------

def test_catalog_reports_only_concepts_with_data(db):
    keys = {c["key"] for c in series.concept_catalog(CIK)}
    assert {"Revenues", "GrossProfit", "Assets"} <= keys
    assert "Goodwill" not in keys


# ---- concurrent access ---------------------------------------------------

def test_reading_and_writing_can_overlap(db, monkeypatch):
    """Regression: DuckDB refuses to open one file with mixed read-only and
    read-write connections in the same process —

        Can't open a connection to same database file with a different
        configuration than existing connections

    which took the dashboard down with a 500 the moment a chart query
    overlapped a company auto-syncing. Both paths now share one handle.
    """
    from filingdesk import db as dbmod

    with dbmod.reading() as reader:
        reader.execute("SELECT count(*) FROM facts").fetchone()

    with dbmod.writing() as writer:                    # the write path
        writer.execute("DELETE FROM loaded WHERE cik = ?", [CIK])

    assert series.has_any(CIK) is True                  # the read path again


def test_a_write_does_not_lock_out_a_second_process(db):
    """The MCP tool server is a SUBPROCESS reading the same file. A cached
    read-write handle in the parent takes an exclusive lock and its queries
    come back empty rather than failing loudly — so readers must be read-only
    and writers must not linger."""
    import subprocess
    import sys

    from filingdesk import config
    from filingdesk import db as dbmod

    with dbmod.writing() as con:                       # a write, then released
        con.execute("SELECT 1")
    with dbmod.reading() as con:                       # parent holds a reader
        con.execute("SELECT count(*) FROM facts").fetchone()

        out = subprocess.run(
            [sys.executable, "-c",
             "import duckdb,sys;"
             f"c=duckdb.connect({str(config.DUCK)!r}, read_only=True);"
             "print(c.execute('SELECT count(*) FROM facts').fetchone()[0])"],
            capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    assert int(out.stdout.strip()) > 0


def test_concurrent_requests_do_not_collide(db):
    """Two dashboard requests in flight at once is the normal case under a
    web server, not an edge case."""
    import concurrent.futures as cf

    def work(i):
        from filingdesk import db as dbmod
        if i % 4 == 0:                       # interleave writes with reads
            with dbmod.writing() as con:
                con.execute("SELECT 1")
        return len(series.quarterly(CIK, "Revenues", 8))

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(work, range(16)))
    assert all(n == 4 for n in got)


# ---- synthetic mode must not touch real data -----------------------------

def test_stub_mode_uses_its_own_database(tmp_path, monkeypatch):
    """Synthetic fixtures used to be written into the REAL cache — a
    `DELETE FROM facts WHERE cik=1045810` followed by fabricated NVDA
    numbers. Running the eval harness therefore replaced genuine filings
    with invented ones, and every later chart showed them."""
    from filingdesk import config, stub
    from filingdesk import db as dbmod

    real = tmp_path / "real.duckdb"
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DUCK", real)
    dbmod.close_all()

    with dbmod.writing() as con:                      # a real cached filing
        con.execute("INSERT INTO facts VALUES "
                    "(1045810,'NVIDIA CORP','GrossProfit','USD',"
                    "'2025-01-01','2025-03-31',999.0,NULL,NULL,'10-Q',"
                    "'real-accn','2025-04-20',NULL)")

    stub.isolate()
    assert config.DUCK != real                        # redirected away
    stub.seed_duckdb()

    monkeypatch.setattr(config, "DUCK", real)         # look at the real one
    dbmod.close_all()
    with dbmod.reading() as con:
        rows = con.execute(
            "SELECT val, entity FROM facts WHERE cik = 1045810").fetchall()
    assert rows == [(999.0, "NVIDIA CORP")], "synthetic data leaked into real"
    dbmod.close_all()


def test_stub_marks_the_environment_for_child_processes(tmp_path, monkeypatch):
    """The MCP tools run in a subprocess. Without FD_STUB set they open the
    real database, and a 'synthetic' run quietly quotes genuine figures."""
    import os

    from filingdesk import config, stub
    from filingdesk import db as dbmod

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DUCK", tmp_path / "r.duckdb")
    monkeypatch.setattr(config, "VAULT_DB", tmp_path / "v.db")
    monkeypatch.delenv("FD_STUB", raising=False)
    monkeypatch.setattr(stub, "vault", _NullVault())
    dbmod.close_all()

    stub.install()
    assert os.environ.get("FD_STUB") == "1"
    dbmod.close_all()


class _NullVault:
    """install() indexes a vault; that is not what these tests are about."""

    def index(self, *a, **k):
        return 0


# ---- the company universe ------------------------------------------------

def test_search_ranks_exact_ticker_first():
    hits = companies.search("AAPL", 5)
    assert hits and hits[0]["ticker"] == "AAPL"


def test_search_finds_companies_by_name():
    hits = companies.search("NVIDIA", 5)
    assert any(h["ticker"] == "NVDA" for h in hits)


def test_universe_is_the_whole_registrant_list():
    assert companies.count() > 5000


def test_universe_behaves_like_the_dict_it_replaced():
    u = config.TICKERS
    assert "NVDA" in u
    assert u.get("NVDA") == 1045810
    assert u.get("NOTAREALTICKER") is None


def test_class_shares_resolve_in_either_spelling():
    """SEC writes BRK-B; every financial site writes BRK.B; people type both."""
    assert companies.resolve("BRK.B") == companies.resolve("BRK-B") == 1067983


def test_a_bare_cik_resolves_directly():
    """Registrants without a ticker are only reachable by number — and that
    is exactly where a holding-company reorganisation leaves the operating
    history (Exxon's sits under CIK 34088, which carries no ticker)."""
    assert companies.resolve("34088") == 34088
    assert companies.resolve("0000034088") == 34088


def test_cik_search_returns_the_registrant():
    hits = companies.search("1045810", 5)
    assert hits and hits[0]["cik"] == 1045810 and hits[0]["ticker"] == "NVDA"


# ---- ticker extraction ---------------------------------------------------

def test_prose_words_that_are_also_tickers_are_not_treated_as_mentions():
    """ALL, ON, IT, NOW and BE are real tickers. Substring-scanning a
    10,000-entry universe made every lowercase question mention a dozen
    companies, which then failed the entity-mismatch check."""
    got = agent.expected_tickers(
        "how has all of it moved on so far, and can we be sure?", "NVDA")
    assert got == {"NVDA"}


def test_a_capitalised_ticker_in_the_question_is_a_mention():
    got = agent.expected_tickers("Compare AMD gross margin", "NVDA")
    assert got == {"NVDA", "AMD"}
