"""API endpoints that need no network: health, saved list, view, ask without a key."""
import json

import pytest
from fastapi.testclient import TestClient

from app import api, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(store, "OUT_DIR", tmp_path / "out")
    return TestClient(api.app)


def minimal_timeline(appid=42):
    return {
        "appid": appid, "name": "Test Game", "generated_at": "2026-10-22T00:00:00+00:00",
        "method": "lexicon", "degraded": False, "events_analyzed": 0,
        "window": {}, "selection": {"strategy": "movers", "k": 6},
        "baseline": {"start_ts": 1_600_000_000, "end_ts": 1_610_000_000, "points": [
            {"date": "2020-09-01", "ts": 1_600_000_000, "approval": 0.5, "n": 10},
            {"date": "2021-01-01", "ts": 1_610_000_000, "approval": 0.8, "n": 10}]},
        "events": [], "llm_usage": {"calls": 0, "cost_usd": 0.0}, "cache": {"hit_rate": None},
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["keys_configured"]["deepseek"] is False      # blanked by conftest
    assert "DEEPSEEK" not in json.dumps(body)                 # names only, never values


def test_saved_list_and_view(client):
    store.save(minimal_timeline())
    rows = client.get("/api/timelines").json()
    assert rows[0]["appid"] == 42 and rows[0]["approval_last"] == 0.8
    page = client.get("/view/42")
    assert page.status_code == 200 and "Test Game" in page.text and "ask-form" in page.text
    assert client.get("/api/timeline/42").json()["name"] == "Test Game"   # saved copy, no build


def test_view_unknown_is_404(client):
    assert client.get("/view/999").status_code == 404


def test_ask_without_key_is_503(client):
    r = client.post("/api/ask", json={"question": "How is No Man's Sky doing?"})
    assert r.status_code == 503


def test_ask_validates_length(client):
    assert client.post("/api/ask", json={"question": "x"}).status_code == 422


def test_bad_select_is_422(client):
    assert client.get("/api/timeline/1?refresh=true&select=vibes").status_code == 422


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "build-form" in r.text
