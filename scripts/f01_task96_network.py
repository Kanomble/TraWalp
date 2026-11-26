"""Bound SEC and Alpaca acquisitions. No transport is constructed by offline commands."""

import csv
import json
import os
import shutil
import tempfile
import time
import urllib.error
from datetime import UTC, date, datetime, timedelta
from datetime import time as daytime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import run_f01_historical_50_manual as task9
from f01_alpaca_historical_sip_coverage import populate
from f01_historical_50_preflight_run import NY, valid_bar
from f01_readiness_inventory import FORWARD_DAILY, digest, read_database
from f01_task96_contract import (
    CONTRACT,
    GIB,
    JOBS,
    MARKET,
    PARENT,
    REPO,
    allocation,
    current_code,
    key,
    publish,
    require,
)
from f01_task96_evidence import cache_root

from trading_system.data.canonical_facts import fingerprint
from trading_system.data.market_sessions import trading_sessions_between
from trading_system.data.sec_sync_persistence import stage_lock

SEC = task9.filing
SPY_ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
PAGE_BYTES = 8 * 1024**2


def known_spy(plan):
    # Credit the same variant already inventoried by Task 9.5. No legacy feed inference.
    manifest = plan.inputs.json(PARENT / "manifest.json")
    path = PARENT / "market_coverage.csv"
    task9.bulk.plain(path)
    sha = manifest["outputs"]["market_coverage.csv"]
    require(digest(path) == sha, "PUBLISHED_MARKET_COVERAGE_HASH_MISMATCH")
    plan.inputs.files[str(path)] = {"sha256": sha, "bytes": path.stat().st_size}
    with path.open(encoding="utf-8", newline="") as stream:
        return {
            r["session"]
            for r in csv.DictReader(stream)
            if (r["symbol"], r["provider"], r["feed"], r["adjustment"], r["research_usable"])
            == ("SPY", "alpaca", "sip", "all", "True")
        }


def current_spy(plan, start, end):
    legacy = REPO / "data/trading_system.sqlite3"
    with read_database(legacy, plan.inputs.files) as db:
        seen, valid, invalid, scope_sha = populate(
            db.execute(FORWARD_DAILY, ("SPY", "alpaca", "sip", "all", start, end))
        )
    plan.inputs.files[f"forward_spy/{start}/{end}"] = {
        "sha256": scope_sha,
        "seen": len(seen),
        "invalid": sum(invalid.values()),
    }
    return valid


def trading_days(start, end):
    return [
        d.isoformat()
        for d in trading_sessions_between(date.fromisoformat(start), date.fromisoformat(end))
    ]


def missing_windows(days, reusable):
    result = []
    for day in days:
        if day in reusable:
            if result and result[-1]:
                result.append([])
        else:
            if not result:
                result.append([])
            result[-1].append(day)
    return [window for window in result if window]


def scope_for(plan, block_id, current=None, *, spy=None):
    require(block_id in plan.blocks, "BLOCK_NOT_IN_BOUND_PLAN")
    provider, block = plan.blocks[block_id]
    status = "AWAITING_EXPLICIT_PROVIDER_AND_BATCH_AUTHORIZATION"
    info = {"block": block}
    if provider == "SEC":
        metadata, unresolved = plan.metadata(block["accessions"])
        rows = [metadata[key(r)] for r in block["accessions"] if key(r) in metadata]
        blocked = [
            {"cik": p[0], "accession": p[1], "reason": reason}
            for p, reason in sorted(unresolved.items())
        ]
        eligible, reused = [], []
        for row in rows:
            pair = key(row)
            item = (current or {}).get(pair)
            if item and item["status"] != "ORIGINAL_MISSING":
                reused.append({"cik": pair[0], "accession": pair[1], "status": item["status"]})
            else:
                eligible.append(row)
        if not eligible:
            status = "NO_DOWNLOAD_REQUIRED_OR_ALL_FILINGS_BLOCKED"
        info |= {
            "network_filing_scope": eligible,
            "local_reuse_or_review": reused,
            "metadata_blockers": blocked,
        }
        count = len(eligible)
    elif provider == "SPY":
        days = trading_days(block["start"], block["end"])
        require(block["sessions"] == len(days), "SPY_PLAN_SESSION_MISMATCH")
        reusable = set(days) & (known_spy(plan) if spy is None else spy)
        windows = missing_windows(days, reusable)
        if block["provider"] != "ALPACA_SIP" or block["start"] < "2016-01-01":
            status = "BLOCKED_PRE2016_PROVIDER_UNRESOLVED"
        elif not windows:
            status = "NO_DOWNLOAD_REQUIRED"
        info |= {
            "network_session_windows": windows,
            "reusable_sessions": sorted(reusable),
            "product": "Alpaca US Stocks Historical Data SIP/all 1Day",
        }
        count = len(windows)
    else:
        status, count = "BLOCKED_SECURITY_EPOCH_AND_CORPORATE_ACTION_EVIDENCE_REQUIRED", 0
    information = {
        "contract": CONTRACT,
        "parent": plan.binding(),
        "code_fingerprint": current_code(),
        "block_id": block_id,
        "provider": provider,
        "status": status,
        **info,
    }
    return information | {
        "authorization_draft": {
            "contract": CONTRACT,
            "scope": fingerprint(information),
            "decision": "PENDING",
            "authorized_by": "",
            "authorized_at": "",
            "provider_terms_evidence": "",
            "retention_rights_evidence": "",
            "backup_restore_evidence": "",
            "limits": allocation(provider, count),
        }
    }


class Budget:
    """Journal attempts before transport and charge the reserved cap after interruption."""

    def __init__(self, path, binding, limits, *, clock=time.time, sleep=time.sleep):
        self.path, self.clock, self.sleep = path, clock, sleep
        self.limits = limits
        self.state = {
            "binding": binding,
            "requests": 0,
            "received_bytes": 0,
            "uncertain_bytes": 0,
            "reserved": 0,
            "next_at": 0.0,
            "statuses": {},
            "limiter_blocked": False,
        }
        if path.is_file():
            _, state = task9.checked(path)
            require(state["binding"] == binding, "DOWNLOAD_CHECKPOINT_BINDING_CHANGED")
            self.state = state
        if self.state["reserved"]:
            self.state["uncertain_bytes"] += self.state["reserved"]
            self.state["reserved"] = 0
            self.save()

    def save(self):
        if task9.bulk.exists(self.path):
            task9.bulk.plain(self.path)
        with tempfile.TemporaryDirectory(prefix=".budget-", dir=self.path.parent) as tmp:
            stage = Path(tmp) / "checkpoint.json"
            task9.bulk.write_new(stage, task9.d6.json_bytes(self.state))
            os.replace(stage, self.path)

    def reserve(self, cap):
        state = self.state
        require(not state["reserved"], "UNACCOUNTED_REQUEST")
        require(not state["limiter_blocked"], "PROVIDER_RATE_LIMIT_MANUAL_REVIEW_REQUIRED")
        require(state["requests"] < self.limits["max_requests"], "HTTP_REQUEST_BUDGET_EXHAUSTED")
        remaining = self.limits["max_bytes"] - state["received_bytes"] - state["uncertain_bytes"]
        require(cap <= remaining, "HTTP_BYTE_BUDGET_EXHAUSTED")
        delay = max(0.0, state["next_at"] - self.clock())
        require(delay <= 60, "RATE_LIMIT_HOLD_ACTIVE")
        self.sleep(delay)
        state["requests"] += 1
        state["reserved"] = cap
        state["next_at"] = self.clock() + max(1, 60 / self.limits["requests_per_minute"])
        self.save()

    def finish(self, size, status, *, retry_after=0):
        state = self.state
        require(0 <= size <= state["reserved"], "HTTP_RESPONSE_BUDGET_EXCEEDED")
        state["received_bytes"] += size
        state["reserved"] = 0
        state["next_at"] = max(state["next_at"], self.clock() + retry_after)
        token = str(status)
        state["statuses"][token] = state["statuses"].get(token, 0) + 1
        self.save()


class CountedResponse:
    def __init__(self, response, budget):
        self.response, self.budget, self.bytes = response, budget, 0
        self.status, self.headers = response.status, response.headers

    def read(self, size):
        remaining = self.budget.state["reserved"] - self.bytes
        data = self.response.read(min(size, remaining + 1))
        self.bytes += len(data)
        require(self.bytes <= self.budget.state["reserved"], "SEC_RESPONSE_TOO_LARGE")
        return data

    def close(self):
        try:
            self.response.close()
        finally:
            self.budget.finish(min(self.bytes, self.budget.state["reserved"]), self.status)


class CountedSecOpener:
    def __init__(self, opener, budget):
        self.opener, self.budget = opener, budget

    def open(self, request, *, timeout):
        self.budget.reserve(SEC.LIMIT)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            self.budget.finish(0, exc.code)
            raise
        except task9.bulk.TRANSPORT_ERRORS:
            self.budget.finish(0, "TRANSPORT_FAILED")
            raise
        else:
            return CountedResponse(response, self.budget)


def verify_sec_listing(index, row):
    name = index["directory"]["name"]
    require(
        isinstance(name, str) and name.rstrip("/") == urlsplit(SEC.filing_base(row)).path,
        "SEC_LISTING_CIK_OR_ACCESSION_MISMATCH",
    )


def download_sec_filing(client, root, row):
    """Download or resume one filing through the existing SEC document cache."""
    from f01_task96_evidence import checked_original

    listing, source = SEC.get_document(client, root, row, "index.json")
    index = json.loads(listing)
    verify_sec_listing(index, row)
    names = dict.fromkeys(SEC.documents(row, index) + SEC.taxonomy_files(index))
    require(len(names) <= task9.DOCUMENTS_PER_FILING - 1, "SEC_DOCUMENT_COUNT_LIMIT")
    documents = [source]
    for name in names:
        _, item = SEC.get_document(client, root, row, name)
        documents.append(item)
    return checked_original(root.joinpath(*key(row)), row, documents)


def sec_download(job, scope, budget, user_agent):
    from f01_task96_evidence import checked_original

    filings = scope["network_filing_scope"]
    root = SEC.safe_directory(job / "originals")
    statuses = {}
    path = job / "checkpoint.json"
    if path.is_file():
        _, statuses = task9.checked(path)
    with task9.network(user_agent) as client:
        client.opener = CountedSecOpener(client.opener, budget)
        for row in filings:
            cik, accession = key(row)
            filing_id = "/".join((cik, accession))
            folder = root / cik / accession
            original_elsewhere = any(
                p.is_dir()
                for p in (
                    cache_root / cik / accession,
                    *(
                        j / "originals" / cik / accession
                        for j in task9.ARTIFACTS.glob("f01-task9-*")
                    ),
                    *(
                        j / "originals" / cik / accession
                        for j in JOBS.iterdir()
                        if j != job and j.is_dir()
                    ),
                )
            )
            if filing_id in statuses:
                checked_original(folder, row, statuses[filing_id]["documents"])
                continue
            if original_elsewhere:
                statuses.setdefault("_withheld", {})[filing_id] = "LOCAL_EVIDENCE_RECHECK_REQUIRED"
                continue
            try:
                statuses[filing_id] = download_sec_filing(client, root, row)
            except (ValueError, KeyError, TypeError) as exc:
                failure = {
                    "status": "SEC_FILING_BLOCKED",
                    "filing": filing_id,
                    "reason": str(exc),
                    "budget": budget.state,
                }
                publish(job / f"quarantine-{fingerprint(failure)}.json", failure)
                raise
            finally:
                replace_checkpoint(path, statuses)
    return {
        "status": "SEC_BLOCK_COMPLETE" if "_withheld" not in statuses else "SEC_BLOCK_PARTIAL",
        "verified_filings": {k: v for k, v in statuses.items() if k != "_withheld"},
        "withheld_filings": statuses.get("_withheld", {}),
        "bytes_archived": sum(
            d["bytes"] for k, v in statuses.items() if k != "_withheld" for d in v["documents"]
        ),
    }


def replace_checkpoint(path, obj):
    if task9.bulk.exists(path):
        task9.bulk.plain(path)
    with tempfile.TemporaryDirectory(prefix=".checkpoint-", dir=path.parent) as temp:
        stage = Path(temp) / "checkpoint.json"
        task9.bulk.write_new(stage, task9.d6.json_bytes(obj))
        os.replace(stage, path)


def spy_query(window, token):
    start, end = map(date.fromisoformat, (window[0], window[-1]))
    bounds = {
        "start": datetime.combine(start, daytime.min, NY).astimezone(UTC).isoformat(),
        "end": (
            datetime.combine(end + timedelta(days=1), daytime.min, NY).astimezone(UTC)
            - timedelta(microseconds=1)
        ).isoformat(),
    }
    return {
        "symbols": "SPY",
        "feed": "sip",
        "adjustment": "all",
        "timeframe": "1Day",
        "sort": "asc",
        "limit": 1000,
        **bounds,
        **({"page_token": token} if token else {}),
    }


def parse_spy(raw, days, prior):
    obj = json.loads(raw, parse_float=Decimal)
    bars = obj["bars"] or {}
    require(isinstance(bars, dict) and set(bars) <= {"SPY"}, "ALPACA_RESPONSE_SYMBOL_MISMATCH")
    parsed = {}
    for row in bars.get("SPY", []):
        stamp = datetime.fromisoformat(row["t"])
        require(stamp.utcoffset() is not None, "ALPACA_UNZONED_TIMESTAMP")
        local = stamp.astimezone(NY)
        day = local.date().isoformat()
        values = [row[k] for k in ("o", "h", "l", "c", "v")]
        require(
            day in days
            and day not in prior
            and day not in parsed
            and local.time() == daytime.min
            and valid_bar((row["t"], *values)),
            f"ALPACA_DUPLICATE_INVALID_OR_OUT_OF_SCOPE_SESSION: {day}",
        )
        parsed[day] = row
    more = obj.get("next_page_token")
    require(
        more is None
        or (
            isinstance(more, str)
            and 0 < len(more) <= 4096
            and more.isascii()
            and not any(c.isspace() for c in more)
        ),
        "INVALID_ALPACA_CONTINUATION_TOKEN",
    )
    return parsed, more


def alpaca_fetch(sdk, query, budget):
    """Use the existing Alpaca SDK session/auth without its merging paginator."""
    import requests

    for attempt in range(3):
        budget.reserve(PAGE_BYTES)
        try:
            with sdk._session.get(
                SPY_ENDPOINT,
                params=query,
                headers=sdk._get_default_headers(),
                timeout=30,
                allow_redirects=False,
                stream=True,
            ) as response:
                status = response.status_code
                if status == 200:
                    data = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        data.extend(chunk)
                        if len(data) > PAGE_BYTES:
                            budget.finish(PAGE_BYTES, "OVERSIZE")
                            raise ValueError("ALPACA_PAGE_EXCEEDS_BYTE_BUDGET")
                    budget.finish(len(data), status)
                    return bytes(data)
                hint = response.headers.get("Retry-After")
                if status == 429 and not hint:
                    budget.state["limiter_blocked"] = True
                    budget.finish(0, status)
                    raise ValueError("ALPACA_429_UNVERIFIED_RETRY_AFTER")
                retryable = status in task9.bulk.RETRYABLE
                delay = 0
                if retryable:
                    try:
                        delay = task9.bulk.retry_delay(hint, status, attempt)
                    except ValueError:
                        # Preserve an unresolved hold in the persisted checkpoint.
                        budget.finish(0, status, retry_after=3600)
                        raise
                budget.finish(0, status, retry_after=delay)
                require(retryable and attempt < 2, f"ALPACA_HTTP_{status}")
        except requests.RequestException:
            # Partial/ambiguous wire delivery stays conservatively charged at the full cap.
            budget.finish(PAGE_BYTES, "TRANSPORT_FAILED", retry_after=2**attempt)
            require(attempt < 2, "ALPACA_TRANSPORT_FAILED")
    raise AssertionError("unreachable")


def spy_download(job, scope, fetch):
    seen, requests = {}, 0
    requested = sorted(day for win in scope["network_session_windows"] for day in win)
    for i, window in enumerate(scope["network_session_windows"]):
        token, visited, page = None, set(), 0
        while True:
            query = spy_query(window, token)
            archive = job / f"window-{i:03d}-page-{page:04d}"
            if archive.is_dir():
                task9.bulk.directory(archive)
                _, receipt = task9.checked(archive / "receipt.json")
                raw_path = archive / "raw.json"
                require(
                    task9.bulk.plain(raw_path).st_size == receipt["bytes"]
                    and receipt["query"] == query
                    and digest(raw_path) == receipt["sha256"],
                    "ALPACA_PAGE_CHECKPOINT_MISMATCH",
                )
                raw = raw_path.read_bytes()
                parsed, token = parse_spy(raw, window, seen)
            else:
                raw = fetch(query)
                try:
                    parsed, token = parse_spy(raw, window, seen)
                except (ValueError, KeyError, TypeError) as exc:
                    failure = {
                        "status": "QUARANTINED",
                        "query": query,
                        "reason": str(exc),
                        "sha256": task9.sha(raw),
                    }
                    save_page(job, job / ("quarantine-" + fingerprint(failure)), raw, failure)
                    raise
                receipt = {
                    "query": query,
                    "endpoint": SPY_ENDPOINT,
                    "sha256": task9.sha(raw),
                    "bytes": len(raw),
                    "retrieved_utc": datetime.now(UTC).isoformat(),
                }
                save_page(job, archive, raw, receipt)
            requests += 1
            seen.update(parsed)
            if token is None:
                break
            require(token not in visited, "ALPACA_PAGINATION_TOKEN_LOOP")
            visited.add(token)
            page += 1
    return {
        "status": "SPY_BLOCK_COMPLETE" if len(seen) == len(requested) else "SPY_BLOCK_PARTIAL",
        "requested_sessions": len(requested),
        "sessions_verified": sorted(seen),
        "still_missing": sorted(set(requested) - seen.keys()),
        "response_pages": requests,
        "feed": "sip",
        "adjustment": "all",
        "pit_trading_qualified": False,
        "bytes_archived": sum((p / "raw.json").stat().st_size for p in job.glob("window-*-page-*")),
    }


def save_page(job, target, raw, receipt):
    if target.exists():
        return
    with tempfile.TemporaryDirectory(prefix=".page-", dir=job) as temp:
        stage = Path(temp)
        task9.bulk.write_new(stage / "raw.json", raw)
        publish(stage / "receipt.json", receipt)
        stage.rename(target)


def spy_execution_scope(job, authorized, reuse):
    path = job / "execution-sessions.json"
    days = [d for w in authorized["network_session_windows"] for d in w]
    parent = authorized["authorization_draft"]["scope"]
    if path.exists():
        _, record = task9.checked(path)
    else:
        available = set(reuse())
        remaining = [
            piece
            for w in authorized["network_session_windows"]
            for piece in missing_windows(w, available)
        ]
        record = {
            "scope": parent,
            "network_session_windows": remaining,
            "locally_reused_after_approval": sorted(set(days) & available),
        }
        publish(path, record)
    flattened = [d for w in record["network_session_windows"] for d in w]
    reused = record["locally_reused_after_approval"]
    require(
        record["scope"] == parent
        and sorted(flattened + reused) == sorted(days)
        and set(flattened).isdisjoint(reused)
        and all(
            w
            and w == trading_days(w[0], w[-1])
            and any(set(w) <= set(original) for original in authorized["network_session_windows"])
            for w in record["network_session_windows"]
        ),
        "SPY_EXECUTION_SESSION_SCOPE_CHANGED",
    )
    return authorized | record


def execute(scope, auth, auth_sha, *, user_agent=None, resume=False, spy_reuse=None):
    provider, block_id = scope["provider"], scope["block_id"]
    require(
        scope["status"] == "AWAITING_EXPLICIT_PROVIDER_AND_BATCH_AUTHORIZATION"
        and provider in {"SEC", "SPY"},
        f"BLOCKED_SCOPE: {scope['status']}",
    )
    root = JOBS if provider == "SEC" else MARKET
    root.parent.mkdir(exist_ok=True)
    task9.bulk.directory(root.parent)
    SEC.safe_directory(root)
    job = root / block_id
    require(job.exists() == resume, "USE_RESUME_FOR_EXISTING_BLOCK_OR_EXECUTE_FOR_NEW")
    if provider == "SEC":
        # Validate user agent before creating a job.
        task9.bulk.SecHttp(user_agent)
    else:
        from alpaca.data.historical import StockHistoricalDataClient

        from trading_system.config import load_settings

        sdk = StockHistoricalDataClient(
            *load_settings().require_alpaca_credentials(), raw_data=True
        )
    require(
        shutil.disk_usage(root).free >= auth["limits"]["max_bytes"] + 2 * GIB,
        "INSUFFICIENT_FREE_SPACE_FOR_BOUND_BLOCK",
    )
    SEC.safe_directory(job)
    with stage_lock(job / ".block.lock"):
        binding = {"scope": auth["scope"], "authorization_sha256": auth_sha}
        publish(job / "scope.json", scope)
        budget = Budget(job / "request-budget.json", binding, auth["limits"])
        if (job / "result.json").exists():
            _, result = task9.checked(job / "result.json")
            require(result["binding"] == binding, "RESULT_BINDING_CHANGED")
            return result
        try:
            if provider == "SEC":
                result = sec_download(job, scope, budget, user_agent)
            else:
                with stage_lock(root / ".alpaca-network.lock"):
                    bound = spy_execution_scope(job, scope, spy_reuse)
                    result = spy_download(job, bound, lambda q: alpaca_fetch(sdk, q, budget))
                    result["locally_reused_after_approval"] = bound["locally_reused_after_approval"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failure = {
                "status": "BLOCKED_RESUME",
                "binding": binding,
                "reason": str(exc),
                "progress": budget.state,
            }
            publish(job / f"blocked-{fingerprint(failure)}.json", failure)
            raise
        record = {
            "binding": binding,
            "block_id": block_id,
            "network_progress": budget.state,
            **result,
        }
        publish(job / "result.json", record)
        return record
