"""Lite Cookbook — Odysseus Lite overlay (Phase 3).

Proves the first-run model recommender:
  - detects hardware and maps RAM to the right curated tier;
  - returns a concrete primary model pick + honest tokens/sec;
  - never downloads without explicit confirmation AND the opt-in env flag;
  - degrades gracefully when hardware detection returns nothing.
"""
import pytest

from services import cookbook_lite


def test_tier_mapping_by_ram():
    cases = {0: "balanced", 2: "minimal", 3.9: "minimal", 4: "balanced",
             7.9: "balanced", 8: "comfortable", 64: "comfortable"}
    for ram, expected in cases.items():
        assert cookbook_lite._tier_for_ram(ram)["id"] == expected, ram


def test_recommend_has_concrete_pick():
    rec = cookbook_lite.recommend({"total_ram_gb": 6, "available_ram_gb": 4,
                                   "cpu_cores": 4, "cpu_name": "x", "has_gpu": False})
    assert rec["tier"]["id"] == "balanced"
    assert rec["primary"] and rec["primary"]["name"]          # a concrete model
    assert rec["tier"]["tokens_per_sec"]                      # honest expectation
    assert rec["models"]                                       # full tier list
    assert "api_fallback" in rec


def test_recommend_suggests_api_on_tiny_ram():
    rec = cookbook_lite.recommend({"total_ram_gb": 2, "available_ram_gb": 1,
                                   "cpu_cores": 2, "cpu_name": "x", "has_gpu": False})
    assert rec["tier"]["id"] == "minimal"
    assert rec["suggest_api"] is True


def test_recommend_unknown_hardware_defaults_balanced():
    rec = cookbook_lite.recommend({"total_ram_gb": 0, "available_ram_gb": 0,
                                   "cpu_cores": 1, "cpu_name": "?", "has_gpu": False})
    assert rec["detection_ok"] is False
    assert rec["tier"]["id"] == "balanced"
    assert rec["suggest_api"] is False        # don't push API when we just don't know


def test_autopull_refuses_without_confirm():
    out = cookbook_lite.autopull(confirm=False)
    assert out["ok"] is False
    assert "confirm" in out["message"].lower()


def test_autopull_refuses_without_optin(monkeypatch):
    monkeypatch.delenv("LITE_AUTOPULL_MODEL", raising=False)
    out = cookbook_lite.autopull(confirm=True)
    assert out["ok"] is False
    assert "lite_autopull_model" in out["message"].lower()


def test_can_autopull_reports_reason(monkeypatch):
    monkeypatch.delenv("LITE_AUTOPULL_MODEL", raising=False)
    gate = cookbook_lite.can_autopull()
    assert gate["allowed"] is False
    assert gate["reason"]


def test_detect_hardware_shape():
    hw = cookbook_lite.detect_hardware()
    for key in ("total_ram_gb", "available_ram_gb", "cpu_cores", "cpu_name", "has_gpu"):
        assert key in hw
    assert hw["cpu_cores"] >= 1
