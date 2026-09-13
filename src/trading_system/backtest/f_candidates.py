"""Streaming F discovery and a content-verified, session-local replay artifact."""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from time import monotonic

from trading_system.backtest.discovery_snapshot import data_fingerprint as data_fingerprint
from trading_system.backtest.entry_quality import (
    F_INTRADAY_ENTRY_RESEARCH_FAMILY,
    F_INTRADAY_ENTRY_VARIANTS,
)
from trading_system.backtest.f_replay import replay_record, replay_report
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.models.screening import ScreenRecord
from trading_system.models.signals import TechnicalSnapshot

DISCOVERY_VERSION = 2


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def config_fingerprint(config):
    # Output/database paths do not affect research; everything else is conservative.
    return fingerprint(config.model_dump(mode="json", exclude={"storage"}))


def discovery_code_fingerprint():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in (
        "backtest/f_candidates.py",
        "backtest/discovery_snapshot.py",
        "backtest/f_replay.py",
        "backtest/lifecycle_validation.py",
        "backtest/lifecycle_diagnostics.py",
        "backtest/native_entries.py",
        "backtest/features.py",
        "backtest/engine.py",
        "backtest/screen_strategies.py",
        "backtest/research_registry.py",
        "backtest/entry_quality.py",
        "data/database.py",
        "data/sec_identity.py",
        "data/market_sessions.py",
        "data/universe.py",
        "strategy/screener.py",
        "strategy/scoring.py",
        "fundamentals/peers.py",
        "fundamentals/metrics.py",
        "fundamentals/quality.py",
        "technical/indicators.py",
        "technical/momentum.py",
        "config.py",
        "models/fundamentals.py",
        "models/market_data.py",
        "models/scores.py",
        "models/screening.py",
        "models/signals.py",
    ):
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def runtime_fingerprint():
    return fingerprint(
        {
            "python": list(sys.version_info[:3]),
            **{
                package: version(package)
                for package in ("numpy", "pandas", "pydantic", "exchange-calendars")
            },
        }
    )


@dataclass(frozen=True, slots=True)
class FSessionCandidates:
    records: tuple[tuple[float, ScreenRecord], ...]
    replay: dict | None


def compact_record(symbol, view):
    return {
        "symbol": symbol,
        "sic": getattr(view, "sic", None),
        "scores": [
            getattr(view.scores, name).score
            for name in ("quality", "valuation", "opportunity", "timing")
        ],
        "technical": view.technical.model_dump(mode="json", exclude_none=True),
        "exclusion_reasons": list(view.exclusion_reasons),
    }


def replay_session(session, eligible, rejected, omitted_rejections):
    return {
        "session": session.isoformat(),
        "eligible": [
            {**compact_record(record.symbol, record), "evaluation_score": score}
            for score, record in eligible
        ],
        "rejected": rejected,
        "omitted_rejections": dict(omitted_rejections),
    }


class ReplaySpool:
    """Disk spool: discovery never retains previous sessions' records or score graphs."""

    def __init__(self):
        self.file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 -- owned until export/replay
        self.digest = hashlib.sha256()
        self.offsets = {}
        self.records = self.bytes = 0
        self.serialization_seconds = 0.0

    def append(self, value):
        started = monotonic()
        self.offsets[date.fromisoformat(value["session"])] = self.file.tell()
        encoded = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
        self.file.write(encoded)
        self.digest.update(encoded)
        self.bytes += len(encoded)
        self.records += len(value["eligible"]) + len(value["rejected"])
        self.serialization_seconds += monotonic() - started


class CandidateManifest(dict):
    """JSON-compatible requirements with an owned, transient disk spool for export."""

    def __init__(self, payload, spool):
        super().__init__(payload)
        self.replay_spool = spool

    def close(self):
        self.replay_spool.file.close()


def manifest_metadata(config, candidates, spool, snapshot):
    return {
        "candidate_discovery_version": DISCOVERY_VERSION,
        "strategy_variant": "F",
        "config_fingerprint": config_fingerprint(config),
        "discovery_code_fingerprint": discovery_code_fingerprint(),
        "runtime_fingerprint": runtime_fingerprint(),
        "data_snapshot_fingerprint": snapshot,
        "data_snapshot_scope": "current universe/identity; participating PIT facts <= end; "
        "Daily technical warmup through end; SPY and portfolio session keys",
        "candidate_count": len(candidates),
        "candidate_fingerprint": fingerprint(candidates),
        "replay_sha256": spool.digest.hexdigest(),
    }


class ManifestScreenSource:
    """One session of F/configured evidence; no full ScreenRecord reconstruction.

    Retain every symbol from its first F eligibility onward, including all later
    rejections. Earlier rejections have no position/trigger consumer, only counters.
    """

    f_configured_replay = True

    def __init__(self, path, offsets, *, stream=None, checksums=None):
        self.path, self.offsets = path, offsets
        self.stream = stream
        self.cache = {}
        self.parse_seconds = 0.0
        self.checksums = checksums

    def screen(self, session):
        if session not in self.cache:
            started = monotonic()
            with nullcontext(self.stream) if self.stream else self.path.open("rb") as stream:
                stream.seek(self.offsets[session])
                line = stream.readline()
            if (
                self.checksums is not None
                and hashlib.sha256(line).digest() != self.checksums[session]
            ):
                raise ValueError(
                    "Candidate replay changed after manifest validation; rerun preflight"
                )
            payload = json.loads(line)
            self.cache.clear()
            self.cache[session] = replay_report(payload)
            self.parse_seconds += monotonic() - started
        return self.cache[session]


def load_manifest(path, database, config, start, end, sessions, *, diagnostics=None):
    started = monotonic()
    try:
        return _load_manifest(path, database, config, start, end, sessions, diagnostics)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed candidate manifest/replay; rerun preflight") from exc
    finally:
        if diagnostics is not None:
            diagnostics["manifest_validation_seconds"] = monotonic() - started


def _load_manifest(path, database, config, start, end, sessions, diagnostics):
    from trading_system.backtest.engine import evaluate_variant_entry

    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {
        "report_type": "intraday_candidate_requirements",
        "research_family": F_INTRADAY_ENTRY_RESEARCH_FAMILY,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "strategy_variant": "F",
        "candidate_discovery_version": DISCOVERY_VERSION,
        "config_fingerprint": config_fingerprint(config),
        "discovery_code_fingerprint": discovery_code_fingerprint(),
        "runtime_fingerprint": runtime_fingerprint(),
        "discovery_complete": True,
        "candidate_discovery_status": "COMPLETE",
        "timeframes": ["15m"],
        "extended_hours": False,
        "warmup_bars": 0,
        "strategies": [item.label for item in F_INTRADAY_ENTRY_VARIANTS],
        "potential_position_ranges": [],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"Candidate manifest incompatible/stale: {key}; rerun preflight")
    if manifest.get("data_snapshot_fingerprint") != data_fingerprint(
        database, config, start=start, end=end, diagnostics=diagnostics
    ):
        raise ValueError(
            "Candidate manifest incompatible/stale: data_snapshot_fingerprint; rerun preflight"
        )
    candidates = manifest.get("candidate_sessions", [])
    if (
        manifest.get("candidate_count") != len(candidates)
        or manifest.get("candidate_fingerprint") != fingerprint(candidates)
        or manifest.get("required_sessions") != candidates
        or manifest.get("candidate_symbols")
        != [{"symbol": symbol} for symbol in sorted({row["symbol"] for row in candidates})]
    ):
        raise ValueError("Candidate manifest count/requirements/checksum mismatch")
    replay_name = manifest.get("replay_file", "")
    if (
        not replay_name
        or Path(replay_name).name != replay_name
        or any(character in replay_name for character in "/\\:")
    ):
        raise ValueError("Candidate manifest requires a sibling replay file")
    replay_path = Path(path).parent / replay_name
    if replay_path.resolve().parent != Path(path).resolve().parent:
        raise ValueError("Candidate manifest requires a sibling replay file")
    offsets, digest, by_session = {}, hashlib.sha256(), defaultdict(list)
    for row in candidates:
        by_session[row["signal_date"]].append(row)
    relevant, checksums = set(), {}
    with replay_path.open("rb") as stream:
        for session, execution in zip(sessions[:-1], sessions[1:], strict=True):
            offsets[session] = stream.tell()
            line = stream.readline()
            digest.update(line)
            checksums[session] = hashlib.sha256(line).digest()
            payload = json.loads(line)
            if payload["session"] != session.isoformat():
                raise ValueError("Candidate manifest replay session mismatch")
            found = []
            seen = set()
            for row in payload["eligible"]:
                record = _validated_replay_record(row, session)
                evaluation = evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config)
                if not evaluation.eligible or row["evaluation_score"] != evaluation.score:
                    raise ValueError("Candidate manifest has an invalid F entry or PIT session")
                found.append((evaluation.score, record.symbol))
                seen.add(record.symbol)
            if found != sorted(found, key=lambda item: (-item[0], item[1])):
                raise ValueError("Candidate manifest replay rank order mismatch")
            for row in payload["rejected"]:
                record = _validated_replay_record(row, session)
                if (
                    record.symbol not in relevant
                    or record.symbol in seen
                    or evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config).eligible
                ):
                    raise ValueError("Candidate manifest has invalid retained rejection")
                seen.add(record.symbol)
            relevant.update(symbol for _, symbol in found)
            omitted = payload["omitted_rejections"]
            if not isinstance(omitted, dict) or any(
                not isinstance(reason, str) or not reason or type(count) is not int or count <= 0
                for reason, count in omitted.items()
            ):
                raise ValueError("Candidate manifest has invalid rejection counts")
            required = by_session.pop(session.isoformat(), [])
            if len(found) != len(required) or len({symbol for _, symbol in found}) != len(found):
                raise ValueError("Candidate manifest replay count mismatch")
            for rank, ((score, symbol), row) in enumerate(zip(found, required, strict=True), 1):
                if (
                    row["symbol"] != symbol
                    or row["evaluation_score"] != score
                    or row["candidate_rank"] != rank
                    or row["execution_session"] != execution.isoformat()
                    or row["timeframe"] != "15m"
                    or row["requirement_type"] != "candidate_session"
                    or row["signal_session"] != session.isoformat()
                    or row["candidate_paths"] != [F_INTRADAY_ENTRY_VARIANTS[1].label]
                ):
                    raise ValueError("Candidate manifest rank/score/requirement mismatch")
        if stream.read(1) or by_session or digest.hexdigest() != manifest.get("replay_sha256"):
            raise ValueError("Candidate manifest replay checksum/session mismatch")
    return manifest, ManifestScreenSource(replay_path, offsets, checksums=checksums)


def _validated_replay_record(row, session):
    """Validate external payload once; subsequent trusted replay uses slotted views."""
    if (
        not isinstance(row["symbol"], str)
        or not row["symbol"]
        or row.get("sic") is not None
        and not isinstance(row["sic"], str)
        or len(row["scores"]) != 4
        or any(
            value is not None and (type(value) not in (float, int) or not math.isfinite(value))
            for value in row["scores"]
        )
        or not isinstance(row["exclusion_reasons"], list)
        or any(not isinstance(reason, str) for reason in row["exclusion_reasons"])
        or set(row["technical"]) - TechnicalSnapshot.model_fields.keys()
    ):
        raise ValueError("Malformed candidate replay record; rerun preflight")
    technical = TechnicalSnapshot.model_validate(row["technical"])
    if technical.market_session is not None and technical.market_session > session:
        raise ValueError("Candidate manifest has future technical evidence")
    return replay_record(row, session)
