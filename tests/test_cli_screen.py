from datetime import date

from trading_system import cli
from trading_system.config import StorageConfig, load_settings
from trading_system.models.screening import ScreenReport


class EmptyScreener:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def run(self, as_of: date) -> ScreenReport:
        return ScreenReport(
            as_of=as_of,
            generated_at="2025-01-02T00:00:00+00:00",
            analyzed_count=0,
            eligible_count=0,
            records=(),
        )


def test_screen_cli_exports_without_api_credentials(tmp_path, monkeypatch, capsys) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "test.sqlite3", reports_path=tmp_path / "reports"
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "Screener", EmptyScreener)

    result = cli.main(["screen", "--as-of", "2025-01-02"])

    output = capsys.readouterr().out
    assert result == 0
    assert "No stocks passed" in output
    assert (tmp_path / "reports" / "screen_2025-01-02.csv").exists()
    assert (tmp_path / "reports" / "screen_2025-01-02.json").exists()


def test_explain_cli_reports_unknown_local_symbol(tmp_path, monkeypatch, capsys) -> None:
    load_settings.cache_clear()
    settings = load_settings()
    strategy = settings.strategy.model_copy(
        update={
            "storage": StorageConfig(
                database_path=tmp_path / "test.sqlite3", reports_path=tmp_path / "reports"
            )
        }
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda _path: settings.model_copy(update={"strategy": strategy})
    )
    monkeypatch.setattr(cli, "Screener", EmptyScreener)

    assert cli.main(["explain", "missing", "--as-of", "2025-01-02"]) == 1
    assert "MISSING" in capsys.readouterr().err
