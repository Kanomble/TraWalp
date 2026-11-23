"""Small offline checks for the fixed historical market probe."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reports"))
import f01_historical_market_provider_probe as audit  # noqa: E402


def test_offline_is_default_and_network_requires_both_flags(monkeypatch, capsys):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Provider path reached")

    monkeypatch.setattr(audit, "run_probes", forbidden)
    audit.main([])
    assert json.loads(capsys.readouterr().out)["account_probe"] == "NOT_RUN"
    for args in (["--probe", "alpaca"], ["--authorize-provider-network"]):
        with pytest.raises(SystemExit, match="2"):
            audit.main(args)


def test_fixed_plan_has_bounded_windows_and_never_follows_pagination():
    plan = audit.probe_plan()
    assert sum(p.provider == "alpaca" for p in plan) == 25
    assert sum(p.provider == "massive" for p in plan) == 14
    assert all("apiKey" not in p.query and "page_token" not in p.query for p in plan)
    assert {p.origin for p in plan} == set(audit.ORIGINS)
    result = audit.analyze(plan[0], 200, b'{"next_page_token":"ignored"}')
    assert result["classification"] == "PAGINATION_REQUIRED_NOT_FOLLOWED"
    assert "ignored" not in str(result)


def test_bar_validation_keeps_holidays_missing_and_rejects_duplicates():
    probe = next(
        p
        for p in audit.probe_plan()
        if p.symbol == "SPY" and "2019-12-24" in p.query.get("start", "")
    )
    row = {"t": "2019-12-24T05:00:00Z", "o": 3, "h": 4, "l": 2, "c": 3, "v": 5}
    result = audit.analyze(probe, 200, audit.encode({"symbol": "SPY", "bars": [row]}))
    assert result["classification"] == "PARTIAL_SAMPLE"
    assert "2019-12-25" not in result["expected_sessions"]
    result = audit.analyze(probe, 200, audit.encode({"symbol": "SPY", "bars": [row, row]}))
    assert result["rows_usable"] == result["invalid_rows"] == 1
    assert result["classification"] == "INVALID_OR_DUPLICATE_BAR"
    assert audit.analyze(probe, 404, b"{}")["classification"] == "NO_PROVIDER_OBSERVATION"


def test_transport_does_not_redirect_leak_or_retry_after_429(monkeypatch):
    supplied = []
    secret = "synthetic-credential-only"
    local = [p for p in audit.probe_plan() if p.origin == "alpaca_paper"]

    def stub_get(self, url, **kwargs):
        assert url.startswith(audit.ORIGINS["alpaca_paper"])
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        supplied.append(kwargs["headers"])
        response = requests.Response()
        response.status_code = 200 if len(supplied) == 1 else 429
        response._content_consumed = True
        response._content = audit.encode(
            {"symbol": "ACN", "id": secret} if len(supplied) == 1 else {"error": secret}
        )
        return response

    monkeypatch.setenv("ALPACA_API_KEY", secret)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "synthetic-secret-value")
    monkeypatch.setattr(requests.Session, "get", stub_get)
    result = audit.run_probes("alpaca", local, None)
    assert [r["classification"] for r in result] == [
        "POINT_OBSERVATION",
        "RATE_LIMITED",
        "PROBE_STOPPED",
    ]
    assert len(supplied) == 2
    assert secret not in str(result) and "[REDACTED]" in str(result)


def test_massive_limiter_failure_preserves_safe_detail_and_stops_before_transport(monkeypatch):
    from trading_system.data.massive_rate_limit import MassiveReferenceError
    from trading_system.data.massive_reference import _REQUEST_LIMITER

    monkeypatch.setenv("MASSIVE_API_KEY", "test-fixture-only")
    plan = [p for p in audit.probe_plan() if p.provider == "massive"][:2]

    def forbid_network(*args, **kwargs):
        pytest.fail("Network unexpectedly reached")

    monkeypatch.setattr(requests.Session, "get", forbid_network)
    for source, expected in (
        ("MASSIVE_RATE_LIMIT_CLOCK_ANOMALY", "MASSIVE_RATE_LIMIT_CLOCK_ANOMALY"),
        ("unsafe provider content: test-fixture-only", "UNRECOGNIZED_LIMITER_ERROR"),
    ):

        def fail(operation, *, limit, source=source):
            assert callable(operation) and limit == 5
            raise MassiveReferenceError(source)

        monkeypatch.setattr(_REQUEST_LIMITER, "transmit", fail)
        first, second = audit.run_probes("massive", plan, None)
        assert first["classification"] == "LOCAL_RATE_LIMITER_ERROR"
        assert first["limiter_error"] == expected
        assert first["transport_started"] is False and "http_status" not in first
        assert second["classification"] == "PROBE_STOPPED"


def test_massive_network_error_and_http_auth_remain_distinct(monkeypatch):
    from trading_system.data.massive_reference import _REQUEST_LIMITER

    monkeypatch.setenv("MASSIVE_API_KEY", "test-fixture-only")
    monkeypatch.setattr(_REQUEST_LIMITER, "transmit", lambda operation, **kwargs: operation())
    calls = []

    def stub_get(self, url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise requests.ConnectionError("synthetic")
        response = requests.Response()
        response.status_code = 401
        response._content_consumed = True
        response._content = b"{}"
        return response

    monkeypatch.setattr(requests.Session, "get", stub_get)
    probes = [p for p in audit.probe_plan() if p.provider == "massive"][:3]
    first, second, third = audit.run_probes("massive", probes, None)
    assert first["classification"] == "NETWORK_ERROR" and "http_status" not in first
    assert second["classification"] == "AUTH_FAILED" and second["http_status"] == 401
    assert third["classification"] == "PROBE_STOPPED" and len(calls) == 2


def test_capacity_uses_complete_same_variant_samples_and_never_mixes_feeds():
    samples = []
    for probe in audit.probe_plan():
        if (
            probe.symbol in {"AAPL", "SPY"}
            and probe.query.get("feed") == "sip"
            and probe.query.get("adjustment") == "raw"
        ):
            samples.append(asdict(probe))
    results = [
        {
            "probe": probe,
            "classification": "SAMPLE_COMPLETE",
            "rows_usable": 5,
            "decoded_body_bytes": 500,
            "normalized_jsonl_bytes": 300,
            "normalized_gzip_bytes": 175,
        }
        for probe in samples
    ]
    results.append(
        results[0] | {"probe": samples[0] | {"query": samples[0]["query"] | {"feed": "iex"}}}
    )
    estimate = audit.capacity(results)
    [scenario] = estimate["by_feed_and_adjustment"]
    assert scenario["feed"] == "sip"
    assert scenario["projected_bytes"]["normalized_gzip_bytes"] == scenario["projected_rows"] * 35
