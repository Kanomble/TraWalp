"""Repository and runtime evidence for offline F01 research candidates."""

import hashlib
import importlib.metadata
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from trading_system.data.canonical_facts import fingerprint  # noqa: E402


def repository_evidence(root):
    head = subprocess.run(
        ["git", "-c", "safe.directory=" + root.as_posix(), "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    files, consumers = {}, []
    candidates = sorted(
        {
            p
            for folder in ("src", "scripts", "config")
            for p in (root / folder).rglob("*")
            if p.is_file()
            and p.suffix in (".py", ".yaml", ".yml", ".sql", ".json")
            and "__pycache__" not in p.parts
        }
    )
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        files[relative] = hashlib.sha256(raw).hexdigest()
        lines = [
            i
            for i, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1)
            if any(
                token in line
                for token in ("fundamental_facts", "canonical_fact", "source_occurrence")
            )
        ]
        if lines:
            consumers.append(
                dict(
                    path=relative,
                    sha256=files[relative],
                    lines=lines,
                    classification="repository SQL/schema consumer candidate",
                )
            )
    for path in (root / "pyproject.toml", root / "requirements.txt", root / ".env"):
        if path.exists():
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    packages = {
        d.metadata["Name"]: d.version
        for d in importlib.metadata.distributions()
        if d.metadata["Name"]
    }
    return dict(
        git_head=head,
        files=files,
        code_fingerprint=fingerprint([files, packages, sys.version, sqlite3.sqlite_version]),
        runtime_packages=packages,
        consumers=consumers,
        external_coverage="requires explicit reviewed declaration",
    )
