"""Small local Shadow CLI; never constructs normal CLI synchronizers/settings."""

from __future__ import annotations

import json
import sqlite3
import sys

from trading_system.backtest.data_integrity import DataIntegrityError
from trading_system.config import DEFAULT_STRATEGY_PATH, StrategyConfig, _read_yaml
from trading_system.shadow.contracts import ShadowError, normalized
from trading_system.shadow.inputs import source_error
from trading_system.shadow.journal import Journal
from trading_system.shadow.runner import ShadowRunner


def command(args) -> int:
    try:
        config = StrategyConfig.model_validate(_read_yaml(args.config or DEFAULT_STRATEGY_PATH))
        journal_path = args.journal or config.storage.database_path.with_name("shadow_f.sqlite3")
        runner = ShadowRunner(Journal(journal_path), config.storage.database_path, config)
        if args.command == "shadow-f-init":
            inception = runner.initialize(capital=args.initial_capital)
            output = {
                "status": "SHADOW_INITIALIZED",
                **{
                    key: inception[key]
                    for key in (
                        "journal_id",
                        "created_at_utc",
                        "initial_capital",
                        "production_label",
                        "latest_completed_official_session_at_inception",
                        "strategy_fingerprint",
                        "economic_config_fingerprint",
                        "engine_fingerprint",
                        "journal_schema_version",
                    )
                },
            }
        elif args.command == "shadow-f-run":
            result = runner.run(args.session)
            output = {key: value for key, value in result.items() if key != "portfolio"}
        elif args.command == "shadow-f-status":
            output = runner.status()
        else:
            result = runner.journal.verify()
            output = {
                "status": "VALID",
                "journal_id": result.inception["journal_id"],
                "event_count": result.event_count,
                "terminal_event_hash": result.terminal_hash,
            }
        print(json.dumps(normalized(output), indent=2, sort_keys=True))
        return 0
    except (ShadowError, DataIntegrityError) as exc:
        print(json.dumps(exc.as_dict(), sort_keys=True), file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        error = source_error(exc)
        output = (
            error.as_dict()
            if isinstance(error, ShadowError)
            else {"status": "SHADOW_REFUSED", "reason": str(error)}
        )
        print(json.dumps(output, sort_keys=True), file=sys.stderr)
        return 1
