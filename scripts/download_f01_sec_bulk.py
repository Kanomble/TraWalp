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
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.dont_write_bytecode = True
from prepare_f01_migration import REPO  # noqa: E402

from trading_system.data.canonical_bootstrap import BootstrapBlocked, _local_drive  # noqa: E402
from trading_system.data.canonical_paths import pinned_files  # noqa: E402
from trading_system.data.sec_sync_persistence import stage_lock  # noqa: E402

URLS = {
    "companyfacts": "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip",
    "submissions": "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
}
MANIFEST_NAME = "f01_sec_bulk_source_manifest.json"
MANIFEST_CONTRACT = "F01_SEC_BULK_SOURCE_MANIFEST_V1"
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
        request = urllib.request.Request(
            URLS[name],
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
                        raise Blocked(f"SEC_HTTP_{error.code}: {name}") from error
                    wait = retry_delay(error.headers.get("Retry-After"), error.code, attempt)
            except TRANSPORT_ERRORS as error:
                if attempt == 2:
                    raise Blocked(f"SEC_TRANSPORT_FAILED: {name}") from error
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


def validate_members(archive, name):
    unique, supplement_files, supplement_refs = set(), set(), set()
    empty_companyfacts = []
    issuers = 0
    for member in archive.infolist():
        filename = member.orig_filename
        match = MEMBER_NAME.fullmatch(filename)
        require(
            match
            and filename not in unique
            and stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG)
            and not member.flag_bits & 1
            and 0 < member.file_size <= MAX_MEMBER_BYTES,
            f"UNSAFE_UNEXPECTED_OR_DUPLICATE_MEMBER: {filename!r}",
        )
        cik, historical = match.groups()
        require(not historical or name == "submissions", f"UNEXPECTED_MEMBER: {filename!r}")
        unique.add(filename)
        with archive.open(member) as stream:
            # Reading to EOF verifies the ZIP member CRC. Only one JSON is decoded at a time.
            raw = stream.read(MAX_MEMBER_BYTES + 1)
            require(len(raw) == member.file_size, f"ZIP_MEMBER_SIZE_MISMATCH: {filename}")
            record = json.loads(raw)
        require(isinstance(record, dict), f"ZIP_MEMBER_OBJECT_REQUIRED: {filename}")
        if name == "companyfacts" and not record:
            empty_companyfacts.append(filename)
            continue
        if "cik" in record:
            value = str(record["cik"])
            require(
                bool(re.fullmatch(r"[0-9]{1,10}", value)) and value.zfill(10) == cik,
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
                filings = record.get("filings")
                require(
                    isinstance(filings, dict)
                    and isinstance(filings.get("recent"), dict)
                    and isinstance(filings.get("files"), list),
                    f"INVALID_SUBMISSIONS: {filename}",
                )
                for item in filings["files"]:
                    ref = item.get("name") if isinstance(item, dict) else None
                    require(
                        isinstance(ref, str)
                        and re.fullmatch(rf"CIK{cik}-submissions-[0-9]{{3,}}\.json", ref)
                        and ref not in supplement_refs,
                        f"INVALID_SUBMISSION_CHUNK_REFERENCE: {filename}",
                    )
                    supplement_refs.add(ref)
        del record, raw
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


def read_checkpoint(path, checkpoint_path, name):
    require(exists(path) and exists(checkpoint_path), f"PARTIAL_STATE_AMBIGUOUS: {name}")
    _, state = read_metadata(checkpoint_path)
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
        and plain(path).st_size == count
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
    manifest = json_bytes(source_manifest(generation, final, states, validations))
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
        "status": "PUBLISHED",
        "manifest": str(final / MANIFEST_NAME),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }


def complete_download_state(path, checkpoint, name):
    state = read_checkpoint(path, checkpoint, name)
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


def finalize_offline(layout, generation, *, progress):
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
    parser.add_argument("--endpoint", choices=URLS)
    parser.add_argument("--expected-bytes", type=int)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    network = args.network_preflight or args.download
    if (
        args.authorize_network != network
        or args.authorize_download != args.download
        or (args.resume and not args.download)
        or (network and not args.user_agent)
        or (
            args.sizes_from
            and (network or args.finalize_offline or args.verify or args.verify_archive)
        )
        or bool(args.generation) != (args.download or args.finalize_offline or args.verify)
        or (args.generation and not GENERATION.fullmatch(args.generation))
        or (args.verify and not SHA256.fullmatch(args.manifest_sha256 or ""))
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
        elif args.finalize_offline:
            result = finalize_offline(layout, args.generation, progress=progress)
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
        return 3 if result["status"] in {"CAPACITY_BLOCKED", "CAPACITY_UNDETERMINED"} else 0
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
