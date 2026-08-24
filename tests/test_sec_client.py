import pytest
import requests

from trading_system.data.sec_client import SecClient, SecResourceNotFound


class Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self) -> dict:
        return self.payload

    @property
    def text(self) -> str:
        return str(self.payload.get("text", ""))


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.headers = {}
        self.responses = iter(responses)
        self.calls = 0
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs) -> Response:
        self.calls += 1
        self.urls.append(url)
        return next(self.responses)


def test_sec_client_retries_retryable_status_without_real_network() -> None:
    session = Session([Response(429, {}), Response(200, {"cik": 1})])
    sleeps: list[float] = []
    client = SecClient(
        "Researcher research@example.com",
        session=session,  # type: ignore[arg-type]
        sleeper=sleeps.append,
        request_interval_seconds=0.1,
    )
    assert client.company_facts(1) == {"cik": 1}
    assert session.calls == 2
    assert any(delay >= 1 for delay in sleeps)


def test_sec_client_does_not_retry_not_found_response() -> None:
    session = Session([Response(404, {})])
    sleeps: list[float] = []
    client = SecClient(
        "Researcher research@example.com",
        session=session,  # type: ignore[arg-type]
        sleeper=sleeps.append,
        request_interval_seconds=0,
    )

    with pytest.raises(SecResourceNotFound) as error:
        client.company_facts(1)

    assert error.value.resource_type == "companyfacts"
    assert error.value.cik == "0000000001"
    assert session.calls == 1
    assert sleeps == []
    assert client.request_counts == {"companyfacts": 1}


def test_not_found_distinguishes_submissions_resource() -> None:
    client = SecClient(
        "Researcher research@example.com",
        session=Session([Response(404, {})]),  # type: ignore[arg-type]
        request_interval_seconds=0,
    )

    with pytest.raises(SecResourceNotFound) as error:
        client.submissions(1234)

    assert error.value.resource_type == "submissions"
    assert error.value.cik == "0000001234"


def test_filing_index_uses_official_xbrl_current_and_archive_paths() -> None:
    session = Session([Response(200, {"text": "current"}), Response(200, {"text": "old"})])
    client = SecClient(
        "Researcher research@example.com",
        session=session,  # type: ignore[arg-type]
        request_interval_seconds=0,
    )

    assert client.filing_index(2026, 3, current=True) == "current"
    assert client.filing_index(2025, 4, current=False) == "old"
    assert session.urls == [
        "https://www.sec.gov/Archives/edgar/full-index/xbrl.idx",
        "https://www.sec.gov/Archives/edgar/full-index/2025/QTR4/xbrl.idx",
    ]


def test_daily_master_resources_use_shared_sec_transport_and_request_counters() -> None:
    directory = {"directory": {"item": [{"name": "master.20260821.idx"}]}}
    session = Session([Response(200, directory), Response(200, {"text": "daily master"})])
    client = SecClient(
        "Researcher research@example.com",
        session=session,  # type: ignore[arg-type]
        request_interval_seconds=0,
    )

    assert client.daily_master_index_directory(2026, 3) == directory
    assert client.daily_master_index(2026, 3, "20260821") == "daily master"
    assert session.urls == [
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/index.json",
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/master.20260821.idx",
    ]
    assert client.request_counts == {
        "daily_index_directory": 1,
        "daily_master_index": 1,
    }
