# F/configured portfolio-capacity research

`F/configured` with `max_positions=1` remains the frozen historical research champion. This
experiment registers exactly four research identities—`F-capacity-1`, `F-capacity-2`,
`F-capacity-3`, and `F-capacity-5`—and changes only `portfolio.max_positions`. The F screen,
candidate scores and ranking, next-session-open entry, configured Daily management, risk per
trade, maximum position size, sector limits, and cost assumptions are shared unchanged.

The levels 1/2/3/5 were pre-registered to test whether the capacity-constrained F candidate stream
contains usable simultaneous opportunities. This is a controlled historical comparison, not a
parameter sweep and not evidence that the best historical row is optimal. Capacity 1 is the frozen
control; capacities 2/3/5 are new hypotheses that would require independent Forward/OOS evidence
before any champion change.

Interpret additional return together with average exposure, maximum drawdown, turnover, modeled
cost, simultaneous-position utilization, entry-rank performance, and concentration. A return gain
that requires disproportionate exposure, drawdown, or costs is not equivalent to a free
improvement. The report never selects a winning capacity automatically.

All variants use the development-involved interval and the current-universe construction. Reports
therefore classify the requested frozen period as `DEVELOPMENT_OVERLAP`,
`CURRENT_UNIVERSE_ONLY`, and `NOT_SURVIVORSHIP_CLEAN`. Higher capacity does not remedy
survivorship bias. The frozen `champion-f-forward` target remains F/configured with
`max_positions=1`; this capacity family is not added to that forward track automatically.

## Manual PowerShell command

Run the expensive local historical comparison manually from the repository root with a fresh
output stem:

```powershell
.\.venv\Scripts\python.exe -m trading_system.cli validate-champion-f-capacity --start 2024-01-02 --end 2026-08-12 --output-stem f_configured_capacity_1_2_3_5_2024-01-02_2026-08-12_v1
```

The command includes the canonical `BASELINE` (5/0 bps), `2X_SLIPPAGE` (10/0 bps),
`3X_SLIPPAGE` (15/0 bps), and `COMMISSION_SENSITIVITY` (5/5 bps) full portfolio reruns for every
capacity. No second cost command is needed. The report also includes monthly, yearly, equal-session
thirds, symbol concentration, capacity utilization, candidate-entry-rank analysis, positions, and
execution legs. Exports refuse to overwrite any existing artifact.
