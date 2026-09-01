# F/configured robustness validation

`F/configured` is frozen as the current historical research champion for the next robustness
validation stage. This is a research label, not a live-production designation or a claim of
proven profitability. The F screen, configured entry and management, risk sizing, portfolio
limits, and execution semantics remain unchanged.

The interval `2024-01-02` through `2026-08-12` was used during strategy development. Reports
therefore label it a development-used historical robustness period, not clean out-of-sample data.
They retain the existing warning that current tradable-universe membership can introduce
survivorship bias.

## Manual PowerShell commands

Run these commands manually from the repository root. The validation command is local-only but
historically expensive; Codex must not execute it.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli backtest --start 2024-01-02 --end 2026-08-12 --variant F --strategy configured --output-stem f_configured_baseline_2024-01-02_2026-08-12_v1
```

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-extended --target champion-f --start 2024-01-02 --end 2026-08-12 --output-stem f_configured_robustness_2024-01-02_2026-08-12_v1
```

The second command automatically runs and exports the canonical full-path cost scenarios:

- `BASELINE`: 5 bps slippage, 0 bps commission
- `2X_SLIPPAGE`: 10 bps slippage, 0 bps commission
- `3X_SLIPPAGE`: 15 bps slippage, 0 bps commission
- `COMMISSION_SENSITIVITY`: 5 bps slippage, 5 bps commission

It also exports path-preserving cost stress, post-hoc leave-one-symbol-out aggregation, symbol
concentration, calendar-month and calendar-year stability, equal-session chronological thirds,
positions, execution legs, post-exit diagnostics, and the equity curve. The CLI deliberately does
not expose an arbitrary parameter sweep or custom stress grid.

Use a new stem such as `_v2` if any named output already exists; fresh-stem exports refuse to
overwrite prior evidence.

## Expected robustness artifacts

For the validation stem above, the report directory receives:

```text
f_configured_robustness_2024-01-02_2026-08-12_v1.json
f_configured_robustness_2024-01-02_2026-08-12_v1.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_positions.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_execution_legs.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_post_exit_analysis.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_equity.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_robustness_summary.json
f_configured_robustness_2024-01-02_2026-08-12_v1_monthly.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_yearly.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_chronological_subperiods.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_symbol_concentration.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_leave_one_symbol_out.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_cost_stress.csv
f_configured_robustness_2024-01-02_2026-08-12_v1_path_preserving_cost_stress.csv
```

LOSO is explicitly marked `post_hoc_only`: it removes closed positions from aggregation without
rerunning execution, sizing, or portfolio-slot paths. Its return and profit-factor fields are
useful concentration sensitivities, but LOSO Sharpe and drawdown are intentionally left blank
rather than presented as path-faithful counterfactuals.
