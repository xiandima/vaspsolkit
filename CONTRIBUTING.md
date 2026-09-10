# Contributing

Thank you for improving VASPsolKit.

## Before opening a pull request

1. Do not commit `POTCAR`, VASP outputs, scheduler logs, user case directories, or institution-specific paths.
2. Add or update a regression test for every behavior change.
3. Run the full test suite locally:

   ```bash
   python -m pytest -q
   ```

4. Keep SLURM and custom-scheduler behavior explicit. Legacy PBS input is migration-only; do not reintroduce a built-in PBS runtime.
5. A scheduler action must never cancel a running job without a separate, deliberate feature and confirmation flow.

## Scope

The public package should remain chemistry-agnostic. System-specific calculations, figures, and exploratory scripts belong outside this repository.
