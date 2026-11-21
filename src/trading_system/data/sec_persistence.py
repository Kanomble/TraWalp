"""Explicit SEC persistence backends. No runtime registration or provider calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_system.data.canonical_facts import (
    COLS,
    Admission,
    RequestIdentity,
    fingerprint,
    install_writer_guard,
    write_company,
)
from trading_system.data.database import (
    _execute_sec_company_update,
    _fact_rows,
    _require_complete_migration,
    _require_legacy_fact_writer,
)
from trading_system.models.fundamentals import CompanyIdentity, FundamentalFact

REQUEST_CONTRACT = "F01_SEC_REQUEST_V1"
LEGACY_RECEIPTS = "sec_persistence_receipt_v1"


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def provider_cik(payload) -> str:
    value = payload.get("cik") if isinstance(payload, dict) else None
    if (
        type(value) not in (int, str)
        or not re.fullmatch(r"[0-9]{1,10}", str(value))
        or int(value) == 0
    ):
        raise ValueError("SEC_PROVIDER_CIK_INVALID")
    return str(value).zfill(10)


class SecRequestEnvelope(BaseModel):
    """An immutable observation plus the exact normalization and admission plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation_id: str = Field(min_length=1)
    company: CompanyIdentity
    identity: RequestIdentity
    retrieved_at: datetime
    parser_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    payload_json: str
    facts: tuple[FundamentalFact, ...]
    accessions: tuple[str, ...]
    admissions: tuple[tuple[str, str, Admission], ...]

    @field_validator("retrieved_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("SEC_RETRIEVAL_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        payloads = json.loads(self.payload_json)
        if (
            not isinstance(payloads, dict)
            or set(payloads) != {"submissions", "companyfacts"}
            or json_bytes(payloads).decode() != self.payload_json
        ):
            raise ValueError("SEC_PAYLOAD_ENVELOPE_INVALID")
        if any(provider_cik(payload) != self.company.cik for payload in payloads.values()):
            raise ValueError("SEC_PROVIDER_CIK_MISMATCH")
        if not isinstance(payloads["companyfacts"].get("facts"), dict):
            raise ValueError("SEC_COMPANYFACTS_INVALID")
        if (
            self.identity.cik != self.company.cik
            or self.identity.symbol != self.company.symbol
            or not self.identity.evidence.strip()
            or self.identity.unresolved_conflict
            or not self.company.symbol.strip()
            or not all(
                s.strip() for s in (self.observation_id, self.parser_version, self.policy_version)
            )
            or any(fact.cik != self.company.cik for fact in self.facts)
        ):
            raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")
        if self.accessions != tuple(sorted(set(self.accessions))) or any(
            not acc.strip() for acc in self.accessions
        ):
            raise ValueError("SEC_ACCESSIONS_NOT_CANONICAL")
        if not {fact.accession_number for fact in self.facts if fact.accession_number}.issubset(
            self.accessions
        ):
            raise ValueError("SEC_FACT_ACCESSION_MISSING")
        keys = [(symbol, taxonomy) for symbol, taxonomy, _ in self.admissions]
        if keys != sorted(set(keys)) or any(
            not item.evidence.strip() for _, _, item in self.admissions
        ):
            raise ValueError("SEC_ADMISSIONS_NOT_CANONICAL")
        return self

    @classmethod
    def capture(
        cls,
        *,
        observation_id: str,
        company: CompanyIdentity,
        identity: RequestIdentity,
        retrieved_at: datetime,
        submissions: dict,
        companyfacts: dict,
        facts: Iterable[FundamentalFact],
        accessions: Iterable[str],
        parser_version: str,
        policy_version: str,
        admissions: Mapping[tuple[str, str], Admission] | None = None,
    ) -> Self:
        return cls(
            observation_id=observation_id,
            company=company,
            identity=identity,
            retrieved_at=retrieved_at,
            parser_version=parser_version,
            policy_version=policy_version,
            payload_json=json_bytes(
                {"submissions": submissions, "companyfacts": companyfacts}
            ).decode(),
            facts=tuple(facts),
            accessions=tuple(sorted(set(accessions))),
            admissions=tuple(
                (symbol, taxonomy, rule)
                for (symbol, taxonomy), rule in sorted((admissions or {}).items())
            ),
        )

    @property
    def request_id(self) -> str:
        # Observation IDs must be unique within the caller's observation stream.
        # A different envelope under the same ID must conflict, not write again.
        return digest(json_bytes([REQUEST_CONTRACT, self.company.cik, self.observation_id]))

    @property
    def payload_hash(self) -> str:
        return digest(self.payload_json.encode("utf-8"))

    @property
    def input_hash(self) -> str:
        return digest(json_bytes(self.model_dump(mode="json")))

    def archive_bytes(self) -> bytes:
        return json_bytes(
            {
                "contract": REQUEST_CONTRACT,
                "request_id": self.request_id,
                "input_hash": self.input_hash,
                "payload_hash": self.payload_hash,
                "envelope": self.model_dump(mode="json"),
            }
        )

    def admission_map(self) -> dict[tuple[str, str], Admission]:
        return {(symbol, taxonomy): rule for symbol, taxonomy, rule in self.admissions}


class SecRequestArchive:
    """Append-only local archive, separate from the fact database."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).absolute()

    def _path(self, request_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", request_id):
            raise ValueError("INVALID_SEC_REQUEST_ID")
        return self.directory / (request_id + ".json")

    def load(self, request_id: str) -> SecRequestEnvelope:
        raw = self._path(request_id).read_bytes()
        try:
            envelope = SecRequestEnvelope.model_validate(json.loads(raw)["envelope"])
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("SEC_REQUEST_ARCHIVE_INTEGRITY_FAILURE") from error
        if envelope.request_id != request_id or envelope.archive_bytes() != raw:
            raise ValueError("SEC_REQUEST_ARCHIVE_INTEGRITY_FAILURE")
        return envelope

    def store(self, envelope: SecRequestEnvelope) -> None:
        raw = envelope.archive_bytes()
        path = self._path(envelope.request_id)
        # The caller provisions the persistent archive directory before ingestion.
        with NamedTemporaryFile(dir=self.directory, prefix=".sec-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                try:
                    self._publish(temporary, path)
                except FileExistsError:
                    if path.read_bytes() != raw:
                        raise ValueError("SEC_REQUEST_ARCHIVE_CONFLICT") from None
                if os.name != "nt":
                    fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _publish(temporary: Path, target: Path) -> None:
        if os.name != "nt":
            os.link(temporary, target)
            return
        # Same-directory Windows rename with write-through and no replacement.
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        if not move(str(temporary), str(target), 0x8):  # MOVEFILE_WRITE_THROUGH
            code = ctypes.get_last_error()
            if code in (80, 183):  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
                raise FileExistsError(str(target))
            raise ctypes.WinError(code)


@dataclass(frozen=True)
class SecPersistOutcome:
    dataset_version: int | None
    receipt_id: str
    replayed: bool
    facts_processed: int
    commit_reconciled: bool = False


def require_current_identity(connection: sqlite3.Connection, envelope: SecRequestEnvelope) -> None:
    cik, primary = envelope.company.cik, envelope.company.symbol
    assigned = connection.execute("SELECT symbol FROM companies WHERE cik=?", (cik,)).fetchone()
    if assigned and assigned[0] != primary:
        raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")
    reference = connection.execute(
        "SELECT value FROM sync_state WHERE source='sec_reference' AND key='ticker_to_cik'"
    ).fetchone()
    mapping = json.loads(reference[0]) if reference else {}
    for symbol in {primary} | {fact.symbol for fact in envelope.facts}:
        local = connection.execute("SELECT cik FROM companies WHERE symbol=?", (symbol,)).fetchone()
        conflict = connection.execute(
            "SELECT value FROM sync_state WHERE source='sec_identity_conflicts' AND key=?",
            (symbol,),
        ).fetchone()
        proposed = mapping.get(symbol)
        if (
            local
            and local[0] != cik
            or conflict
            and json.loads(conflict[0]).get("status") == "unresolved"
            or proposed
            and str(proposed).zfill(10) != cik
        ):
            raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")


class SecCompanyPersistence(ABC):
    """Own one write transaction on a dedicated connection to an existing file."""

    def __init__(self, path: str | Path, archive: SecRequestArchive):
        self.path = Path(path).absolute()
        self.archive = archive

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None)

    @abstractmethod
    def _validate_request(
        self, envelope: SecRequestEnvelope, expected_version: int | None
    ) -> None: ...

    @abstractmethod
    def _guard(self, connection: sqlite3.Connection) -> None: ...

    @abstractmethod
    def _receipt(
        self, connection: sqlite3.Connection, envelope: SecRequestEnvelope
    ) -> SecPersistOutcome | None: ...

    @abstractmethod
    def _write(
        self,
        connection: sqlite3.Connection,
        envelope: SecRequestEnvelope,
        expected_version: int | None,
    ) -> int | None: ...

    def persist(
        self, envelope: SecRequestEnvelope, *, expected_version: int | None = None
    ) -> SecPersistOutcome:
        # Revalidate also instances constructed with model_copy/model_construct.
        envelope = SecRequestEnvelope.model_validate_json(envelope.model_dump_json())
        self._validate_request(envelope, expected_version)
        self.archive.store(envelope)
        target = self.path.stat()
        commit_attempted = False
        try:
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._guard(connection)
                    receipt = self._receipt(connection, envelope)
                    if receipt is not None:
                        connection.rollback()
                        return receipt
                    require_current_identity(connection, envelope)
                    state = connection.execute(
                        "SELECT value FROM sync_state WHERE source='sec_accessions' AND key=?",
                        (envelope.company.cik,),
                    ).fetchone()
                    stored_accessions = json.loads(state[0]) if state else []
                    if not isinstance(stored_accessions, list) or not all(
                        isinstance(accession, str) for accession in stored_accessions
                    ):
                        raise ValueError("SEC_ACCESSION_STATE_INVALID")
                    if not set(stored_accessions).issubset(envelope.accessions):
                        raise ValueError("STALE_SEC_ACCESSIONS")
                    version = self._write(connection, envelope, expected_version)
                    connection.execute(
                        "DELETE FROM sync_state WHERE source='sec_submissions_status' AND key=?",
                        (envelope.company.cik,),
                    )
                    commit_attempted = True
                    connection.commit()
                except BaseException as error:
                    if connection.in_transaction:
                        try:
                            connection.rollback()
                        except Exception as rollback_error:
                            error.add_note(f"SEC rollback also failed: {rollback_error!r}")
                    raise
        except Exception as error:
            if not commit_attempted:
                raise
            receipt = self._reconcile_commit(envelope, target, error)
            if receipt is None:
                raise
            return SecPersistOutcome(
                receipt.dataset_version,
                receipt.receipt_id,
                False,
                receipt.facts_processed,
                commit_reconciled=True,
            )
        return SecPersistOutcome(version, envelope.request_id, False, len(envelope.facts))

    def _reconcile_commit(self, envelope, target, original_error) -> SecPersistOutcome | None:
        try:
            if not os.path.samestat(target, self.path.stat()):
                raise ValueError("SEC_PERSISTENCE_TARGET_REPLACED")
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                self._guard(connection)
                return self._receipt(connection, envelope)
        except ReceiptConflict:
            raise
        except Exception as error:
            original_error.add_note(f"SEC commit reconciliation failed: {error!r}")
            return None


class ReceiptConflict(ValueError):
    def __init__(self):
        super().__init__("RECEIPT_INPUT_CONFLICT")


class LegacySecPersistence(SecCompanyPersistence):
    """Uses the existing legacy writer, with receipts in the existing sync_state."""

    def _validate_request(self, envelope, expected_version):
        if expected_version is not None or envelope.admissions:
            raise ValueError("LEGACY_SEC_REQUEST_CONTRACT")
        if any(fact.symbol != envelope.company.symbol for fact in envelope.facts):
            raise ValueError("IDENTITY_ASSIGNMENT_REJECTED")

    def _guard(self, connection):
        _require_complete_migration(connection)
        _require_legacy_fact_writer(connection)
        if connection.execute(
            "SELECT type FROM sqlite_master WHERE name='fundamental_facts'"
        ).fetchone() != ("table",):
            raise ValueError("LEGACY_SEC_SCHEMA_REQUIRED")

    def _receipt(self, connection, envelope):
        row = connection.execute(
            "SELECT value FROM sync_state WHERE source=? AND key=?",
            (LEGACY_RECEIPTS, envelope.request_id),
        ).fetchone()
        if row is None:
            return None
        if json.loads(row[0]) != {"input_hash": envelope.input_hash}:
            raise ReceiptConflict()
        return SecPersistOutcome(None, envelope.request_id, True, len(envelope.facts))

    def _write(self, connection, envelope, expected_version):
        timestamp = envelope.retrieved_at.isoformat()
        _execute_sec_company_update(
            connection, envelope.company, _fact_rows(envelope.facts), envelope.accessions, timestamp
        )
        connection.execute(
            "INSERT INTO sync_state(source,key,updated_at,value) VALUES (?,?,?,?)",
            (
                LEGACY_RECEIPTS,
                envelope.request_id,
                timestamp,
                json_bytes({"input_hash": envelope.input_hash}).decode(),
            ),
        )


class CanonicalSecPersistence(SecCompanyPersistence):
    """Uses the frozen A-V1 writer and its native versioned receipt."""

    def _validate_request(self, envelope, expected_version):
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("EXPECTED_DATASET_VERSION_REQUIRED")
        if set(envelope.admission_map()) != {(f.symbol, f.taxonomy) for f in envelope.facts}:
            raise ValueError("SOURCE_MEMBERSHIP_ADMISSION_REQUIRED")

    def _guard(self, connection):
        if connection.execute(
            "SELECT type FROM sqlite_master WHERE name='fact_contract'"
        ).fetchone() != ("table",):
            raise ValueError("CANONICAL_SEC_SCHEMA_REQUIRED")
        install_writer_guard(connection)

    def _receipt(self, connection, envelope):
        row = connection.execute(
            """SELECT r.input_hash,r.payload_hash,r.retrieved_at,r.version,
                      d.dataset_id,d.source_hash
            FROM ingestion_receipt AS r LEFT JOIN fact_dataset AS d USING (version)
            WHERE r.request_id=?""",
            (envelope.request_id,),
        ).fetchone()
        if row is None:
            return None
        # Match write_company's V1 receipt contract without invoking its mutating path.
        retrieved_at = envelope.retrieved_at.isoformat()
        writer_hash = fingerprint(
            [
                envelope.company.model_dump(),
                [dict(zip(COLS, row, strict=True)) for row in _fact_rows(envelope.facts)],
                list(envelope.accessions),
                asdict(envelope.identity),
                {str(key): asdict(rule) for key, rule in envelope.admission_map().items()},
                envelope.payload_hash,
                retrieved_at,
            ]
        )
        if tuple(row[:3]) != (writer_hash, envelope.payload_hash, retrieved_at) or tuple(
            row[4:]
        ) != (envelope.request_id, writer_hash):
            raise ReceiptConflict()
        return SecPersistOutcome(row[3], envelope.request_id, True, len(envelope.facts))

    def _write(self, connection, envelope, expected_version):
        return write_company(
            connection,
            envelope.company,
            envelope.facts,
            envelope.accessions,
            request_id=envelope.request_id,
            payload_hash=envelope.payload_hash,
            retrieved_at=envelope.retrieved_at.isoformat(),
            identity=envelope.identity,
            admissions=envelope.admission_map(),
            expected_version=expected_version,
        )
