# Contributing

Thank you for improving VASPsolKit.

Looking for calculation instructions? Start with the [README](README.md) or
[user guide](docs/USER_GUIDE.md). This page is for contributors.

## Development setup

From the source directory, activate an isolated Python >=3.9 environment, then run:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q vaspsolkit tests
python -m build --wheel --no-isolation
```

The test fixtures isolate activity records; subprocess tests also isolate HOME,
PATH and state directories. Do not replace fake scheduler commands with a real
cluster or use licensed production POTCAR files in tests. Offline and installation
coverage is documented in the [compatibility matrix](docs/COMPATIBILITY.md).

`tools/build_release.py` packages the committed HEAD only. To verify uncommitted
fixes, build the current working tree instead; do not treat a HEAD-only artifact as
evidence that those fixes were installed.

## User-facing documentation

Keep README focused on installation, inputs, menu steps, resuming jobs and finding
results. Put optional command variants in the user guide, cluster-specific setup
in the cluster guide, and implementation/test history in maintainer documentation.
The [documentation tests](tests/test_documentation_examples.py) validate command
examples, menu shortcuts and local links. Update examples with behavior changes.
Document only implemented behavior and distinguish offline tests from real HPC
or scientific validation.

### Preview the documentation website

The website uses Sphinx, MyST and the Read the Docs theme. Its dependencies are
separate from the calculation runtime. Use Python >=3.11 for this environment;
Python 3.12 is used in documentation CI. From the repository root:

```bash
python3.12 -m venv .venv-docs
.venv-docs/bin/python -m pip install -r docs/requirements.txt
.venv-docs/bin/python -m sphinx -n -W --keep-going -b html docs build/docs
```

Open `build/docs/index.html` in a browser. Search returns matching links without
fetched snippets, so it also works from local files. To preview over HTTP instead:

```bash
.venv-docs/bin/python -m http.server 8000 --bind 127.0.0.1 --directory build/docs
```

The chapter pages include sections of `docs/USER_GUIDE.md`; edit that source when
changing shared instructions. Keep maintainer notes out of the reader navigation.
In the development environment, check generated local links and anchors with:

```bash
VASPSOLKIT_DOCS_BUILD=build/docs python -m pytest -q tests/test_docs_site.py
```

The E-U image in the results chapter is synthetic, not a VASP calculation. To
regenerate it using the installed calculation package:

```bash
vaspsolkit postprocess --summary docs/_static/example-summary.csv --output build/example-eu
cp build/example-eu/eu_curve.png docs/_static/example-eu.png
```

The docs workflow tests the program and strictly builds the manual from the same
commit. Pull requests produce preview artifacts only; successful `main` builds
deploy to [the online manual](https://xiandima.github.io/vaspsolkit/) through GitHub
Actions. Runtime-only changes also rebuild the manual. Its versions page and
`version.json` record the source commit. Publication notes are in
[the Pages guide](docs/maintainer/GITHUB_PAGES.md).

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

See the [public release scope](docs/maintainer/PUBLIC_RELEASE_SCOPE.md),
[documentation audit](docs/DOCUMENTATION_AUDIT.md) and
[UX review](docs/UX_REVIEW.md) for maintainer background.
