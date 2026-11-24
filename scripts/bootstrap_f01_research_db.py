#!/usr/bin/env python3
"""Preflight or create an isolated, empty F01 Canonical A-V1 research dataset."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

# The default preflight must not write bytecode to the checkout.
sys.dont_write_bytecode = True

from f01_research_evidence import REPO, repository_evidence  # noqa: E402

from trading_system.data.canonical_bootstrap import (  # noqa: E402
    MANIFEST_MAX_BYTES,
    BootstrapBlocked,
    bootstrap,
    preflight,
)


def _code_evidence() -> dict:
    branch = subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "-c",
            f"safe.directory={REPO.as_posix()}",
            "branch",
            "--show-current",
        ],
        cwd=REPO,
        text=True,
    ).strip()
    if branch != "SEC-integration":
        raise BootstrapBlocked("BOOTSTRAP_WRONG_CANDIDATE_BRANCH")
    return repository_evidence(REPO)


def _read_manifest(path: Path) -> bytes:
    path = path.absolute()
    if path.suffix.lower() != ".json" or path.resolve(strict=True) != path:
        raise BootstrapBlocked("BOOTSTRAP_PLAIN_JSON_MANIFEST_REQUIRED")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or info.st_nlink != 1
        or info.st_size > MANIFEST_MAX_BYTES
    ):
        raise BootstrapBlocked("BOOTSTRAP_PLAIN_JSON_MANIFEST_REQUIRED")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(info, opened) or opened.st_nlink != 1:
            raise BootstrapBlocked("BOOTSTRAP_MANIFEST_FILE_REPLACED")
        return stream.read(MANIFEST_MAX_BYTES + 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True, help="New absolute research DB path")
    parser.add_argument("--manifest", type=Path, required=True, help="Reviewed bootstrap JSON")
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--expect-code-fingerprint", help="Reviewed candidate from a preflight")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-create-research-only", action="store_true")
    args = parser.parse_args(argv)
    if args.execute != args.confirm_create_research_only or (
        args.execute and not args.expect_code_fingerprint
    ):
        parser.error(
            "Execution requires --execute, --confirm-create-research-only "
            "and --expect-code-fingerprint together"
        )

    try:
        manifest = _read_manifest(args.manifest)
        evidence = _code_evidence()
        code = evidence["code_fingerprint"]
        if args.expect_code_fingerprint and args.expect_code_fingerprint != code:
            raise BootstrapBlocked("BOOTSTRAP_CODE_FINGERPRINT_MISMATCH")
        if args.execute:
            result = bootstrap(
                args.target,
                manifest=manifest,
                manifest_sha256=args.manifest_sha256,
                expected_code_fingerprint=args.expect_code_fingerprint,
                verify_code=lambda: _code_evidence()["code_fingerprint"],
                authorized=True,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        else:
            plan = preflight(
                args.target,
                manifest=manifest,
                manifest_sha256=args.manifest_sha256,
                code_fingerprint=code,
            )
            result = {
                "status": "PREFLIGHT_ONLY",
                "head": evidence["git_head"],
                "code_inventory_files": len(evidence["files"]),
                "runtime_packages": len(evidence["runtime_packages"]),
                **plan.record(),
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except BootstrapBlocked as error:
        print(f"Bootstrap blocked: {error}", file=sys.stderr)
        return 3
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"Bootstrap failed: {error}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("Bootstrap interrupted. Inspect the target and staging paths.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
