"""Tests verifying UI banner rendering across execution modes: live, recorded, and sim."""

import pytest
from httpx import ASGITransport, AsyncClient

from alloc_platform.api.main import app
from alloc_platform.config import config


@pytest.mark.asyncio
async def test_sim_mode_banner(monkeypatch):
    """Verify that SIMULATED banner appears when in sim mode."""
    monkeypatch.setattr(config, "alloc_mode", "sim")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "SIMULATED DATA (Offline Fluid Model - Not Measured on Real EC2 Hardware)" in resp.text
        assert "RECORDED real measurements" not in resp.text


@pytest.mark.asyncio
async def test_recorded_mode_banner(monkeypatch):
    """Verify that RECORDED banner with date range, run count, and instance type appears in recorded mode."""
    monkeypatch.setattr(config, "alloc_mode", "recorded")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "RECORDED real measurements from 2026-10-14, 3 runs on c7i-flex.large, not live" in resp.text
        assert "SIMULATED DATA" not in resp.text


@pytest.mark.asyncio
async def test_recorded_mode_banner_dynamic_range(monkeypatch, tmp_path):
    """Verify that date range and run count are derived dynamically from fallback bundle."""
    monkeypatch.setattr(config, "alloc_mode", "recorded")
    monkeypatch.setattr(config, "data_dir", str(tmp_path))
    custom_bundle = {
        "instance_type": "c7i-flex.large",
        "date_range": "2026-10-14 to 2026-10-21",
        "run_count": 125,
        "provenance": "MEASURED",
        "experiments": [],
    }
    fb_file = tmp_path / "recorded_fallback.json"
    import json
    fb_file.write_text(json.dumps(custom_bundle))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "RECORDED real measurements from 2026-10-14 to 2026-10-21, 125 runs on c7i-flex.large, not live" in resp.text


@pytest.mark.asyncio
async def test_live_mode_banner(monkeypatch):
    """Verify that no fallback or simulation banner appears in live mode."""
    monkeypatch.setattr(config, "alloc_mode", "live")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "RECORDED real measurements" not in resp.text
        assert "SIMULATED DATA" not in resp.text
