# Qvasp-Inspired CLI Input Workflow Implementation Plan

> **For agentic workers:** Use task-by-task implementation and independent review. Preserve the existing dirty worktree and do not submit real jobs.

**Goal:** Keep numbered menus and direct commands consistent while supporting native KSPACING and confirmed input completion.

**Architecture:** A shared sampling policy serves preparation, identity checks, and views. A separate input-completion module validates candidates in isolation and publishes only missing files. CLI and menu actions use that same module; existing scheduler and scientific checks remain authoritative.

**Tech Stack:** Python standard library, argparse, pytest; optional local VASPKIT.

## Approved Design

The user approved keeping numbered menus alongside direct commands, asking only for missing settings, accepting KSPACING without KPOINTS, and adding `inputs` and `next` commands. Submission still requires confirmation. No qvasp source or scientific defaults are copied.

References: https://qvasp.com/Tutorials.html and https://qvasp.com/Getting%20Started.html.

## Tasks

- [x] Sampling: added `sampling.py`, integrated with preparation, copying, fingerprints and snapshots. Tested finite positive KSPACING, KGAMMA, KPOINTS precedence, and a neutral-to-charge synthetic workflow.
- [x] Completion: added `input_completion.py` and regression tests. Explicit mesh or VASPKIT supplies missing KPOINTS. A licensed library plus confirmed element mapping supplies POTCAR. Existing inputs are never replaced. Reusable settings save only on request.
- [x] Commands: added `inputs` with preview/confirmation and noninteractive options; added `next` dispatching the same recommended action used by menu 02. Added menu 14 for completion while menu 10 remains read-only.
- [x] Integration: made menu recommendations actionable when inputs are missing; retained existing commands and safety gates. Tested batch operation, cancellation, helper failures, stale inputs, UNKNOWN jobs and original-file preservation.
- [x] Documentation: updated README, compatibility notes, UX review and changelog to match tested behavior; distinguished unsupported automatic controllers from these one-step actions.
- [x] Verification: full pytest passed (781 tests), wheel installed in an isolated venv, both CLI entry points passed, and the independent review's six findings have eight passing regression cases. Real VASPKIT 1.5.0 task 103/102 smoke tests passed with synthetic parser-only data. No commit/push is requested.

## Acceptance Commands

```bash
python -m pytest -q tests/test_kspacing_workflow.py tests/test_input_completion.py tests/test_quick_commands.py
python -m pytest -q
python -m vaspsolkit inputs --help
python -m vaspsolkit next --help
```

Synthetic tests use metadata-only POTCAR fixtures and mocked scheduler/helper executables. Passing them is not a physical VASP convergence test or validation of every server/VASPKIT version.
