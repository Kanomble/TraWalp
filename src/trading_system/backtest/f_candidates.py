"""Streaming F discovery and a content-verified, session-local replay artifact."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from trading_system.backtest.entry_quality import (
    F_INTRADAY_ENTRY_RESEARCH_FAMILY,
    F_INTRADAY_ENTRY_VARIANTS,
)
from trading_system.backtest.research_registry import FROZEN_CHAMPION_F
from trading_system.models.screening import ScreenRecord
from trading_system.models.signals import TechnicalSnapshot

DISCOVERY_VERSION = 1


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
        "backtest/features.py",
        "backtest/engine.py",
        "backtest/screen_strategies.py",
        "backtest/research_registry.py",
        "backtest/entry_quality.py",
        "data/database.py",
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


def data_fingerprint(database):
    """Hash actual discovery inputs, including corrections; intraday-only sync is compatible.

    One streaming read transaction, independent of candidate count. No full database
    file hash (a native 15m sync must not invalidate Daily candidate decisions).
    """
    digest = hashlib.sha256()
    with database.read_only() as connection:
        connection.execute("BEGIN")
        for table, where, order in (
            ("assets", "", "symbol"),
            ("companies", "", "symbol"),
            ("fundamental_facts", "", "id"),
            ("bars", "WHERE timeframe='1d'", "symbol,timeframe,timestamp"),
            (
                "sync_state",
                "WHERE source IN ('sec_identity_conflicts','sec_reference')",
                "source,key",
            ),
        ):
            digest.update(table.encode())
            cursor = connection.execute(f"SELECT * FROM {table} {where} ORDER BY {order}")
            while rows := cursor.fetchmany(4096):
                for row in rows:
                    digest.update(json.dumps(tuple(row), separators=(",", ":")).encode())
                    digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class FSessionCandidates:
    records: tuple[tuple[float, ScreenRecord], ...]
    replay: dict


def compact_record(symbol, view):
    return {
        "symbol": symbol,
        "scores": [
            getattr(view.scores, name).score
            for name in ("quality", "valuation", "opportunity", "timing")
        ],
        "technical": view.technical.model_dump(mode="json", exclude_none=True),
        "exclusion_reasons": list(view.exclusion_reasons),
    }


def replay_session(session, eligible, rejected):
    return {
        "session": session.isoformat(),
        "eligible": [record.model_dump(mode="json") for _, record in eligible],
        "rejected": rejected,
    }


class ReplaySpool:
    """Disk spool: discovery never retains previous sessions' records or score graphs."""

    def __init__(self):
        self.file = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 -- owned until export/replay
        self.digest = hashlib.sha256()
        self.offsets = {}

    def append(self, value):
        self.offsets[date.fromisoformat(value["session"])] = self.file.tell()
        encoded = (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()
        self.file.write(encoded)
        self.digest.update(encoded)


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
        "data_snapshot_scope": "all local Daily bars, facts, universe, identity quarantine",
        "candidate_count": len(candidates),
        "candidate_fingerprint": fingerprint(candidates),
        "replay_sha256": spool.digest.hexdigest(),
    }


class ManifestScreenSource:
    """Replay one disk session at a time, including rejected-symbol score/trigger evidence.

    Only eligible entries become full ScreenRecords. Rejected views satisfy the
    canonical evaluator and open-position score/re-entry observations structurally.
    """

    def __init__(self, path, offsets, *, stream=None):
        self.path, self.offsets = path, offsets
        self.stream = stream
        self.cache = {}

    def screen(self, session):
        if session not in self.cache:
            with nullcontext(self.stream) if self.stream else self.path.open("rb") as stream:
                stream.seek(self.offsets[session])
                payload = json.loads(stream.readline())
            records = [ScreenRecord.model_validate(row) for row in payload["eligible"]]
            for row in payload["rejected"]:
                scores = SimpleNamespace(
                    **{
                        name: SimpleNamespace(score=value)
                        for name, value in zip(
                            ("quality", "valuation", "opportunity", "timing"),
                            row["scores"],
                            strict=True,
                        )
                    }
                )
                records.append(
                    SimpleNamespace(
                        symbol=row["symbol"],
                        scores=scores,
                        technical=TechnicalSnapshot.model_validate(row["technical"]),
                        exclusion_reasons=tuple(row["exclusion_reasons"]),
                    )
                )
            self.cache.clear()
            self.cache[session] = SimpleNamespace(as_of=session, records=tuple(records))
        return self.cache[session]


def load_manifest(path, database, config, start, end, sessions):
    try:
        return _load_manifest(path, database, config, start, end, sessions)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed candidate manifest/replay; rerun preflight") from exc


def _load_manifest(path, database, config, start, end, sessions):
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
    if manifest.get("data_snapshot_fingerprint") != data_fingerprint(database):
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
    if not replay_name or Path(replay_name).name != replay_name:
        raise ValueError("Candidate manifest requires a sibling replay file")
    replay_path = Path(path).parent / replay_name
    offsets, digest, by_session = {}, hashlib.sha256(), defaultdict(list)
    for row in candidates:
        by_session[row["signal_date"]].append(row)
    with replay_path.open("rb") as stream:
        for session, execution in zip(sessions[:-1], sessions[1:], strict=True):
            offsets[session] = stream.tell()
            line = stream.readline()
            digest.update(line)
            payload = json.loads(line)
            if payload["session"] != session.isoformat():
                raise ValueError("Candidate manifest replay session mismatch")
            found = []
            for row in payload["eligible"]:
                record = ScreenRecord.model_validate(row)
                evaluation = evaluate_variant_entry(record, FROZEN_CHAMPION_F.variant, config)
                if not evaluation.eligible or record.as_of != session:
                    raise ValueError("Candidate manifest has an invalid F entry or PIT session")
                found.append((evaluation.score, record.symbol))
            found.sort(key=lambda item: (-item[0], item[1]))
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
    return manifest, ManifestScreenSource(replay_path, offsets)
