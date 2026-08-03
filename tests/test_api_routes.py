"""Every page and the health endpoint, over HTTP.

These exist because of a specific failure: renaming a setting in config.py
broke `/api/health` — and with it the health chip on every page — while the
whole suite stayed green, because nothing in it had ever made a request. Unit
tests over `web.render` prove the templates are right about their own markup;
they cannot prove the app can build the context to render them with.

`llm.ping` is stubbed throughout. It is the one thing in the health payload
that reaches the network, and a test suite that quietly depends on a model
endpoint being up is a test suite that fails for reasons it is not about.
"""
import pytest
from fastapi.testclient import TestClient

from filingdesk import api, config, llm


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(llm, "ping", lambda: (True, "stubbed"))
    return TestClient(api.app)


@pytest.mark.parametrize("path", ["/", "/app", "/app?ticker=AAPL", "/ask"])
def test_every_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.text.lstrip().startswith("<!doctype html>")


def test_health_answers_and_names_the_endpoint(client):
    h = client.get("/api/health").json()
    assert h["endpoint"] == config.LLM_BASE_URL
    assert h["model_label"] == llm.describe()
    assert set(h) >= {"status", "ready", "facts_loaded", "universe",
                      "model", "model_enabled", "model_online"}


def test_health_survives_a_broken_data_layer(client, monkeypatch):
    """It is the endpoint a container healthcheck calls. Raising there turns a
    degraded instance into an unreachable one."""
    def boom(*a, **k):
        raise RuntimeError("duckdb is gone")

    monkeypatch.setattr(api.db, "reading", boom)
    h = client.get("/api/health").json()
    assert h["ready"] is False
    assert h["facts_loaded"] is None


def test_health_html_renders_a_chip_either_way(client, monkeypatch):
    assert "chip" in client.get("/api/health/html").text
    monkeypatch.setattr(config, "CHAT_MODEL", "")
    assert "no model" in client.get("/api/health/html").text


def test_the_landing_page_kept_its_old_address(client):
    r = client.get("/landing", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == f"{config.BASE_PATH}/"


def test_ui_config_describes_this_instance(client):
    c = client.get("/api/config").json()
    assert c["endpoint"] == config.LLM_BASE_URL
    assert isinstance(c["model_enabled"], bool)


def test_static_files_are_served(client):
    for name in ("app.css", "landing.css", "dashboard.js"):
        assert client.get(f"/static/{name}").status_code == 200


def test_the_stylesheet_asks_for_fonts_relative_to_itself(client):
    """Absolute /static/fonts/… resolves past the mount prefix. Under one, the
    page rendered in fallback faces while every font it asked for 404'd."""
    css = client.get("/static/landing.css").text
    assert 'url("fonts/' in css
    assert 'url("/static/fonts/' not in css   # the comment above it may say so
