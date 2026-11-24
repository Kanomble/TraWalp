#!/usr/bin/env python3
"""Offline by default. Acquire a new immutable pair of official SEC Bulk ZIPs."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.dont_write_bytecode = True
from f01_research_evidence import REPO  # noqa: E402

from trading_system.data.canonical_bootstrap import BootstrapBlocked, _local_drive  # noqa: E402
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.sec_sync_persistence import stage_lock  # noqa: E402

URLS = {
    "companyfacts": "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip",
    "submissions": "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
}
MANIFEST_NAME = "f01_sec_bulk_source_manifest.json"
MANIFEST_CONTRACT = "F01_SEC_BULK_SOURCE_MANIFEST_V1"
CENSUS_NAME = "f01_sec_bulk_structure_census.json"
CENSUS_CONTRACT = "F01_SEC_BULK_STRUCTURE_CENSUS_V1"
REVIEW_CONTRACT = "F01_SEC_BULK_FINALIZATION_REVIEW_V1"
REVIEW_PATH = REPO / "docs" / "f01_sec_bulk_finalization_review.json"
REVIEWED_STATUS = "SEC_BULK_ARCHIVES_VERIFIED"
NON_DATA_MEMBER = "placeholder.txt"
NON_DATA_BYTES = b"Placeholder file"
CENSUS_CATEGORIES = (
    "VALID",
    "EMPTY_COMPANYFACTS",
    "MISSING_INTERNAL_CIK",
    "CIK_MISMATCH",
    "MISSING_FACTS",
    "EMPTY_FACTS",
    "INVALID_PAYLOAD",
    "INVALID_SUBMISSIONS",
    "UNKNOWN_STRUCTURE",
)
COMPANYFACTS_REVIEWABLE = {"EMPTY_COMPANYFACTS", "MISSING_INTERNAL_CIK", "EMPTY_FACTS"}
PLAN_CONTRACT = "F01_SEC_BULK_CAPACITY_V1"
CHECKPOINT_CONTRACT = "F01_SEC_BULK_PARTIAL_V1"
QUALITY = "CURRENT_PROVIDER_RECONSTRUCTION"
ROOTS = ("originals", "temporary", "research_db", "import_artifacts", "backup")
BUDGETS = (
    "pilot_database",
    "request_archives",
    "sqlite_sidecars",
    "import_index_work",
    "backup_generation",
    "restore_workspace",
    "safety_reserve",
)
BLOCK_SIZE = 1024**2
MAX_MEMBER_BYTES = 128 * 1024**2
MAX_METADATA_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_REVIEWED_CENSUS_BYTES = 64 * 1024**2
SHA256 = re.compile(r"[0-9a-f]{64}")
GENERATION = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
MEMBER_NAME = re.compile(r"CIK([0-9]{10})(?:-submissions-([0-9]{3,}))?\.json")
HTTP_FIELDS = ("ETag", "Last-Modified", "Date", "Content-Length", "Accept-Ranges", "Content-Type")
RETRYABLE = {429, 500, 502, 503, 504}
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)


class Blocked(ValueError):
    """A failed capacity, authorization, or evidence check."""


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def utc_now():
    return datetime.now(UTC).isoformat()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def plain(path, *, directory=False):
    info = path.lstat()
    linked = stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    proper_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    require(
        not linked and proper_type and (directory or info.st_nlink == 1),
        f"UNSAFE_PATH: {path}",
    )
    return info


def exists(path):
    return os.path.lexists(path)


def directory(path):
    require(
        path.is_absolute()
        and re.fullmatch(r"[A-Za-z]:", path.drive)
        and all(
            part != ".." and ":" not in part and not part.endswith((".", " "))
            for part in path.parts[1:]
        ),
        f"ABSOLUTE_LOCAL_PATH_REQUIRED: {path}",
    )
    try:
        _local_drive(path)
    except BootstrapBlocked as error:
        raise Blocked(f"FIXED_LOCAL_WINDOWS_VOLUME_REQUIRED: {path.anchor}") from error
    for component in reversed((path, *path.parents)):
        plain(component, directory=True)
    require(path.resolve(strict=True) == path, f"REDIRECTED_PATH: {path}")
    return path


def read_metadata(path, *, max_bytes=MAX_METADATA_BYTES):
    path = path.absolute()
    directory(path.parent)
    require(":" not in path.name and path.suffix.lower() == ".json", "JSON_METADATA_PATH_REQUIRED")
    info = plain(path)
    require(info.st_size <= max_bytes, f"OVERSIZED_METADATA: {path}")
    with path.open("rb") as stream:
        require(os.path.samestat(info, os.fstat(stream.fileno())), f"REPLACED_METADATA: {path}")
        raw = stream.read(max_bytes + 1)
    require(len(raw) <= max_bytes, f"OVERSIZED_METADATA: {path}")
    return raw, json.loads(raw)


@dataclass(frozen=True)
class Layout:
    paths: dict
    budget: dict

    @classmethod
    def load(cls, path):
        _, value = read_metadata(path)
        require(
            isinstance(value, dict) and set(value) == set(ROOTS) | {"budget_bytes"},
            "LAYOUT_FIELDS_INVALID",
        )
        require(all(isinstance(value[key], str) for key in ROOTS), "LAYOUT_PATH_INVALID")
        paths = {key: directory(Path(value[key])) for key in ROOTS}
        require(
            not any(
                a.is_relative_to(b) for x, a in paths.items() for y, b in paths.items() if x != y
            ),
            "LAYOUT_ROOTS_MUST_BE_DISTINCT_AND_NON_NESTED",
        )
        require(
            not any(
                path.is_relative_to(REPO / protected) or (REPO / protected).is_relative_to(path)
                for path in paths.values()
                for protected in ("data", "reports")
            ),
            "PROTECTED_REPOSITORY_PATH",
        )
        budget = value["budget_bytes"]
        require(isinstance(budget, dict) and set(budget) == set(BUDGETS), "BUDGET_FIELDS_INVALID")
        require(
            all(type(amount) is int and amount >= 0 for amount in budget.values())
            and budget["safety_reserve"] > 0,
            "BUDGET_REQUIRES_BYTE_COUNTS_AND_POSITIVE_RESERVE",
        )
        require(
            paths["originals"].stat().st_dev == paths["temporary"].stat().st_dev,
            "ORIGINALS_AND_TEMPORARY_MUST_SHARE_A_VOLUME",
        )
        return cls(paths, budget)


def capacity(layout, observations, *, already_staged=0):
    sizes = {name: observations.get(name, {}).get("bytes") for name in URLS}
    known = all(type(size) is int and size > 0 for size in sizes.values())
    total = sum(sizes.values()) if known else None
    allocations = {
        "originals": {},
        "temporary": {"remaining_download": total - already_staged if known else None},
        "research_db": {key: layout.budget[key] for key in BUDGETS[:3]},
        "import_artifacts": {
            key: layout.budget[key] for key in ("import_index_work", "restore_workspace")
        },
        "backup": {
            "backup_zip_pair": total,
            "backup_generation": layout.budget["backup_generation"],
        },
    }
    devices = {}
    for role, path in layout.paths.items():
        free = shutil.disk_usage(path).free
        volume = devices.setdefault(
            str(path.stat().st_dev),
            {
                "available_bytes": free,
                "paths": {},
                "allocations": {"safety_reserve": layout.budget["safety_reserve"]},
            },
        )
        volume["available_bytes"] = min(volume["available_bytes"], free)
        volume["paths"][role] = str(path)
        volume["allocations"].update(allocations[role])
    enough = known
    for volume in devices.values():
        amounts = volume["allocations"].values()
        required = sum(amounts) if all(amount is not None for amount in amounts) else None
        volume["required_additional_bytes"] = required
        volume["shortfall_bytes"] = (
            max(0, required - volume["available_bytes"]) if required is not None else None
        )
        enough = enough and volume["shortfall_bytes"] == 0
    return {
        "contract": PLAN_CONTRACT,
        "status": "CAPACITY_OK"
        if enough
        else "CAPACITY_BLOCKED"
        if known
        else "CAPACITY_UNDETERMINED",
        "observations": observations,
        "archive_bytes": sizes,
        "original_zip_total_bytes": total,
        "temporary_download_peak_bytes": total,
        "already_staged_bytes": already_staged,
        "volumes": devices,
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SecHttp:
    def __init__(self, user_agent, *, opener=None, pause=time.sleep, clock=time.monotonic):
        require(
            isinstance(user_agent, str)
            and user_agent.isascii()
            and len(user_agent) <= 256
            and bool(re.fullmatch(r"[ -~]+\s+[^ @]+@[^ @]+\.[^ @]+", user_agent)),
            "SEC_USER_AGENT_REQUIRES_OPERATOR_NAME_AND_CONTACT_EMAIL",
        )
        self.user_agent = user_agent
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.pause = pause
        self.clock = clock
        self.last_request = None

    def open(self, name, method, **headers):
        return self.open_url(URLS[name], method, **headers)

    def open_url(self, url, method, **headers):
        request = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity", **headers},
        )
        for attempt in range(3):
            if self.last_request is not None:
                self.pause(max(0, 1 - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            try:
                return self.opener.open(request, timeout=30)
            except urllib.error.HTTPError as error:
                with closing(error):
                    if error.code not in RETRYABLE or attempt == 2:
                        raise Blocked(f"SEC_HTTP_{error.code}: {url}") from error
                    wait = retry_delay(error.headers.get("Retry-After"), error.code, attempt)
            except TRANSPORT_ERRORS as error:
                if attempt == 2:
                    raise Blocked(f"SEC_TRANSPORT_FAILED: {url}") from error
                wait = 2**attempt
            self.pause(wait)
        raise AssertionError("unreachable")


def retry_delay(header, code, attempt):
    if header and re.fullmatch(r"[0-9]{1,9}", header):
        wait = int(header)
    elif header:
        try:
            wait = max(0, (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError) as error:
            raise Blocked("SEC_RETRY_AFTER_INVALID") from error
    else:
        require(code != 429, "SEC_RATE_LIMIT_WITHOUT_RETRY_AFTER")
        wait = 2**attempt
    require(wait <= 60, "SEC_RETRY_AFTER_EXCEEDS_WAIT_BUDGET")
    return wait


def response_metadata(response):
    require(
        len(response.headers.get_all("ETag", [])) <= 1,
        "SEC_HTTP_AMBIGUOUS_ETAG",
    )
    return {field: response.headers[field] for field in HTTP_FIELDS if field in response.headers}


def has_identity_encoding(response):
    return response.headers.get("Content-Encoding", "identity").lower() == "identity"


def response_length(response):
    values = response.headers.get_all("Content-Length", [])
    if len(values) == 1 and re.fullmatch(r"[0-9]{1,16}", values[0]):
        return int(values[0])
    return None


def collect_remote(http):
    observations = {}
    for name, url in URLS.items():
        with http.open(name, "HEAD") as response:
            require(
                response.status == 200
                and response.geturl() == url
                and has_identity_encoding(response)
                and response.headers.get("Content-Range") is None,
                f"SEC_HEAD_RESPONSE_UNUSABLE: {name}",
            )
            observations[name] = {
                "url": url,
                "observed_at_utc": utc_now(),
                "bytes": response_length(response) or None,
                "http": response_metadata(response),
            }
    return observations


def load_observations(path):
    if path is None:
        return {}
    _, report = read_metadata(path)
    require(
        isinstance(report, dict) and report.get("contract") == PLAN_CONTRACT, "SIZE_REPORT_INVALID"
    )
    observations = report.get("observations")
    require(
        isinstance(observations, dict) and set(observations) == set(URLS), "SIZE_REPORT_ENDPOINTS"
    )
    for name, item in observations.items():
        require(
            isinstance(item, dict)
            and item.get("url") == URLS[name]
            and (item.get("bytes") is None or type(item["bytes"]) is int and item["bytes"] > 0),
            "SIZE_REPORT_OBSERVATION_INVALID",
        )
    return observations


def validate_archive(path, name, expected_size, expected_sha):
    require(
        name in URLS and SHA256.fullmatch(expected_sha), "ARCHIVE_VERIFICATION_BINDING_REQUIRED"
    )
    info = plain(path)
    require(info.st_size == expected_size, f"ARCHIVE_SIZE_MISMATCH: {name}")
    with path.open("rb") as stream:
        require(os.path.samestat(info, os.fstat(stream.fileno())), f"ARCHIVE_REPLACED: {name}")
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == expected_sha,
            f"ARCHIVE_SHA256_MISMATCH: {name}",
        )
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                result = validate_members(archive, name)
        except (
            zipfile.BadZipFile,
            zlib.error,
            EOFError,
            NotImplementedError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise Blocked(f"INVALID_ZIP_OR_MEMBER_JSON: {name}: {error}") from error
        after = os.fstat(stream.fileno())
    require(
        os.path.samestat(info, plain(path))
        and (after.st_size, after.st_mtime_ns) == (info.st_size, info.st_mtime_ns),
        f"ARCHIVE_CHANGED_DURING_VALIDATION: {name}",
    )
    return {"status": "VERIFIED", **result}


def member_name(member, endpoint):
    match = MEMBER_NAME.fullmatch(member.orig_filename)
    return match if match and (not match[2] or endpoint == "submissions") else None


def readable_member(member):
    return (
        stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG)
        and not member.flag_bits & 1
        and 0 < member.file_size <= MAX_MEMBER_BYTES
    )


def read_zip_member(archive, member):
    with archive.open(member) as stream:
        # Reading to EOF verifies the CRC without extracting any file.
        raw = stream.read(member.file_size + 1)
    require(len(raw) == member.file_size, f"ZIP_MEMBER_SIZE_MISMATCH: {member.orig_filename}")
    return raw


def matches_cik(value, cik):
    value = str(value)
    return bool(re.fullmatch(r"[0-9]{1,10}", value)) and value.zfill(10) == cik


def submissions_filing_index(record):
    filings = record.get("filings")
    if (
        isinstance(filings, dict)
        and isinstance(filings.get("recent"), dict)
        and isinstance(filings.get("files"), list)
    ):
        return filings["files"]
    return None


def submission_reference(item, cik):
    ref = item.get("name") if isinstance(item, dict) else None
    if isinstance(ref, str) and re.fullmatch(rf"CIK{cik}-submissions-[0-9]{{3,}}\.json", ref):
        return ref
    return None


def validate_members(archive, name):
    unique, supplement_files, supplement_refs = set(), set(), set()
    empty_companyfacts = []
    issuers = 0
    for member in archive.infolist():
        filename = member.orig_filename
        require(
            MEMBER_NAME.fullmatch(filename) and filename not in unique and readable_member(member),
            f"UNSAFE_UNEXPECTED_OR_DUPLICATE_MEMBER: {filename!r}",
        )
        match = member_name(member, name)
        require(match, f"UNEXPECTED_MEMBER: {filename!r}")
        cik, historical = match.groups()
        unique.add(filename)
        record = json.loads(read_zip_member(archive, member))
        require(isinstance(record, dict), f"ZIP_MEMBER_OBJECT_REQUIRED: {filename}")
        if name == "companyfacts" and not record:
            empty_companyfacts.append(filename)
            continue
        if "cik" in record:
            require(
                matches_cik(record["cik"], cik),
                f"ZIP_MEMBER_CIK_MISMATCH: {filename}",
            )
        else:
            require(historical, f"ZIP_MEMBER_CIK_REQUIRED: {filename}")
        if historical:
            require(
                isinstance(record.get("accessionNumber"), list),
                f"INVALID_SUBMISSION_CHUNK: {filename}",
            )
            supplement_files.add(filename)
        else:
            issuers += 1
            if name == "companyfacts":
                require(isinstance(record.get("facts"), dict), f"INVALID_COMPANYFACTS: {filename}")
            else:
                files = submissions_filing_index(record)
                require(
                    files is not None,
                    f"INVALID_SUBMISSIONS: {filename}",
                )
                for item in files:
                    ref = submission_reference(item, cik)
                    require(
                        ref is not None and ref not in supplement_refs,
                        f"INVALID_SUBMISSION_CHUNK_REFERENCE: {filename}",
                    )
                    supplement_refs.add(ref)
        del record
    require(
        issuers + len(empty_companyfacts) > 0 and supplement_files == supplement_refs,
        "ARCHIVE_LAYOUT_OR_CHUNKS_INCOMPLETE",
    )
    result = {
        "members": len(unique),
        "issuers": issuers,
        "historical_submission_chunks": len(supplement_files),
    }
    if name == "companyfacts":
        result["empty_companyfacts"] = {
            "classification": "EMPTY_COMPANYFACTS",
            "count": len(empty_companyfacts),
            "members": sorted(empty_companyfacts),
            "import_eligible": False,
        }
    return result


def strong_etag(value):
    return isinstance(value, str) and bool(re.fullmatch(r'"[\x21\x23-\x7e]*"', value))


def write_new(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def save_checkpoint(path, checkpoint):
    next_path = path.with_suffix(".next")
    write_new(next_path, json_bytes(checkpoint))
    next_path.replace(path)


def read_checkpoint(path, checkpoint_path, name, *, check_file_size=True, expected_sha=None):
    require(exists(path) and exists(checkpoint_path), f"PARTIAL_STATE_AMBIGUOUS: {name}")
    raw, state = read_metadata(checkpoint_path)
    if expected_sha is not None:
        require(
            hashlib.sha256(raw).hexdigest() == expected_sha, f"CHECKPOINT_SHA256_MISMATCH: {name}"
        )
    require(isinstance(state, dict), f"PARTIAL_STATE_INVALID: {name}")
    count = state.get("received_bytes")
    total = state.get("archive_bytes")
    require(
        state.get("contract") == CHECKPOINT_CONTRACT
        and state.get("url") == URLS[name]
        and state.get("endpoint") == name
        and type(total) is int
        and total > 0
        and type(count) is int
        and 0 <= count <= total
        and (not check_file_size or plain(path).st_size == count)
        and type(state.get("complete")) is bool
        and isinstance(state.get("http"), dict)
        and isinstance(state.get("segments"), list)
        and isinstance(state.get("retrieval_started_at_utc"), str)
        and isinstance(state.get("retrieval_ended_at_utc"), str)
        and isinstance(state.get("prefix_sha256"), str)
        and SHA256.fullmatch(state["prefix_sha256"]),
        f"PARTIAL_STATE_INVALID: {name}",
    )
    return state


def resume_state(path, checkpoint_path, name, observation, *, resume):
    if not exists(path) and not exists(checkpoint_path):
        return None
    require(resume, f"PARTIAL_STATE_AMBIGUOUS: {name}")
    state = read_checkpoint(path, checkpoint_path, name)
    require(state["archive_bytes"] == observation["bytes"], f"REMOTE_GENERATION_CHANGED: {name}")
    etag = state["http"].get("ETag")
    current = observation["http"]
    require(
        strong_etag(etag) and current.get("ETag") == etag,
        f"REMOTE_GENERATION_CHANGED_OR_UNPROVEN: {name}",
    )
    if state["received_bytes"] < observation["bytes"]:
        require(
            current.get("Accept-Ranges", "").lower() == "bytes",
            f"REMOTE_RANGE_SUPPORT_UNPROVEN: {name}",
        )
    return state


def retrieve(http, name, observation, path, checkpoint_path, state, progress):
    total = observation["bytes"]
    count = state["received_bytes"] if state else 0
    digest = hashlib.sha256()
    if state:
        with path.open("rb") as stream:
            hashlib.file_digest(stream, lambda: digest)
        require(
            digest.hexdigest() == state.get("prefix_sha256"), f"PARTIAL_SHA256_MISMATCH: {name}"
        )
        if count == total:
            require(state.get("complete") is True, f"PARTIAL_COMPLETION_UNPROVEN: {name}")
            return state

    started, clock = utc_now(), time.monotonic()
    etag = observation["http"].get("ETag")
    headers = {"If-Match": etag} if strong_etag(etag) else {}
    if state:
        headers |= {"Range": f"bytes={count}-", "If-Range": etag}
    with http.open(name, "GET", **headers) as response:
        metadata = response_metadata(response)
        require(
            response.geturl() == URLS[name]
            and has_identity_encoding(response)
            and response.status == (206 if state else 200)
            and response_length(response) == total - count
            and response.headers.get("Content-Range")
            == (f"bytes {count}-{total - 1}/{total}" if state else None)
            and (etag is None or response.headers.get("ETag") == etag)
            and (
                etag is not None
                or observation["http"].get("Last-Modified") is None
                or response.headers.get("Last-Modified") == observation["http"]["Last-Modified"]
            ),
            f"GET_GENERATION_OR_RANGE_MISMATCH: {name}",
        )
        state = state or {
            "contract": CHECKPOINT_CONTRACT,
            "endpoint": name,
            "url": URLS[name],
            "archive_bytes": total,
            "retrieval_started_at_utc": started,
            "http": metadata,
            "segments": [],
        }
        initial = count
        last_progress = clock
        complete = False
        with path.open("ab" if headers.get("Range") else "xb") as output:
            require(output.tell() == count, f"PARTIAL_LENGTH_CHANGED: {name}")
            try:
                while block := response.read(BLOCK_SIZE):
                    require(count + len(block) <= total, f"HTTP_BODY_TOO_LARGE: {name}")
                    if output.write(block) != len(block):
                        raise OSError("Incomplete filesystem write")
                    digest.update(block)
                    count += len(block)
                    now = time.monotonic()
                    if now - last_progress >= 1 or count == total:
                        elapsed = max(now - clock, 0.001)
                        rate = (count - initial) / elapsed
                        eta = (total - count) / rate if rate else 0
                        progress(
                            f"{name}: {count}/{total} bytes | "
                            f"{rate / 1024**2:.2f} MiB/s | ETA {eta:.0f}s"
                        )
                        last_progress = now
                complete = count == total
                require(complete, f"HTTP_BODY_INTERRUPTED: {name}: {count}/{total}")
            finally:
                ended = utc_now()
                output.flush()
                os.fsync(output.fileno())
                state["segments"].append(
                    {
                        "start_utc": started,
                        "end_utc": ended,
                        "offset": initial,
                        "bytes": count - initial,
                        "http": metadata,
                    }
                )
                state.update(
                    received_bytes=count,
                    prefix_sha256=digest.hexdigest(),
                    retrieval_ended_at_utc=ended,
                    complete=complete,
                )
                save_checkpoint(checkpoint_path, state)
    return state


def source_manifest(generation, final, states, validations):
    entries = {}
    for name in URLS:
        state = states[name]
        entries[name] = {
            "endpoint": name,
            "url": URLS[name],
            "archive_generation": f"{generation}/{name}",
            "retrieval_started_at_utc": state["retrieval_started_at_utc"],
            "retrieval_ended_at_utc": state["retrieval_ended_at_utc"],
            "bytes": state["archive_bytes"],
            "sha256": state["prefix_sha256"],
            "http": state["http"],
            "segments": state["segments"],
            "local_path": str(final / f"{name}.zip"),
            "integrity": validations[name],
        }
    return {
        "contract": MANIFEST_CONTRACT,
        "generation": generation,
        "quality": QUALITY,
        "archives": entries,
        "shared_sec_generation_verified": False,
        "generation_note": (
            "Two independently observed SEC archives with no proven common upstream vintage."
        ),
    }


def staging_files(stage):
    publish = directory(stage / "publish")
    paths = {name: publish / f"{name}.zip" for name in URLS}
    checkpoints = {name: stage / f"{name}.checkpoint.json" for name in URLS}
    require(
        not any(exists(p.with_suffix(".next")) for p in checkpoints.values()),
        "INCOMPLETE_CHECKPOINT_UPDATE_REQUIRES_REVIEW",
    )
    return publish, paths, checkpoints


def publish_generation(generation, final, publish, paths, states, progress):
    validations = {}
    for name, path in paths.items():
        progress(f"Verifying SHA-256, ZIP member CRC and layout: {name}")
        validations[name] = validate_archive(
            path, name, states[name]["archive_bytes"], states[name]["prefix_sha256"]
        )
    manifest = source_manifest(generation, final, states, validations)
    return publish_source_manifest(final, publish, paths, manifest, status="PUBLISHED")


def publish_source_manifest(final, publish, paths, source, *, status):
    manifest = json_bytes(source)
    require(len(manifest) <= MAX_MANIFEST_BYTES, "SOURCE_MANIFEST_TOO_LARGE")
    manifest_path = publish / MANIFEST_NAME
    if exists(manifest_path):
        require(
            read_metadata(manifest_path, max_bytes=MAX_MANIFEST_BYTES)[0] == manifest,
            "STAGED_MANIFEST_CONFLICT",
        )
    else:
        write_new(manifest_path, manifest)
    require(set(publish.iterdir()) == {*paths.values(), manifest_path}, "UNEXPECTED_STAGING_FILE")
    for path in (*paths.values(), manifest_path):
        path.chmod(stat.S_IREAD)
    require(not exists(final), f"ORIGINAL_GENERATION_ALREADY_EXISTS: {final}")
    # Windows same-volume rename fails if a competing process created the destination.
    publish.rename(final)
    return {
        "status": status,
        "manifest": str(final / MANIFEST_NAME),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }


def complete_download_state(path, checkpoint, name, *, check_file_size=True, expected_sha=None):
    state = read_checkpoint(
        path, checkpoint, name, check_file_size=check_file_size, expected_sha=expected_sha
    )
    total = state["archive_bytes"]
    require(
        state["complete"] and state["received_bytes"] == total and state["segments"],
        f"OFFLINE_FINALIZATION_REQUIRES_COMPLETE_DOWNLOAD: {name}",
    )
    offset = 0
    for segment in state["segments"]:
        require(isinstance(segment, dict), f"INVALID_DOWNLOAD_SEGMENT: {name}")
        size = segment.get("bytes")
        require(
            type(segment.get("offset")) is int
            and segment["offset"] == offset
            and type(size) is int
            and 0 <= size <= total - offset
            and isinstance(segment.get("start_utc"), str)
            and isinstance(segment.get("end_utc"), str)
            and isinstance(segment.get("http"), dict),
            f"INVALID_DOWNLOAD_SEGMENT: {name}",
        )
        offset += size
    first, last = state["segments"][0], state["segments"][-1]
    require(
        offset == total
        and first["start_utc"] == state["retrieval_started_at_utc"]
        and last["end_utc"] == state["retrieval_ended_at_utc"]
        and first["http"] == state["http"],
        f"DOWNLOAD_SEGMENTS_NOT_BOUND_TO_CHECKPOINT: {name}",
    )
    return state


@dataclass(slots=True)
class CensusMember:
    name: str
    keys: tuple | None = None
    findings: tuple = ()

    def flag(self, category):
        if category not in self.findings:
            self.findings += (category,)


def census_payload(record, match, entry, endpoint, chunks, references):
    if not isinstance(record, dict):
        entry.flag("INVALID_PAYLOAD")
        return
    if match is None:
        return
    cik, historical = match.groups()
    if endpoint == "companyfacts" and not record:
        entry.flag("EMPTY_COMPANYFACTS")
        return
    if "cik" in record:
        if not matches_cik(record["cik"], cik):
            entry.flag("CIK_MISMATCH")
    elif not historical:
        entry.flag("MISSING_INTERNAL_CIK")

    if endpoint == "companyfacts":
        facts = record.get("facts")
        if "facts" not in record:
            entry.flag("MISSING_FACTS")
        elif not isinstance(facts, dict) or any(
            not isinstance(taxonomy, dict) for taxonomy in facts.values()
        ):
            entry.flag("INVALID_PAYLOAD")
        elif not facts:
            entry.flag("EMPTY_FACTS")
    elif historical:
        if not isinstance(record.get("accessionNumber"), list):
            entry.flag("INVALID_SUBMISSIONS")
        chunks[entry.name] = entry
    else:
        files = submissions_filing_index(record)
        if files is None:
            entry.flag("INVALID_SUBMISSIONS")
            return
        for item in files:
            ref = submission_reference(item, cik)
            if ref is None or ref in references:
                entry.flag("INVALID_SUBMISSIONS")
            else:
                references[ref] = entry


def census_zip(archive, endpoint, integrity_issues):
    infos = archive.infolist()
    names = Counter(info.orig_filename for info in infos)
    entries, key_sets, chunks, references = [], {}, {}, {}
    crc_verified = 0
    has_primary = False
    for info in infos:
        entry = CensusMember(info.orig_filename)
        entries.append(entry)
        match = member_name(info, endpoint)
        has_primary |= match is not None and match[2] is None
        if match is None or names[entry.name] > 1:
            reason = "UNSAFE_OR_UNEXPECTED_MEMBER" if match is None else "DUPLICATE_MEMBER"
            integrity_issues.append({"reason": reason, "member": entry.name})
            entry.flag("UNKNOWN_STRUCTURE")
        if not readable_member(info):
            integrity_issues.append(
                {"reason": "UNSUPPORTED_OR_OVERSIZE_MEMBER", "member": entry.name}
            )
            entry.flag("UNKNOWN_STRUCTURE")
            continue
        try:
            raw = read_zip_member(archive, info)
        except (
            Blocked,
            zipfile.BadZipFile,
            zlib.error,
            EOFError,
            OSError,
            NotImplementedError,
            RuntimeError,
        ) as error:
            integrity_issues.append(
                {"reason": "MEMBER_INTEGRITY_FAILED", "member": entry.name, "detail": str(error)}
            )
            entry.flag("INVALID_PAYLOAD")
            continue
        crc_verified += 1
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            entry.flag("INVALID_PAYLOAD")
        else:
            if isinstance(record, dict):
                keys = tuple(sorted(record))
                entry.keys = key_sets.setdefault(keys, keys)
            census_payload(record, match, entry, endpoint, chunks, references)
            del record
        finally:
            del raw
    if not has_primary:
        integrity_issues.append({"reason": "ARCHIVE_PRIMARY_MEMBERS_MISSING"})
    for ref, owner in references.items():
        if ref not in chunks or chunks[ref].findings:
            owner.flag("INVALID_SUBMISSIONS")
    for ref, chunk in chunks.items():
        if ref not in references or references[ref].findings:
            chunk.flag("INVALID_SUBMISSIONS")
    return entries, crc_verified


def census_categories(endpoint, entries, verified):
    groups = {category: {} for category in CENSUS_CATEGORIES}
    for entry in entries:
        for category in entry.findings or ("VALID",):
            groups[category].setdefault(entry.keys, []).append(entry.name)
    return {
        category: {
            "endpoint": endpoint,
            "count": sum(map(len, by_keys.values())),
            "import_eligible": category == "VALID" and verified,
            "disposition": (
                "QUARANTINED"
                if category != "VALID"
                else "STRUCTURALLY_ELIGIBLE"
                if verified
                else "BLOCKED_ARCHIVE_INTEGRITY"
            ),
            "key_groups": [{"keys": keys, "members": names} for keys, names in by_keys.items()],
        }
        for category, by_keys in groups.items()
    }


def census_archive(endpoint, path, checkpoint):
    issues = []
    integrity = {"issues": issues, "members_scanned": 0, "members_crc_verified": 0}
    entries = []
    if exists(checkpoint.with_suffix(".next")):
        issues.append({"reason": "INCOMPLETE_CHECKPOINT_UPDATE_REQUIRES_REVIEW"})
    try:
        state = complete_download_state(path, checkpoint, endpoint, check_file_size=False)
    except (Blocked, OSError, ValueError) as error:
        issues.append({"reason": "CHECKPOINT_BINDING_FAILED", "detail": str(error)})
        state = None
    try:
        info = plain(path)
        with pinned_files({endpoint: path}), path.open("rb") as stream:
            require(os.path.samestat(info, os.fstat(stream.fileno())), "ARCHIVE_REPLACED")
            integrity["observed_bytes"] = info.st_size
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            integrity["observed_sha256"] = digest
            if state:
                integrity["expected_bytes"] = state["archive_bytes"]
                integrity["expected_sha256"] = state["prefix_sha256"]
                if info.st_size != state["archive_bytes"]:
                    issues.append({"reason": "ARCHIVE_SIZE_MISMATCH"})
                if digest != state["prefix_sha256"]:
                    issues.append({"reason": "ARCHIVE_SHA256_MISMATCH"})
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                integrity["members_scanned"] = len(archive.infolist())
                entries, integrity["members_crc_verified"] = census_zip(archive, endpoint, issues)
            after = os.fstat(stream.fileno())
            require(
                os.path.samestat(info, plain(path))
                and (after.st_size, after.st_mtime_ns) == (info.st_size, info.st_mtime_ns),
                "ARCHIVE_CHANGED_DURING_CENSUS",
            )
    except (ValueError, OSError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as error:
        issues.append({"reason": "ARCHIVE_UNREADABLE_OR_UNSAFE", "detail": str(error)})
    integrity["status"] = "BLOCKED" if issues else "VERIFIED"
    return {
        "path": str(path),
        "integrity": integrity,
        "categories": census_categories(endpoint, entries, not issues),
    }, entries


def census_pilot(archives, entries):
    spec_path = REPO / "docs" / "f01_sec_bulk_pilot_spec.json"
    raw, spec = read_metadata(spec_path)
    issuers = spec["issuers"]
    ciks = {issuer["cik"] for issuer in issuers}
    require(
        spec["contract"] == "F01_SEC_BULK_PILOT_SPEC_V1"
        and spec["core_count"] == len(issuers) == len(ciks) == 8,
        "CENSUS_PILOT_SPEC_INVALID",
    )
    by_cik = {endpoint: {cik: [] for cik in ciks} for endpoint in URLS}
    for endpoint in URLS:
        for entry in entries[endpoint]:
            match = MEMBER_NAME.fullmatch(entry.name)
            if match and match[1] in ciks:
                by_cik[endpoint][match[1]].append(entry)
    results = []
    for issuer in issuers:
        cik = issuer["cik"]
        members = {}
        for endpoint in URLS:
            expected = issuer[f"{endpoint}_member"]
            require(expected == f"CIK{cik}.json", "CENSUS_PILOT_MEMBER_BINDING_INVALID")
            observed = by_cik[endpoint][cik]
            findings = [
                {"member": entry.name, "categories": entry.findings}
                for entry in observed
                if entry.findings
            ]
            primary_present = any(entry.name == expected for entry in observed)
            verified = archives[endpoint]["integrity"]["status"] == "VERIFIED"
            if findings or verified and not primary_present:
                status = "AFFECTED"
            elif not verified:
                status = "UNDETERMINED"
            else:
                status = "CLEAR"
            members[endpoint] = {
                "expected_primary": expected,
                "primary_present": primary_present,
                "status": status,
                "findings": findings,
            }
        results.append({"symbol": issuer["symbol"], "cik": cik, "endpoints": members})
    return {"spec_sha256": hashlib.sha256(raw).hexdigest(), "issuers": results}


def structure_census(layout, generation, *, progress):
    stage = layout.paths["temporary"] / f"{generation}.incomplete"
    report_dir = layout.paths["import_artifacts"] / f"{generation}-structure-census"
    output = report_dir / CENSUS_NAME
    require(not exists(report_dir), f"CENSUS_REPORT_DIRECTORY_ALREADY_EXISTS: {report_dir}")
    directory(stage)
    publish = directory(stage / "publish")
    lock_path = stage / "download.lock"
    plain(lock_path)
    with (
        pinned_files({}, directories=(stage, publish, layout.paths["import_artifacts"])),
        stage_lock(lock_path),
    ):
        archives, entries = {}, {}
        for endpoint in URLS:
            progress(f"Scanning staged ZIP structure and checkpoints: {endpoint}")
            archives[endpoint], entries[endpoint] = census_archive(
                endpoint, publish / f"{endpoint}.zip", stage / f"{endpoint}.checkpoint.json"
            )
        pilot = census_pilot(archives, entries)
        blockers = []
        for endpoint, archive in archives.items():
            if archive["integrity"]["status"] != "VERIFIED":
                blockers.append(f"{endpoint}:ARCHIVE_INTEGRITY")
            for category, summary in archive["categories"].items():
                reviewable = endpoint == "companyfacts" and category in COMPANYFACTS_REVIEWABLE
                if summary["count"] and category != "VALID" and not reviewable:
                    blockers.append(f"{endpoint}:{category}")
        for issuer in pilot["issuers"]:
            for endpoint, result in issuer["endpoints"].items():
                if result["status"] != "CLEAR":
                    blockers.append(f"pilot:{issuer['symbol']}:{endpoint}:{result['status']}")
        status = "CENSUS_BLOCKED" if blockers else "CENSUS_COMPLETE"
        report = {
            "contract": CENSUS_CONTRACT,
            "status": status,
            "generation": generation,
            "archives": archives,
            "pilot": pilot,
            "review_blockers": blockers,
            "publication_authorized": False,
            "import_authorized": False,
        }
        report_dir.mkdir()
        staged_report = output.with_suffix(".incomplete")
        with staged_report.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        staged_report.rename(output)
    return {"status": status, "report": str(output)}


def category_names(category):
    return (member for group in category["key_groups"] for member in group["members"])


def reviewed_exclusions(name, census, review):
    binding = review["archives"][name]
    integrity = census["integrity"]
    expected_issues = (
        [{"member": NON_DATA_MEMBER, "reason": "UNSAFE_OR_UNEXPECTED_MEMBER"}]
        if name == "submissions"
        else []
    )
    require(
        integrity["issues"] == expected_issues
        and integrity["status"] == ("BLOCKED" if expected_issues else "VERIFIED")
        and integrity["members_scanned"] == integrity["members_crc_verified"] == binding["members"]
        and integrity["observed_bytes"] == integrity["expected_bytes"] == binding["bytes"]
        and integrity["observed_sha256"] == integrity["expected_sha256"] == binding["sha256"],
        f"CENSUS_ARCHIVE_INTEGRITY_UNPROVEN: {name}",
    )
    categories = census["categories"]
    counts = (
        review["companyfacts_quarantine_counts"]
        if name == "companyfacts"
        else {"INVALID_PAYLOAD": 1, "UNKNOWN_STRUCTURE": 1}
    )
    require(
        set(categories) == set(CENSUS_CATEGORIES)
        and (name != "companyfacts" or set(counts) == COMPANYFACTS_REVIEWABLE),
        f"CENSUS_CATEGORY_CONTRACT: {name}",
    )
    excluded = {}
    for category, summary in categories.items():
        count = sum(len(group["members"]) for group in summary["key_groups"])
        require(
            summary["endpoint"] == name and summary["count"] == count,
            f"CENSUS_CATEGORY_COUNT_MISMATCH: {name}:{category}",
        )
        if category == "VALID":
            continue
        require(
            count == counts.get(category, 0) and summary["import_eligible"] is False,
            f"CENSUS_UNAPPROVED_FINDING: {name}:{category}",
        )
        if name == "submissions":
            if count:
                require(
                    summary["key_groups"] == [{"keys": None, "members": [NON_DATA_MEMBER]}],
                    "SUBMISSIONS_EXCEPTION_MUST_BE_EXACT_PLACEHOLDER",
                )
                excluded[NON_DATA_MEMBER] = "NON_DATA_ARCHIVE_MEMBER"
            continue
        for group in summary["key_groups"]:
            keys = group["keys"]
            expected_keys = (
                keys == []
                if category == "EMPTY_COMPANYFACTS"
                else isinstance(keys, list)
                and "facts" in keys
                and ("cik" in keys) == (category == "EMPTY_FACTS")
            )
            require(expected_keys, f"CENSUS_QUARANTINE_KEYS_MISMATCH: {category}")
            for member in group["members"]:
                require(
                    re.fullmatch(r"CIK[0-9]{10}\.json", member) and member not in excluded,
                    f"CENSUS_UNSAFE_OR_DUPLICATE_EXCLUSION: {member}",
                )
                excluded[member] = category
    valid = categories["VALID"]["count"]
    require(
        valid > 0 and valid + len(excluded) == binding["members"],
        f"CENSUS_MEMBER_PARTITION_MISMATCH: {name}",
    )
    summary = {
        "status": "VERIFIED",
        "members": binding["members"],
        "structurally_valid_members": valid,
        "excluded_member_count": len(excluded),
        "exclusion_counts": dict(sorted(Counter(excluded.values()).items())),
        "excluded_members": [
            {"member": member, "classification": category, "import_eligible": False}
            for member, category in sorted(excluded.items())
        ],
    }
    if name == "companyfacts":
        summary["empty_companyfacts"] = {
            "classification": "EMPTY_COMPANYFACTS",
            "count": counts["EMPTY_COMPANYFACTS"],
            "members": [
                member for member, c in sorted(excluded.items()) if c == "EMPTY_COMPANYFACTS"
            ],
            "import_eligible": False,
        }
    return summary


def reviewed_pilot(census, spec_sha):
    raw, spec = read_metadata(REPO / "docs" / "f01_sec_bulk_pilot_spec.json")
    require(
        hashlib.sha256(raw).hexdigest() == census["pilot"]["spec_sha256"] == spec_sha
        and spec["contract"] == "F01_SEC_BULK_PILOT_SPEC_V1"
        and len(spec["issuers"]) == spec["core_count"] == len(census["pilot"]["issuers"]) == 8
        and len({issuer["cik"] for issuer in spec["issuers"]}) == 8,
        "CENSUS_PILOT_SPEC_MISMATCH",
    )
    primary = {f"CIK{issuer['cik']}.json" for issuer in spec["issuers"]}
    for endpoint in URLS:
        categories = census["archives"][endpoint]["categories"]
        valid = Counter(
            member for member in category_names(categories["VALID"]) if member in primary
        )
        require(
            set(valid) == primary and all(n == 1 for n in valid.values()),
            f"CENSUS_PILOT_VALID_PRIMARY_REQUIRED: {endpoint}",
        )
        require(
            not any(
                primary.intersection(category_names(item))
                for cat, item in categories.items()
                if cat != "VALID"
            ),
            f"CENSUS_PILOT_QUARANTINE: {endpoint}",
        )
    pilot = []
    for expected, actual in zip(spec["issuers"], census["pilot"]["issuers"], strict=True):
        require(
            actual["symbol"] == expected["symbol"]
            and actual["cik"] == expected["cik"]
            and set(actual["endpoints"]) == set(URLS),
            "CENSUS_PILOT_IDENTITY_MISMATCH",
        )
        members = {}
        for endpoint, record in actual["endpoints"].items():
            filename = f"CIK{expected['cik']}.json"
            require(
                record["expected_primary"] == expected[f"{endpoint}_member"] == filename
                and record["primary_present"] is True
                and record["findings"] == []
                and record["status"] == ("CLEAR" if endpoint == "companyfacts" else "UNDETERMINED"),
                f"CENSUS_PILOT_BLOCKER: {expected['symbol']}:{endpoint}",
            )
            members[endpoint] = {"member": filename, "status": "STRUCTURE_CLEAR"}
        pilot.append({"symbol": expected["symbol"], "cik": expected["cik"], "endpoints": members})
    return pilot


def rehash_reviewed_zip(name, path, source, non_data):
    info = plain(path)
    with pinned_files({name: path}) as pins:
        stream = pins[name]
        require(
            os.path.samestat(info, os.fstat(stream.fileno())) and info.st_size == source["bytes"],
            f"REVIEWED_ARCHIVE_SIZE_OR_IDENTITY_MISMATCH: {name}",
        )
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == source["sha256"],
            f"ARCHIVE_SHA256_MISMATCH: {name}",
        )
        if name == "submissions":
            stream.seek(0)
            try:
                with zipfile.ZipFile(stream) as archive:
                    member = archive.getinfo(NON_DATA_MEMBER)
                    require(
                        sum(item.orig_filename == NON_DATA_MEMBER for item in archive.infolist())
                        == 1
                        and member.orig_filename == NON_DATA_MEMBER
                        and readable_member(member)
                        and member.file_size == non_data["bytes"] == len(NON_DATA_BYTES),
                        "REVIEWED_NON_DATA_MEMBER_STRUCTURE_MISMATCH",
                    )
                    content = read_zip_member(archive, member)
            except (zipfile.BadZipFile, zlib.error, EOFError) as error:
                raise Blocked("REVIEWED_NON_DATA_MEMBER_INTEGRITY_FAILED") from error
            require(
                content == NON_DATA_BYTES
                and hashlib.sha256(content).hexdigest() == non_data["sha256"],
                "REVIEWED_NON_DATA_MEMBER_CONTENT_MISMATCH",
            )
        final_info = os.fstat(stream.fileno())
    require(
        os.path.samestat(info, plain(path))
        and (final_info.st_size, final_info.st_mtime_ns) == (info.st_size, info.st_mtime_ns),
        f"ARCHIVE_CHANGED_DURING_REVIEWED_FINALIZATION: {name}",
    )
    return info


def reviewed_source(layout, generation, final, paths, checkpoints, census_input, progress):
    census_path, expected_sha = census_input
    canonical_census_path = (
        layout.paths["import_artifacts"] / f"{generation}-structure-census" / CENSUS_NAME
    )
    require(census_path == canonical_census_path, "REVIEWED_CENSUS_PATH_MISMATCH")
    review_raw, review = read_metadata(REVIEW_PATH)
    require(
        review["contract"] == REVIEW_CONTRACT
        and review["generation"] == generation
        and review["census_sha256"] == expected_sha
        and set(review["archives"]) == set(URLS)
        and review["submissions_non_data_member"]["member"] == NON_DATA_MEMBER,
        "REVIEWED_FINALIZATION_NOT_AUTHORIZED_FOR_EVIDENCE",
    )
    raw, census = read_metadata(census_path, max_bytes=MAX_REVIEWED_CENSUS_BYTES)
    require(
        hashlib.sha256(raw).hexdigest() == expected_sha
        and census["contract"] == CENSUS_CONTRACT
        and census["generation"] == generation
        and census["publication_authorized"] is False
        and census["import_authorized"] is False
        and set(census["archives"]) == set(URLS),
        "REVIEWED_CENSUS_BINDING_MISMATCH",
    )
    del raw
    states, validations, identities = {}, {}, {}
    for name, binding in review["archives"].items():
        prior_path = checkpoints[name].parent / "publish" / f"{name}.zip"
        require(
            census["archives"][name]["path"] == str(prior_path)
            and not exists(checkpoints[name].with_suffix(".next")),
            f"CENSUS_STAGED_PATH_OR_CHECKPOINT_MISMATCH: {name}",
        )
        states[name] = complete_download_state(
            paths[name], checkpoints[name], name, expected_sha=binding["checkpoint_sha256"]
        )
        require(
            states[name]["archive_bytes"] == binding["bytes"]
            and states[name]["prefix_sha256"] == binding["sha256"],
            f"REVIEWED_CHECKPOINT_ARCHIVE_BINDING: {name}",
        )
        validations[name] = reviewed_exclusions(name, census["archives"][name], review)
    pilot = reviewed_pilot(census, review["pilot_spec_sha256"])
    del census
    for name, binding in review["archives"].items():
        progress(f"Verifying actual staged/archive SHA-256 against reviewed CRC census: {name}")
        identities[name] = rehash_reviewed_zip(
            name, paths[name], binding, review["submissions_non_data_member"]
        )
    manifest = source_manifest(generation, final, states, validations)
    manifest.update(
        reviewed_census={
            "contract": REVIEW_CONTRACT,
            "path": str(census_path),
            "sha256": expected_sha,
            "review_sha256": hashlib.sha256(review_raw).hexdigest(),
            "pilot_spec_sha256": review["pilot_spec_sha256"],
            "checkpoints_sha256": {
                name: item["checkpoint_sha256"] for name, item in review["archives"].items()
            },
            "submissions_non_data_member": review["submissions_non_data_member"],
        },
        pilot_structure=pilot,
        qualified_research_issuers=[],
        trading_universe_qualified=False,
    )
    return manifest, identities


def finalize_offline(layout, generation, *, progress, reviewed_census=None):
    require(os.name == "nt", "DOWNLOAD_PUBLICATION_REQUIRES_WINDOWS")
    final = layout.paths["originals"] / generation
    stage = layout.paths["temporary"] / f"{generation}.incomplete"
    with pinned_files({}, directories=tuple(layout.paths.values())):
        require(not exists(final), f"ORIGINAL_GENERATION_ALREADY_EXISTS: {final}")
        directory(stage)
        lock_path = stage / "download.lock"
        if exists(lock_path):
            plain(lock_path)
        with stage_lock(lock_path):
            publish, paths, checkpoints = staging_files(stage)
            if reviewed_census is not None:
                manifest, identities = reviewed_source(
                    layout, generation, final, paths, checkpoints, reviewed_census, progress
                )
                for name, initial in identities.items():
                    current = plain(paths[name])
                    require(
                        os.path.samestat(initial, current)
                        and (initial.st_size, initial.st_mtime_ns)
                        == (current.st_size, current.st_mtime_ns),
                        f"ARCHIVE_CHANGED: {name}",
                    )
                return publish_source_manifest(
                    final, publish, paths, manifest, status=REVIEWED_STATUS
                )
            states = {
                name: complete_download_state(path, checkpoints[name], name)
                for name, path in paths.items()
            }
            return publish_generation(generation, final, publish, paths, states, progress)


def download(layout, generation, http, *, resume=False, progress):
    require(os.name == "nt", "DOWNLOAD_PUBLICATION_REQUIRES_WINDOWS")
    final = layout.paths["originals"] / generation
    stage = layout.paths["temporary"] / f"{generation}.incomplete"
    with pinned_files({}, directories=tuple(layout.paths.values())):
        require(not exists(final), f"ORIGINAL_GENERATION_ALREADY_EXISTS: {final}")
        if resume:
            directory(stage)
            directory(stage / "publish")
        else:
            require(not exists(stage), f"INCOMPLETE_GENERATION_EXISTS: {stage}")
        observations = collect_remote(http)
        require(
            all(item["bytes"] is not None for item in observations.values()),
            "REMOTE_ZIP_SIZE_UNKNOWN",
        )
        if not resume:
            require(
                capacity(layout, observations)["status"] == "CAPACITY_OK", "INSUFFICIENT_CAPACITY"
            )
            stage.mkdir()
            (stage / "publish").mkdir()
        lock_path = stage / "download.lock"
        if exists(lock_path):
            plain(lock_path)
        with stage_lock(lock_path):
            publish, paths, checkpoints = staging_files(stage)
            states = {
                name: resume_state(
                    paths[name], checkpoints[name], name, observations[name], resume=resume
                )
                for name in URLS
            }
            held = sum(state["received_bytes"] for state in states.values() if state)
            require(
                capacity(layout, observations, already_staged=held)["status"] == "CAPACITY_OK",
                "INSUFFICIENT_CAPACITY",
            )
            for name in URLS:
                states[name] = retrieve(
                    http,
                    name,
                    observations[name],
                    paths[name],
                    checkpoints[name],
                    states[name],
                    progress,
                )
            return publish_generation(generation, final, publish, paths, states, progress)


def verify_generation(layout, generation, expected_manifest_sha):
    final = directory(layout.paths["originals"] / generation)
    raw, manifest = read_metadata(final / MANIFEST_NAME, max_bytes=MAX_MANIFEST_BYTES)
    require(
        hashlib.sha256(raw).hexdigest() == expected_manifest_sha, "SOURCE_MANIFEST_SHA256_MISMATCH"
    )
    require(
        isinstance(manifest, dict)
        and manifest.get("contract") == MANIFEST_CONTRACT
        and manifest.get("generation") == generation
        and manifest.get("quality") == QUALITY
        and isinstance(manifest.get("archives"), dict)
        and set(manifest["archives"]) == set(URLS),
        "SOURCE_MANIFEST_CONTRACT_MISMATCH",
    )
    require(
        set(final.iterdir())
        == {final / name for name in (MANIFEST_NAME, *(f"{n}.zip" for n in URLS))},
        "PUBLISHED_GENERATION_LAYOUT_MISMATCH",
    )
    if "reviewed_census" in manifest:
        stage = directory(layout.paths["temporary"] / f"{generation}.incomplete")
        paths = {name: final / f"{name}.zip" for name in URLS}
        checkpoints = {name: stage / f"{name}.checkpoint.json" for name in URLS}
        evidence = manifest["reviewed_census"]
        expected, _ = reviewed_source(
            layout,
            generation,
            final,
            paths,
            checkpoints,
            (Path(evidence["path"]), evidence["sha256"]),
            lambda _: None,
        )
        require(manifest == expected, "REVIEWED_SOURCE_MANIFEST_MISMATCH")
        return {"status": REVIEWED_STATUS, "manifest_sha256": expected_manifest_sha}
    validations = {}
    for name, entry in manifest["archives"].items():
        path = final / f"{name}.zip"
        require(
            entry["local_path"] == str(path)
            and entry["url"] == URLS[name]
            and entry["endpoint"] == name
            and entry["archive_generation"] == f"{generation}/{name}",
            f"SOURCE_MANIFEST_ARCHIVE_BINDING: {name}",
        )
        validations[name] = validate_archive(path, name, entry["bytes"], entry["sha256"])
        actual = validations[name]
        # Earlier sealed manifests have no empty-member field and could only accept zero.
        if name == "companyfacts" and not actual["empty_companyfacts"]["count"]:
            legacy = {key: value for key, value in actual.items() if key != "empty_companyfacts"}
        else:
            legacy = actual
        require(
            entry["integrity"] in (actual, legacy), f"SOURCE_MANIFEST_ARCHIVE_INTEGRITY: {name}"
        )
    return {"status": "VERIFIED", "manifest_sha256": expected_manifest_sha, "archives": validations}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--network-preflight", action="store_true")
    modes.add_argument("--download", action="store_true")
    modes.add_argument("--finalize-offline", action="store_true", help="Verify and publish staging")
    modes.add_argument(
        "--structure-census", action="store_true", help="Scan staged ZIPs and write a diagnosis"
    )
    modes.add_argument("--verify", action="store_true")
    modes.add_argument("--verify-archive", type=Path)
    parser.add_argument("--authorize-network", action="store_true")
    parser.add_argument("--authorize-download", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--user-agent")
    parser.add_argument("--generation")
    parser.add_argument(
        "--sizes-from", type=Path, help="Saved network-preflight JSON (local mode only)"
    )
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--reviewed-census", type=Path)
    parser.add_argument("--census-sha256")
    parser.add_argument("--authorize-reviewed-publication", action="store_true")
    parser.add_argument("--endpoint", choices=URLS)
    parser.add_argument("--expected-bytes", type=int)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    network = args.network_preflight or args.download
    reviewed = args.reviewed_census or args.census_sha256 or args.authorize_reviewed_publication
    if (
        args.authorize_network != network
        or args.authorize_download != args.download
        or (args.resume and not args.download)
        or (network and not args.user_agent)
        or (
            args.sizes_from
            and (
                network
                or args.finalize_offline
                or args.structure_census
                or args.verify
                or args.verify_archive
            )
        )
        or bool(args.generation)
        != (args.download or args.finalize_offline or args.structure_census or args.verify)
        or (args.generation and not GENERATION.fullmatch(args.generation))
        or (args.verify and not SHA256.fullmatch(args.manifest_sha256 or ""))
        or (
            reviewed
            and not (
                args.finalize_offline
                and args.reviewed_census
                and SHA256.fullmatch(args.census_sha256 or "")
                and args.authorize_reviewed_publication
                and not args.user_agent
            )
        )
        or (
            args.verify_archive
            and not (
                args.endpoint
                and args.expected_bytes
                and args.expected_bytes > 0
                and SHA256.fullmatch(args.expected_sha256 or "")
            )
        )
    ):
        parser.error("Invalid mode or missing authorization/binding. See --help.")

    def progress(line):
        print(line, file=sys.stderr, flush=True)

    try:
        layout = Layout.load(args.layout)
        if args.verify:
            result = verify_generation(layout, args.generation, args.manifest_sha256)
        elif args.structure_census:
            result = structure_census(layout, args.generation, progress=progress)
        elif args.finalize_offline:
            result = finalize_offline(
                layout,
                args.generation,
                progress=progress,
                reviewed_census=(args.reviewed_census, args.census_sha256) if reviewed else None,
            )
        elif args.verify_archive:
            path = args.verify_archive
            require(
                path.is_absolute()
                and directory(path.parent).is_relative_to(layout.paths["originals"])
                and ":" not in path.name
                and path.suffix.lower() == ".zip",
                "VERIFY_ARCHIVE_REQUIRES_AN_ORIGINALS_ZIP",
            )
            result = validate_archive(
                path, args.endpoint, args.expected_bytes, args.expected_sha256
            )
        elif network:
            client = SecHttp(args.user_agent)
            if args.download:
                result = download(
                    layout,
                    args.generation,
                    client,
                    resume=args.resume,
                    progress=progress,
                )
            else:
                result = capacity(layout, collect_remote(client))
        else:
            result = capacity(layout, load_observations(args.sizes_from))
        print(json_bytes(result).decode(), end="")
        return (
            3
            if result["status"]
            in {
                "CAPACITY_BLOCKED",
                "CAPACITY_UNDETERMINED",
                "CENSUS_BLOCKED",
            }
            else 0
        )
    except (Blocked, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"SEC Bulk blocked: {error}", file=sys.stderr)
        return 3
    except TRANSPORT_ERRORS as error:
        print(f"SEC Bulk failed: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print(
            "SEC Bulk interrupted. Originals are untouched. Check the staging generation.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
