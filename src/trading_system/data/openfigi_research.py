"""Small unauthenticated identifier bridge; no temporal interval claims or writes."""

import re
import time

import requests

FIELDS = (
    "figi",
    "shareClassFIGI",
    "compositeFIGI",
    "ticker",
    "name",
    "securityType",
    "securityType2",
    "marketSector",
    "exchCode",
)


def parse_openfigi(jobs, payload):
    if not isinstance(payload, list) or len(payload) != len(jobs):
        raise ValueError("OPENFIGI_RESEARCH_INVALID_RESPONSE")
    results = []
    for job, result in zip(jobs, payload, strict=True):
        if not isinstance(result, dict) or not isinstance(result.get("data", []), list):
            raise ValueError("OPENFIGI_RESEARCH_INVALID_RESPONSE")
        results.append(
            {
                "query": job,
                "mappings": [
                    {k: r[k] for k in FIELDS if k in r}
                    for r in result.get("data", [])
                    if isinstance(r, dict)
                ],
                "outcome": "MAPPED"
                if result.get("data")
                else "PROVIDER_ERROR"
                if "error" in result
                else "NO_IDENTIFIER_FOUND",
                "interval_proof": False,
            }
        )
    return results


class OpenFigiResearchClient:
    """At most five jobs/request and eight requests/run; <=20 requests/minute.

    Public docs disagree on 5 versus 10 jobs for unauthenticated mapping; use 5.
    No key, no automatic retry, no pagination. 429 is reported, never bypassed.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.requests = 0
        self._last = None

    def mapping(self, jobs):
        if not 1 <= len(jobs) <= 5 or self.requests >= 8:
            raise ValueError("OPENFIGI_RESEARCH_SCOPE_REFUSED")
        for job in jobs:
            if (
                set(job) - {"idType", "idValue", "exchCode", "includeUnlistedEquities"}
                or job.get("idType") not in {"ID_CUSIP", "TICKER"}
                or not isinstance(job.get("idValue"), str)
                or not re.fullmatch(r"[A-Z0-9.]{1,16}", job["idValue"])
                or (
                    job["idType"] == "ID_CUSIP" and not re.fullmatch(r"[A-Z0-9]{9}", job["idValue"])
                )
                or job.get("exchCode") != "US"
                or type(job.get("includeUnlistedEquities", False)) is not bool
            ):
                raise ValueError("OPENFIGI_RESEARCH_JOB_REFUSED")
        if self._last is not None:
            time.sleep(max(0, 3.1 - (time.monotonic() - self._last)))
        self._last = time.monotonic()
        self.requests += 1
        try:
            response = self.session.post(
                "https://api.openfigi.com/v3/mapping",
                json=jobs,
                timeout=(10, 20),
                allow_redirects=False,
            )
            self._last = time.monotonic()
            try:
                if response.status_code != 200:
                    return {"http_status": response.status_code, "results": [], "queries": jobs}
                if len(response.content) > 2_000_000:
                    raise ValueError("OPENFIGI_RESEARCH_RESPONSE_TOO_LARGE")
                return {"http_status": 200, "results": parse_openfigi(jobs, response.json())}
            finally:
                response.close()
        except (requests.RequestException, ValueError) as exc:
            return {
                "http_status": None,
                "results": [],
                "queries": jobs,
                "error": "NETWORK_FAILED"
                if isinstance(exc, requests.RequestException)
                else "INVALID_RESPONSE",
            }

    def close(self):
        self.session.close()
