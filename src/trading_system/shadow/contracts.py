"""Canonical journal encoding and frozen implementation identity."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from importlib.metadata import version
from pathlib import Path

from pydantic import BaseModel

from trading_system.backtest.research_registry import FROZEN_CHAMPION_F, validate_champion_config
from trading_system.config import PROJECT_ROOT, StrategyConfig

SCHEMA_VERSION = 1
# Names the existing CC-01 API, not a second integrity implementation.
STRICT_CONTRACT_VERSION = "CC01/validate_session_position_data/v1"
GENESIS_HASH = "0" * 64
IDENTITY = "FROZEN_CHAMPION_F"
LABEL = "F/configured/C1"


class ShadowError(RuntimeError):
    def __init__(self, code: str, **details) -> None:
        super().__init__(code)
        self.code, self.details = code, details

    def as_dict(self) -> dict:
        return {"status": self.code, **self.details}


def normalized(value):
    """JSON-native values; finite binary64 floats, decimal strings, UTC ISO dates.

    No rounding of engine floats. JSON's shortest round-trip binary64 representation
    is frozen with the runtime fingerprint. Decimal source prices retain exact value.
    """
    if isinstance(value, BaseModel):
        return normalized(value.model_dump(mode="python"))
    if is_dataclass(value):
        return normalized(asdict(value))
    if isinstance(value, Enum):
        return normalized(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Naive journal timestamp")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite decimal")
        return format(value.normalize(), "f")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Journal object keys must be strings")
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((normalized(item) for item in value), key=canonical_json)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite journal float")
        return 0.0 if value == 0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported journal value: {type(value).__name__}")


def canonical_json(value) -> str:
    return json.dumps(
        normalized(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# Whole UTF-8 files with CRLF normalized to LF. Deliberately conservative scope.
FINGERPRINT_PATTERNS = (
    "champion.py",
    "config.py",
    "cli.py",
    "backtest/engine.py",
    "backtest/position_manager.py",
    "backtest/presets.py",
    "backtest/diagnostics.py",
    "backtest/screen_strategies.py",
    "backtest/research_registry.py",
    "backtest/research_definitions.py",
    "backtest/data_integrity.py",
    "data/database.py",
    "data/market_sessions.py",
    "data/universe.py",
    "data/sec_identity.py",
    "fundamentals/*.py",
    "technical/*.py",
    "strategy/screener.py",
    "strategy/scoring.py",
    "models/*.py",
    "shadow/*.py",
)


def economic_config(config: StrategyConfig) -> dict:
    data = config.model_dump(mode="python", exclude={"storage", "sec"})
    # Journal capital is immutable inception metadata; never a per-run setting.
    data["backtest"].pop("initial_capital")
    return normalized(data)


def fingerprints(config: StrategyConfig) -> dict:
    try:
        validate_champion_config(config)
    except ValueError as exc:
        raise ShadowError("SHADOW_CONFIGURATION_DRIFT", reason=str(exc)) from exc
    if (
        FROZEN_CHAMPION_F.production_label != LABEL
        or FROZEN_CHAMPION_F.variant.name != "QUALITY_VALUE_MOMENTUM"
        or FROZEN_CHAMPION_F.preset.name != "CONFIGURED"
    ):
        raise ShadowError("SHADOW_CONFIGURATION_DRIFT", reason="champion identity")
    root = PROJECT_ROOT / "src/trading_system"
    files = sorted({path for pattern in FINGERPRINT_PATTERNS for path in root.glob(pattern)})
    file_hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for path in files
    }
    import platform

    runtime = {"python": platform.python_version()}
    for package in ("exchange-calendars", "pandas", "numpy", "pydantic", "PyYAML"):
        runtime[package] = version(package)
    return {
        "strategy_identity": IDENTITY,
        "production_label": FROZEN_CHAMPION_F.production_label,
        "champion_definition": normalized(asdict(FROZEN_CHAMPION_F)),
        "strategy_fingerprint": digest(asdict(FROZEN_CHAMPION_F)),
        "economic_config": economic_config(config),
        "economic_config_fingerprint": digest(economic_config(config)),
        "engine_fingerprint": digest({"files": file_hashes, "runtime": runtime}),
        "engine_files": file_hashes,
        "runtime": runtime,
        "data_integrity_contract_version": STRICT_CONTRACT_VERSION,
        "strict_contract_fingerprint": file_hashes["backtest/data_integrity.py"],
        "journal_schema_version": SCHEMA_VERSION,
    }


def assert_no_drift(inception: dict, current: dict) -> None:
    changed = sorted(key for key in current if inception.get(key) != current[key])
    if changed:
        raise ShadowError("SHADOW_CONFIGURATION_DRIFT", changed_fields=changed)


def local_git_identity() -> dict:
    """Only local Git reads. No remote commands or global Git configuration writes."""

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={PROJECT_ROOT.as_posix()}", *args],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    return {"local_git_commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current")}
