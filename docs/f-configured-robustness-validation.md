# F/configured robustness validation

`F/configured` is frozen as the current historical research champion for the next robustness
validation stage. This is a research label, not a live-production designation or a claim of
proven profitability. The F screen, configured entry and management, risk sizing, portfolio
limits, and execution semantics remain unchanged.

The interval `2024-01-02` through `2026-08-12` was used during strategy development. Reports
therefore label it a development-used historical robustness period, not clean out-of-sample data.
The development cutoff is frozen centrally at `2026-08-12`; `champion-f-forward` refuses a start
on or before that date.

Universe survivorship and temporal OOS are independent. Historical screens start from the current
Alpaca active/tradable, SEC-identified company set. Local bars are non-authoritative historical
evidence, and SEC CIK data establishes identity rather than universe membership. The repository
does not store authoritative listing/delisting intervals or ticker history, so reports classify the
universe as `CURRENT_UNIVERSE_ONLY` and `NOT_SURVIVORSHIP_CLEAN`. True PIT-universe remediation
requires a separate authoritative data-acquisition project.

The normal robustness artifact retains `post_hoc_only` LOSO: it subtracts completed positions and
does not reconstruct allocation. The separate `validate-champion-f-loso` command is
`COUNTERFACTUAL_RERUN_LOSO`: each selected symbol is removed from cached PIT screens before
ranking/allocation and the complete unchanged F/configured portfolio path is rerun.

## Manual PowerShell commands

Run these commands manually from the repository root. The validation command is local-only but
historically expensive; Codex must not execute it.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli audit-universe-provenance --start 2024-01-02 --end 2026-08-12 --output-stem f_configured_universe_audit_2024-01-02_2026-08-12_v1
```

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-extended --target champion-f --start 2024-01-02 --end 2026-08-12 --output-stem f_configured_robustness_2024-01-02_2026-08-12_v2
```

The following command is expensive: by default it reruns once for every symbol traded by the
baseline. Use `--symbols NVDA,FTI,MU` for a targeted manual check.

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-champion-f-loso --start 2024-01-02 --end 2026-08-12 --output-stem f_configured_exact_loso_2024-01-02_2026-08-12_v1
```

Future snapshots must use a new stem. A clean temporal start is `2026-08-13`, but a short sample is
not automatically adequate validation.

```powershell
$EndDate = Get-Date -Format 'yyyy-MM-dd'
.\.venv\Scripts\python.exe -m trading_system.cli validate-extended --target champion-f-forward --start 2026-08-13 --end $EndDate --output-stem "champion_f_forward_2026-08-13_${EndDate}_v1"
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

For the robustness stem above, the report directory receives:

```text
f_configured_robustness_2024-01-02_2026-08-12_v2.json
f_configured_robustness_2024-01-02_2026-08-12_v2.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_positions.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_execution_legs.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_post_exit_analysis.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_equity.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_robustness_summary.json
f_configured_robustness_2024-01-02_2026-08-12_v2_monthly.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_yearly.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_chronological_subperiods.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_symbol_concentration.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_leave_one_symbol_out.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_cost_stress.csv
f_configured_robustness_2024-01-02_2026-08-12_v2_path_preserving_cost_stress.csv
```

The universe audit produces `f_configured_universe_audit_2024-01-02_2026-08-12_v1_universe_provenance.json`.
Exact LOSO produces the suffixes `_exact_counterfactual_loso.json` and
`_exact_counterfactual_loso.csv`. A forward target produces the normal robustness bundle under
its forward snapshot stem; its `_robustness_summary.json` contains the cutoff, overlap guard,
sample size, temporal OOS status, and survivorship classification.

LOSO is explicitly marked `post_hoc_only`: it removes closed positions from aggregation without
rerunning execution, sizing, or portfolio-slot paths. Its return and profit-factor fields are
useful concentration sensitivities, but LOSO Sharpe and drawdown are intentionally left blank
rather than presented as path-faithful counterfactuals.
