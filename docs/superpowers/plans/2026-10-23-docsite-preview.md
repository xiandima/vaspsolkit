# Documentation Preview Implementation Plan

**Goal:** Build a local, readable Chinese user manual using the approved Sphinx/Read the Docs direction. Do not publish or submit calculations.

**Architecture:** Existing Markdown remains the source of truth. Chapter pages include selected portions of the user guide; new first-run and result-example pages give readers a concrete entry point. Sphinx builds isolated HTML with local assets and a searchable navigation tree.

**Tech Stack:** Sphinx, MyST, sphinx-rtd-theme, sphinx-copybutton; a separate Python 3.12 environment for documentation and Playwright for browser verification.

## Tasks

- [x] Add failing documentation-site tests: configuration, chapter navigation, example five-point data, source packaging and optional built-HTML link checks.
- [x] Add docs/conf.py, pinned docs/requirements.txt, chapter pages and restrained responsive styling. Reuse existing Markdown; exclude maintainer history from the reader navigation.
- [x] Generate a synthetic five-point E-U example with the existing postprocessor; publish only its clearly labelled example data and image.
- [x] Add a build-only documentation CI workflow, contributor preview instructions, and a README entry. No Pages deployment in this iteration.
- [x] Build with `python -m sphinx -n -W --keep-going -b html docs build/docs`; resolve warnings and inspect desktop/mobile screenshots, navigation, copy buttons and search.
- [x] Run regression tests, sync only this iteration's patch to the public checkout, and provide the local HTML entry and preview image. Preserve all existing work and calculation data.
