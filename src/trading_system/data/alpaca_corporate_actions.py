"""Bounded read-only NameChange requests through the installed alpaca-py SDK."""

import re

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import CorporateActionsType
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest

ENDPOINT = "https://data.alpaca.markets/v1/corporate-actions"


def lifecycle_scope(symbols, start, end, limit=100):
    symbols = sorted({s.strip().upper() for s in symbols})
    if not 1 <= len(symbols) <= 7 or any(
        not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,19}", s) for s in symbols
    ):
        raise ValueError("LIFECYCLE_REQUIRES_ONE_TO_SEVEN_EXPLICIT_SYMBOLS")
    if start > end or not 1 <= limit <= 1000:
        raise ValueError("LIFECYCLE_INVALID_RANGE_OR_LIMIT")
    return symbols


class _BoundedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def request(self, method, url, **kwargs):
        if method.upper() != "GET" or url != ENDPOINT or self.attempts >= 3:
            raise ValueError("LIFECYCLE_REQUEST_BOUND_EXCEEDED")
        self.attempts += 1
        kwargs.update(timeout=(5, 15), allow_redirects=False)
        return super().request(method, url, **kwargs)


class AlpacaCorporateActionClient:
    def __init__(self, api_key, secret_key):
        if not api_key or not secret_key:
            raise ValueError("LIFECYCLE_ALPACA_CREDENTIALS_MISSING")
        self.sdk = CorporateActionsClient(api_key, secret_key, raw_data=True)
        self.sdk._session.close()
        self.sdk._session = _BoundedSession()
        # SDK 0.44.0 otherwise retries 429 and supplies no transport timeout.
        self.sdk._retry = 0

    def name_changes(self, symbols, start, end, *, limit=100):
        symbols = lifecycle_scope(symbols, start, end, limit)
        self.sdk._session.attempts = 0
        try:
            result = self.sdk.get_corporate_actions(
                CorporateActionsRequest(
                    symbols=symbols,
                    types=[CorporateActionsType.NAME_CHANGE],
                    start=start,
                    end=end,
                    limit=limit,
                )
            )
        except APIError as exc:
            status = exc.status_code
            raise ValueError(f"LIFECYCLE_ALPACA_HTTP_{status or 'UNKNOWN'}") from None
        except requests.RequestException:
            # Never surface exception strings, response bodies or credential headers.
            raise ValueError("LIFECYCLE_ALPACA_NETWORK_FAILED") from None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            code = (
                "LIFECYCLE_REQUEST_BOUND_EXCEEDED"
                if exc.args == ("LIFECYCLE_REQUEST_BOUND_EXCEEDED",)
                else "LIFECYCLE_ALPACA_INVALID_RESPONSE"
            )
            raise ValueError(code) from None
        if not isinstance(result, dict) or any(
            not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            for rows in result.values()
        ):
            raise ValueError("LIFECYCLE_ALPACA_INVALID_RESPONSE")
        return result

    def close(self):
        self.sdk._session.close()
