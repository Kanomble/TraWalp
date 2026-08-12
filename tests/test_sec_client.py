import pytest
import requests

from trading_system.data.sec_client import SecClient


class Response:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self.payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def json(self) -> dict:
        return self.payload


class Session:
    def __init__(self, responses: list[Response]) -> None:
        self.headers = {}
        self.responses = iter(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs) -> Response:
        self.calls += 1
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

    with pytest.raises(requests.HTTPError) as error:
        client.company_facts(1)

    assert error.value.response.status_code == 404
    assert session.calls == 1
    assert sleeps == []
