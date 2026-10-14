"""Offline contract checks for immutable evidence and exact security bindings."""

from dataclasses import replace
from datetime import date

import pytest
from test_security_lifecycle import db as db
from test_security_lifecycle import epoch, identity
from test_security_lifecycle import offline as offline

from trading_system.data.security_identity import (
    UNKNOWN,
    SymbolIdentityEpoch,
    _matches_current,
    current_identity_epoch,
    epoch_status,
    identity_epoch,
)


def test_default_ingestion_is_idempotent_and_preserves_original_audit_fields(db):
    original = epoch()
    assert db.upsert_symbol_identity_epochs([original]) == 1
    saved = db.symbol_identity_epochs(["NEW"])
    assert db.upsert_symbol_identity_epochs([original]) == 0
    assert (
        db.upsert_symbol_identity_epochs(
            [replace(original, observed_at="2026-10-15T00:00:00+00:00")]
        )
        == 0
    )
    assert db.symbol_identity_epochs(["NEW"]) == saved
    assert len(saved["NEW"]) == 1


def test_default_ingestion_rejects_weaker_evidence_without_replacing_verified_row(db):
    original = epoch()
    db.upsert_symbol_identity_epochs([original])
    saved = db.symbol_identity_epochs(["NEW"])
    with pytest.raises(ValueError, match="IDENTITY_EVIDENCE_CONFLICT"):
        db.upsert_symbol_identity_epochs(
            [replace(original, verified=False, stable_security_identity=None)]
        )
    assert db.symbol_identity_epochs(["NEW"]) == saved


def test_protection_cannot_be_disabled(db):
    db.upsert_symbol_identity_epochs([epoch()])
    saved = db.symbol_identity_epochs(["NEW"])
    with pytest.raises(ValueError, match="IDENTITY_EPOCH_UNPROTECTED_INGESTION_NOT_ALLOWED"):
        db.upsert_symbol_identity_epochs([epoch(cik="0000000002")], protect_existing=False)
    assert db.symbol_identity_epochs(["NEW"]) == saved


@pytest.mark.parametrize(
    "changes",
    [
        {"stable_security_identity": None},
        {"stable_security_identity": " "},
        {"stable_security_identity": "SHARE_CLASS_FIGI:"},
        {"cik": None},
    ],
)
def test_verified_epoch_requires_both_issuer_and_concrete_security_identity(changes):
    with pytest.raises(ValueError, match="IDENTITY_EPOCH_SECURITY_IDENTITY_REQUIRED"):
        SymbolIdentityEpoch.from_record({**epoch().record(), **changes})


def test_matches_current_rejects_cik_only_even_with_same_symbol_and_issuer():
    legacy = replace(epoch(), stable_security_identity=None, verified=False)
    assert not _matches_current(legacy, {"symbol": "NEW", "cik": legacy.cik})


@pytest.mark.parametrize(
    "stable,current",
    [
        ("SHARE_CLASS_FIGI:CLASS1", {"share_class_figi": "CLASS1"}),
        ("COMPOSITE_FIGI:FIGI1", {"composite_figi": "FIGI1"}),
        ("REVIEWED_SECURITY:SEC1", {"stable_security_identity": "REVIEWED_SECURITY:SEC1"}),
    ],
)
def test_exact_security_identifiers_bind_current_epoch(stable, current):
    record = epoch(stable=stable)
    binding = {"symbol": "NEW", "cik": record.cik, **current}
    assert _matches_current(record, binding)
    assert current_identity_epoch(binding, [record.record()]) == record.record()
    assert identity_epoch({**binding, "identity_epoch": record.record()}) == record


def test_same_cik_does_not_bind_a_different_share_class():
    record = epoch(stable="SHARE_CLASS_FIGI:OTHER")
    assert not _matches_current(record, identity())
    assert current_identity_epoch(identity(), [record.record()]) is None
    assert identity_epoch({**identity(), "identity_epoch": record.record()}) is None


def test_current_reference_without_stable_security_id_cannot_bind_verified_epoch():
    record = epoch()
    assert not _matches_current(record, {"symbol": "NEW", "cik": record.cik})


def test_legacy_cik_only_row_is_unresolved_without_database_mutation(db):
    # Reproduce a row permitted by the pre-hardening schema in this temporary DB.
    legacy = {**epoch().record(), "stable_security_identity": None}
    columns = tuple(legacy)
    with db.connect() as connection:
        connection.execute(
            f"INSERT INTO symbol_identity_epochs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(legacy[k] for k in columns),
        )
    saved = db.symbol_identity_epochs(["NEW"])
    assert current_identity_epoch(identity(), saved["NEW"]) is None
    binding = {**identity(), "identity_epoch": saved["NEW"][0]}
    assert identity_epoch(binding) is None
    assert epoch_status(binding, date(2025, 7, 1)) == UNKNOWN
    assert db.symbol_identity_epochs(["NEW"]) == saved
