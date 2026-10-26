# Shadow runtime snapshot

Keep compatible dependency ranges in `pyproject.toml`; no packaging migration or
automatic installation is required. Before choosing the forward version, use the
same interpreter that will run Shadow:

```powershell
python --version
python -m pip freeze --all > reports/shadow-runtime-freeze.txt
```

Review and retain this full installed-package snapshot together with the local
commit and hardening/preflight reports. Recreate a separate environment from it
when needed, then run the focused tests and preflight again. This is an installed
environment record, not an artifact-hash lockfile or a guarantee of wheel
availability. No environment is reinstalled automatically.

`shadow.contracts.runtime_manifest()` deterministically captures Python,
alpaca-py, requests, pandas, numpy, pydantic, PyYAML and exchange-calendars. The
manifest is journaled at successful inception and participates in the execution
and separate ingestion fingerprints. An incompatible or merely different
installed version stops an existing journal through drift detection.
