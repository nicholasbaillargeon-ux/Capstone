"""Configuration, the company universe handle, and the concept map."""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Read .env from the repo root, without overriding the real environment.

    Every entry point needs the same settings — the server, the CLI, the eval
    harness, `python -m filingdesk.models` — and having only the launcher
    script read .env meant a key that worked for the server was invisible to
    the tool built to verify it. Explicit environment still wins, so Docker
    and systemd override the file rather than fight it.

    Hand-rolled rather than python-dotenv: the SEC User-Agent contains
    parentheses and spaces, this is a dozen lines, and it avoids a dependency
    for something the app reads exactly once at import.
    """
    path = Path(__file__).resolve().parents[2] / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

ROOT = Path(os.environ.get("FD_ROOT", Path.home() / ".filingdesk"))
DUCK = ROOT / "filings.duckdb"
VAULT_DB = ROOT / "vault.db"
VAULT_DIR = os.environ.get("FD_VAULT_DIR", str(Path.home() / "brain"))

# ---- the model ------------------------------------------------------------
# "none"   dashboard only. Not a degraded state: charts, figures, ratios and
#          provenance never touch a model, so this is a complete surface on
#          its own and the UI says so plainly rather than warning about it.
# "ollama" keeps everything on this machine.
# "openai" any OpenAI-compatible endpoint — Fireworks, Together, Groq,
#          OpenRouter, vLLM, llama.cpp's server, LM Studio — which is how you
#          run a bigger open-weights model than local RAM allows. See llm.py
#          for what that trades away.
LLM_PROVIDER = os.environ.get("FD_LLM_PROVIDER", "ollama").strip().lower()
if LLM_PROVIDER in ("", "none", "off", "disabled"):
    LLM_PROVIDER = "none"


def model_enabled() -> bool:
    """Derived, never stored: a cached copy of this can disagree with
    LLM_PROVIDER, and then the app refuses to answer while the health chip
    insists a model is configured."""
    return LLM_PROVIDER != "none"

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

LLM_BASE_URL = os.environ.get("FD_LLM_BASE_URL",
                              "https://api.fireworks.ai/inference/v1")
LLM_API_KEY = os.environ.get("FD_LLM_API_KEY", "")

FIREWORKS_URL = "https://api.fireworks.ai/inference/v1"

# How hard the model is asked to think before answering. Reasoning models
# (gpt-oss, o-series, and anything LiteLLM maps onto them) spend real wall
# clock here: on this deployment the same draft call measured 1.9s at "low"
# and 9.1s at "high", and the endpoint's own default sat near 5s. Every
# request pays it two to four times over, once per planning step plus the
# draft, which is where a 35-second answer comes from.
#
# Measured on the eval suite against gpt-oss-120b, 15 cases each:
#
#   endpoint default   13/15   median 34.4s   worst 95.2s
#   low                12/15   median 14.0s   worst 24.5s
#   medium             14/15   median 31.1s   worst 70.5s
#   plan=med draft=low 11/15   median 24.5s   worst 82.5s
#
# "medium" wins on both counts that matter: it is the most accurate setting
# measured — better than the endpoint's own default — and it cuts the worst
# case by a quarter. It also stopped needing repair rounds entirely, which is
# where the rest of the saving comes from.
#
# The last row is the interesting negative result, and the reason these are two
# knobs rather than one. The guess was that drafting needs no deliberation —
# the figures are already retrieved, computed and checked, so the model only
# copies them into four sentences. The opposite is true: at "low" the draft
# rewrites 64.78 as 0.6478 and 0.75 as "0.7500 b", and no amount of instruction
# in the DRAFT prompt stopped it. Careful transcription is apparently the thing
# the reasoning budget buys here. Planning tolerates less, but not by enough to
# be worth splitting by default.
#
# Empty means "send nothing", for endpoints that reject the parameter.
REASONING_EFFORT = os.environ.get("FD_REASONING_EFFORT", "medium").strip().lower()
PLAN_REASONING_EFFORT = os.environ.get(
    "FD_PLAN_REASONING_EFFORT", REASONING_EFFORT).strip().lower()
if REASONING_EFFORT in ("none", "off", "default"):
    REASONING_EFFORT = ""
if PLAN_REASONING_EFFORT in ("none", "off", "default"):
    PLAN_REASONING_EFFORT = ""

# A default model name is only meaningful when we know whose catalogue it
# refers to. Fireworks' IDs are verified against their own listing
# (deepseek-v4-pro is their top pick for tool-using agents, which is what this
# app is) — but on a self-hosted LiteLLM proxy the names are whatever that
# proxy defines, so guessing one produces a confident 404 instead of an
# answer. Unknown endpoint => no default; `python -m filingdesk.models` lists
# what is actually there.
_DEFAULT_MODELS = {
    "none": ("", ""),
    "ollama": ("qwen2.5:7b-instruct-q4_K_M", "nomic-embed-text"),
    "openai": (("accounts/fireworks/models/deepseek-v4-pro",
                "accounts/fireworks/models/qwen3-embedding-8b")
               if LLM_BASE_URL.rstrip("/") == FIREWORKS_URL else ("", "")),
}
_chat_default, _embed_default = _DEFAULT_MODELS.get(
    LLM_PROVIDER, _DEFAULT_MODELS["ollama"])

CHAT_MODEL = os.environ.get("FD_CHAT_MODEL", "").strip() or _chat_default
EMBED_MODEL = os.environ.get("FD_EMBED_MODEL", "").strip() or _embed_default

# Planning and drafting can run on different models. Selecting a tool from four
# is a smaller job than transcribing figures without mangling them, and the
# planning loop is where most of the wall clock goes — so a smaller, faster
# model here is the obvious trade to test. Defaults to the same model, which
# makes it a no-op until someone sets it.
PLAN_CHAT_MODEL = os.environ.get("FD_PLAN_CHAT_MODEL", "").strip() or CHAT_MODEL

# Whether to end the planning loop as soon as a step has produced facts,
# rather than making one more call to hear the model say it wants no more
# tools. Saves a full round trip per question; costs any question whose plan
# genuinely needs a second round of tool calls to be decided after seeing the
# first round's results. Off until the eval suite says otherwise.
STOP_ON_FIRST_FACTS = os.environ.get(
    "FD_STOP_ON_FIRST_FACTS", "").strip().lower() in ("1", "true", "yes", "on")


def model_configured() -> bool:
    """Enabled AND actually told which model to call."""
    return model_enabled() and bool(CHAT_MODEL)

# Where this instance is mounted. Empty by default: the app owns "/" and every
# URL it writes is absolute from there.
#
# Behind a proxy that serves it under a prefix — the combined showcase puts it
# at /filing-desk/ alongside two other apps — the prefix is stripped before the
# request arrives, so routing needs no change. What breaks without this is
# every URL the app *emits*: /static/app.css, /ask, /api/company/…, the SSE
# stream. Each would resolve against the proxy's root and hit whatever else
# lives there.
#
# Normalised to "" or "/prefix" — one leading slash, no trailing one — so a
# template can write {{ base }}/static/app.css and be right either way.
BASE_PATH = "/" + os.environ.get("FD_BASE_PATH", "").strip().strip("/")
if BASE_PATH == "/":
    BASE_PATH = ""

# SEC requires a descriptive User-Agent with a real contact email.
# No default. Failing loudly here is correct.
SEC_UA = os.environ.get("SEC_USER_AGENT")

# How long a company's cached filings stay fresh before a page load refetches
# them by itself. One hour, not twelve: a companyfacts pull is one request and
# well under a second, so the old window mostly served to make the UI look
# stale until somebody pressed a button. Syncing is the app's job.
FILINGS_TTL_HOURS = float(os.environ.get("FD_FILINGS_TTL_HOURS", "1"))

# Shown as one-click examples on the dashboard. Not a limit on what resolves —
# the universe below is every ticker the SEC publishes.
FEATURED = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AMD",
            "INTC", "JPM", "WMT", "KO"]

# Units worth keeping. The skeleton kept USD only, which silently dropped
# per-share figures and share counts — i.e. everything needed to chart EPS.
UNITS = {"USD", "USD/shares", "shares", "pure"}

# ---- the concept map -----------------------------------------------------
# Filers tag the same line item several ways; first alias with data wins.
#
# `kind` matters more than it looks. Income-statement and cash-flow items are
# DURATION facts (a start and an end — a quarter is a slice of time). Balance
# sheet items are INSTANT facts with no start at all, a snapshot on one date.
# Treating the second like the first is why the skeleton's `start IS NOT NULL`
# filter dropped every balance-sheet concept on the floor.

CONCEPTS: dict[str, dict] = {
    # ---- income statement (duration) ----
    "Revenues": {
        "label": "Revenue", "kind": "duration", "group": "Income statement",
        "tags": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                 "RevenueFromContractWithCustomerIncludingAssessedTax",
                 "Revenues", "SalesRevenueNet", "SalesRevenueGoodsNet"]},
    "CostOfRevenue": {
        "label": "Cost of revenue", "kind": "duration", "group": "Income statement",
        "tags": ["CostOfRevenue", "CostOfGoodsAndServicesSold",
                 "CostOfGoodsSold", "CostOfServices"]},
    "GrossProfit": {
        "label": "Gross profit", "kind": "duration", "group": "Income statement",
        "tags": ["GrossProfit"]},
    "ResearchAndDevelopment": {
        "label": "R&D expense", "kind": "duration", "group": "Income statement",
        "tags": ["ResearchAndDevelopmentExpense",
                 "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]},
    "SellingGeneralAndAdministrative": {
        "label": "SG&A expense", "kind": "duration", "group": "Income statement",
        "tags": ["SellingGeneralAndAdministrativeExpense",
                 "GeneralAndAdministrativeExpense"]},
    "OperatingExpenses": {
        "label": "Operating expenses", "kind": "duration", "group": "Income statement",
        "tags": ["OperatingExpenses", "CostsAndExpenses",
                 "OperatingCostsAndExpenses"]},
    "OperatingIncome": {
        "label": "Operating income", "kind": "duration", "group": "Income statement",
        "tags": ["OperatingIncomeLoss"]},
    "InterestExpense": {
        "label": "Interest expense", "kind": "duration", "group": "Income statement",
        "tags": ["InterestExpense", "InterestExpenseDebt",
                 "InterestIncomeExpenseNet"]},
    "IncomeTaxExpense": {
        "label": "Income tax expense", "kind": "duration", "group": "Income statement",
        "tags": ["IncomeTaxExpenseBenefit"]},
    "NetIncome": {
        "label": "Net income", "kind": "duration", "group": "Income statement",
        "tags": ["NetIncomeLoss", "ProfitLoss",
                 "NetIncomeLossAvailableToCommonStockholdersBasic"]},
    "EarningsPerShareDiluted": {
        "label": "EPS (diluted)", "kind": "duration", "group": "Per share",
        "unit": "USD/shares", "tags": ["EarningsPerShareDiluted"]},
    "EarningsPerShareBasic": {
        "label": "EPS (basic)", "kind": "duration", "group": "Per share",
        "unit": "USD/shares", "tags": ["EarningsPerShareBasic"]},
    "DilutedShares": {
        "label": "Diluted shares", "kind": "duration", "group": "Per share",
        "unit": "shares",
        "tags": ["WeightedAverageNumberOfDilutedSharesOutstanding"]},

    # ---- cash flow (duration) ----
    "OperatingCashFlow": {
        "label": "Operating cash flow", "kind": "duration", "group": "Cash flow",
        "tags": ["NetCashProvidedByUsedInOperatingActivities",
                 "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]},
    "CapitalExpenditures": {
        "label": "Capital expenditure", "kind": "duration", "group": "Cash flow",
        "tags": ["PaymentsToAcquirePropertyPlantAndEquipment",
                 "PaymentsToAcquireProductiveAssets"]},
    "DepreciationAndAmortization": {
        "label": "D&A", "kind": "duration", "group": "Cash flow",
        "tags": ["DepreciationDepletionAndAmortization",
                 "DepreciationAmortizationAndAccretionNet", "Depreciation"]},
    "ShareBasedCompensation": {
        "label": "Stock-based comp", "kind": "duration", "group": "Cash flow",
        "tags": ["ShareBasedCompensation",
                 "AllocatedShareBasedCompensationExpense"]},
    "DividendsPaid": {
        "label": "Dividends paid", "kind": "duration", "group": "Cash flow",
        "tags": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]},
    "StockRepurchased": {
        "label": "Buybacks", "kind": "duration", "group": "Cash flow",
        "tags": ["PaymentsForRepurchaseOfCommonStock"]},

    # ---- balance sheet (instant) ----
    "Assets": {
        "label": "Total assets", "kind": "instant", "group": "Balance sheet",
        "tags": ["Assets"]},
    "CurrentAssets": {
        "label": "Current assets", "kind": "instant", "group": "Balance sheet",
        "tags": ["AssetsCurrent"]},
    "Liabilities": {
        "label": "Total liabilities", "kind": "instant", "group": "Balance sheet",
        "tags": ["Liabilities"]},
    "CurrentLiabilities": {
        "label": "Current liabilities", "kind": "instant", "group": "Balance sheet",
        "tags": ["LiabilitiesCurrent"]},
    "StockholdersEquity": {
        "label": "Shareholders' equity", "kind": "instant", "group": "Balance sheet",
        "tags": ["StockholdersEquity",
                 "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]},
    "CashAndEquivalents": {
        "label": "Cash & equivalents", "kind": "instant", "group": "Balance sheet",
        "tags": ["CashAndCashEquivalentsAtCarryingValue",
                 "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]},
    "Inventory": {
        "label": "Inventory", "kind": "instant", "group": "Balance sheet",
        "tags": ["InventoryNet"]},
    "AccountsReceivable": {
        "label": "Accounts receivable", "kind": "instant", "group": "Balance sheet",
        "tags": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"]},
    "LongTermDebt": {
        "label": "Long-term debt", "kind": "instant", "group": "Balance sheet",
        "tags": ["LongTermDebtNoncurrent", "LongTermDebt"]},
    "Goodwill": {
        "label": "Goodwill", "kind": "instant", "group": "Balance sheet",
        "tags": ["Goodwill"]},
}

# Back-compat view: {logical: [tags]}. series.py and the tests read this.
CONCEPT_ALIASES: dict[str, list[str]] = {k: v["tags"]
                                         for k, v in CONCEPTS.items()}


def concept_kind(logical: str) -> str:
    return CONCEPTS.get(logical, {}).get("kind", "duration")


def concept_label(logical: str) -> str:
    return CONCEPTS.get(logical, {}).get("label", logical)


def concept_unit(logical: str) -> str:
    return CONCEPTS.get(logical, {}).get("unit", "USD")


ROOT.mkdir(parents=True, exist_ok=True)


def __getattr__(name: str):
    """`config.TICKERS` is the full SEC universe, resolved on first touch.

    Deferred rather than imported at module scope because `companies` imports
    this module; doing it lazily means neither has to be loaded first.
    """
    if name == "TICKERS":
        from . import companies
        value = companies.Universe()
        globals()["TICKERS"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
