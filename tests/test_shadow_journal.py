"""Cryptographic and SQLite immutability checks, including hostile offline tampering."""

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from test_champion_consolidation import offline as offline
from test_shadow_execution import case as case
from test_shadow_execution import config as config
from test_shadow_execution import events

from trading_system.shadow import journal as journal_module
from trading_system.shadow.contracts import ShadowError, canonical_json, digest, fingerprints
from trading_system.shadow.journal import Journal, event_hash


def hostile_edit(journal, sql, parameters=(), *, rehash=False):
    """Test-only attacker removes guards, tampers, then restores their exact SQL."""
    with sqlite3.connect(journal.path) as connection:
        connection.row_factory = sqlite3.Row
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        for trigger in triggers:
            connection.execute(f"DROP TRIGGER {trigger['name']}")
        connection.execute(sql, parameters)
        if rehash:
            previous = "0" * 64
            for row in connection.execute(
                "SELECT * FROM events ORDER BY sequence_number"
            ).fetchall():
                event = dict(row)
                event["payload"] = json.loads(event["payload"])
                event["previous_event_hash"] = previous
                previous = event_hash(event)
                connection.execute(
                    "UPDATE events SET previous_event_hash=?,event_hash=? WHERE sequence_number=?",
                    (event["previous_event_hash"], previous, event["sequence_number"]),
                )
        for trigger in triggers:
            connection.execute(trigger["sql"])


def test_genesis_hash_deterministic(tmp_path, config, monkeypatch):
    monkeypatch.setattr(journal_module, "uuid4", lambda: "fixed-test-journal-id")
    journals = [Journal(tmp_path / f"{i}.sqlite3") for i in range(2)]
    for journal in journals:
        journal.initialize(
            now=datetime(2024, 1, 1, tzinfo=UTC),
            fingerprints=fingerprints(config),
            git_identity={"local_git_commit": "fixture", "branch": "fixture"},
        )
    assert journals[0].verify().terminal_hash == journals[1].verify().terminal_hash
    assert len(journals[0].verify().terminal_hash) == 64
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})


def test_chain_validates_and_reconstructs_portfolio(case):
    runner, _, _, _ = case
    for _ in range(3):
        runner.run()
    result = runner.journal.verify()
    assert result.last_session is not None
    assert result.state["position"]["holding_days"] == 2
    assert result.state["pending"] is None
    assert len(result.committed_sessions) == 3
    assert result.terminal_hash == events(runner.journal)[-1]["event_hash"]


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("UPDATE events SET payload='{}'", "SHADOW_IMMUTABLE_UPDATE"),
        ("DELETE FROM events", "SHADOW_IMMUTABLE_DELETE"),
    ],
)
def test_update_delete_rejected_even_on_genesis(case, operation, expected):
    journal = case[0].journal
    with (
        sqlite3.connect(journal.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match=expected),
    ):
        connection.execute(operation)
    assert journal.verify().event_count == 1


def test_duplicate_sequence_and_insert_or_replace_rejected(case):
    journal = case[0].journal
    for insert in ("INSERT", "INSERT OR REPLACE"):
        with sqlite3.connect(journal.path) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"{insert} INTO events SELECT * FROM events")
    assert journal.verify().event_count == 1


@pytest.mark.parametrize(
    "edit,expected",
    [
        ("UPDATE events SET payload='{}' WHERE sequence_number=2", "event hash mismatch"),
        (
            "UPDATE events SET previous_event_hash='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE sequence_number=2",
            "previous hash linkage",
        ),
        ("DELETE FROM events WHERE sequence_number=2", "sequence continuity"),
    ],
)
def test_tampering_detected_before_run(case, edit, expected):
    runner = case[0]
    runner.run()
    hostile_edit(runner.journal, edit)
    before = runner.journal.path.read_bytes()
    with pytest.raises(ShadowError, match="SHADOW_JOURNAL_INTEGRITY_FAILURE") as exc:
        runner.journal.verify()
    assert exc.value.details["reason"] == expected
    with pytest.raises(ShadowError, match="SHADOW_JOURNAL_INTEGRITY_FAILURE"):
        runner.run()
    assert runner.journal.path.read_bytes() == before


def test_rehashed_inconsistent_state_still_rejected(case):
    runner = case[0]
    runner.run()
    commit = events(runner.journal, "SESSION_COMMITTED")[0]
    payload = commit["payload"]
    payload["portfolio_after"]["cash"] += 50
    payload["portfolio_digest"] = digest(payload["portfolio_after"])
    hostile_edit(
        runner.journal,
        "UPDATE events SET payload=? WHERE sequence_number=?",
        (canonical_json(payload), commit["sequence_number"]),
        rehash=True,
    )
    with pytest.raises(ShadowError) as exc:
        runner.journal.verify()
    assert exc.value.details["reason"] == "commit state"


def test_rehashed_duplicate_session_commit_rejected(case):
    runner = case[0]
    runner.run()
    runner.run()
    commits = events(runner.journal, "SESSION_COMMITTED")
    # The unique index rejects a duplicate even with all normal triggers removed.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        hostile_edit(
            runner.journal,
            "UPDATE events SET session=? WHERE sequence_number=?",
            (commits[0]["session"], commits[1]["sequence_number"]),
            rehash=True,
        )


def test_removed_guard_is_not_auto_repaired(case):
    runner = case[0]
    with sqlite3.connect(runner.journal.path) as connection:
        connection.execute("DROP TRIGGER events_no_update")
    with pytest.raises(ShadowError) as exc:
        runner.run()
    assert exc.value.details["reason"] == "journal SQL schema/guards"


def test_uncommitted_tail_rejected(case):
    runner = case[0]
    runner.run()
    commit = events(runner.journal, "SESSION_COMMITTED")[0]
    hostile_edit(
        runner.journal, "DELETE FROM events WHERE sequence_number=?", (commit["sequence_number"],)
    )
    with pytest.raises(ShadowError) as exc:
        runner.journal.verify()
    assert exc.value.details["reason"] == "uncommitted session tail"


def test_corrupt_sqlite_file_fails_closed_before_run(case):
    runner = case[0]
    runner.journal.path.write_bytes(b"test-only corrupt SQLite file")
    with pytest.raises(ShadowError, match="SHADOW_JOURNAL_INTEGRITY_FAILURE"):
        runner.run()
