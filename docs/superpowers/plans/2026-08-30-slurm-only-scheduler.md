# SLURM-only Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release VASPsolKit 0.3.0 with SLURM and custom schedulers only, a version-2 configuration, `compute` automatic allocation, partition-first explicit node selection, and reliable `squeue`/`sacct` status reconciliation.

**Architecture:** Replace PBS-shaped configuration and helpers at their source rather than aliasing names. Keep scheduler transport in `scheduler.py`, SLURM script parsing and rewriting in a focused `slurm_profiles.py`, and interactive resource selection in `submission_resources.py`. Preserve the existing durable submission barrier and non-blocking workflow while changing its scheduler vocabulary and commands.

**Tech Stack:** Python 3.9+, standard library (`dataclasses`, `json`, `re`, `subprocess`, `difflib`), pytest, setuptools, GitHub Actions, SLURM CLI 25.x.

---

## File Map

- Create `vaspsolkit/slurm_profiles.py`: parse, validate, render, diff, and rewrite SLURM scripts and reusable profiles.
- Create `tests/test_vaspsolkit_config_v2.py`: version-2 defaults and migration contracts.
- Create `tests/test_vaspsolkit_slurm_profiles.py`: script/profile behavior.
- Create `tests/test_vaspsolkit_slurm_scheduler.py`: transport, node inspection, and status history behavior.
- Modify `vaspsolkit/config.py`: SLURM-only v2 data model and v1 migration.
- Modify `vaspsolkit/scheduler.py`: remove PBS runtime and complete SLURM adapter.
- Modify `vaspsolkit/submission_resources.py`: partition-first resource selection.
- Modify `vaspsolkit/scheduler_diagnostics.py`: SLURM checks and error classification.
- Modify `vaspsolkit/cli.py`: v2 initialization, migration, defaults, wording, and script review.
- Modify `vaspsolkit/case_setup.py`, `vaspsolkit/orchestrator.py`, `vaspsolkit/workflow.py`: remove PBS-specific workflow fields.
- Modify `vaspsolkit/operations/controller.py`, `actions.py`, `models.py`, `scheduler_profiles.py`, `snapshot.py`, and related tests: preserve durable operations with SLURM vocabulary.
- Delete `vaspsolkit/pbs.py`, `templates/pbs.vasp.pbs`, and `tests/test_vaspsolkit_operations_scheduler_profiles.py` after replacement tests are green.
- Modify `templates/slurm.vasp.sh`, `vaspsolkit.example.json`, `README.md`, `CONTRIBUTING.md`, `docs/maintainer/PUBLIC_RELEASE_SCOPE.md`, and examples.
- Modify `pyproject.toml`, `vaspsolkit/__init__.py`, and `CITATION.cff`: release 0.3.0 metadata.

## Task 1: Configuration v2 and explicit migration

**Files:**
- Create: `tests/test_vaspsolkit_config_v2.py`
- Modify: `vaspsolkit/config.py`
- Modify: `vaspsolkit/cli.py`
- Modify: `vaspsolkit.example.json`

- [ ] **Step 1: Write failing tests for SLURM-only defaults**

```python
from vaspsolkit.config import KitConfig, SchedulerConfig


def test_v2_defaults_target_compute_without_node_binding() -> None:
    config = KitConfig()
    assert config.config_version == 2
    assert config.scheduler == SchedulerConfig(
        kind="slurm",
        partition="compute",
        nodes=[],
        node_count=1,
        tasks=96,
        tasks_per_node=96,
        walltime="72:00:00",
        script="vasp.slurm",
        launcher="mpirun",
        executable="vasp_std",
        module_init="",
        modules=[],
        max_inflight=None,
    )


def test_scheduler_rejects_removed_pbs_kind() -> None:
    config = SchedulerConfig(kind="pbs")
    with pytest.raises(ValueError, match="slurm or custom"):
        config.validate()
```

- [ ] **Step 2: Run the defaults tests and verify RED**

Run:

```bash
python -m pytest -q tests/test_vaspsolkit_config_v2.py::test_v2_defaults_target_compute_without_node_binding tests/test_vaspsolkit_config_v2.py::test_scheduler_rejects_removed_pbs_kind
```

Expected: failures showing `config_version == 1`, missing `partition/tasks` fields, and PBS still accepted.

- [ ] **Step 3: Replace the scheduler and workflow configuration fields**

Implement these public fields in `vaspsolkit/config.py`:

```python
@dataclass
class SchedulerConfig:
    kind: str = "slurm"
    partition: str = "compute"
    nodes: List[str] = field(default_factory=list)
    node_count: int = 1
    tasks: int = 96
    tasks_per_node: int = 96
    memory: str = ""
    walltime: str = "72:00:00"
    max_inflight: Optional[int] = None
    script: str = "vasp.slurm"
    launcher: str = "mpirun"
    executable: str = "vasp_std"
    module_init: str = ""
    modules: List[str] = field(default_factory=list)
    submit_command: List[str] = field(default_factory=list)
    inspect_command: List[str] = field(default_factory=list)
    status_command: List[str] = field(default_factory=list)
    cancel_command: List[str] = field(default_factory=list)
    job_id_pattern: str = r"(?P<job_id>\S+)"

    def validate(self) -> None:
        if self.kind not in {"slurm", "custom"}:
            raise ValueError("scheduler kind must be slurm or custom")
        if self.node_count <= 0 or self.tasks <= 0 or self.tasks_per_node <= 0:
            raise ValueError("SLURM node and task counts must be positive")
        if self.tasks > self.node_count * self.tasks_per_node:
            raise ValueError("tasks exceed node_count * tasks_per_node")
        if self.nodes and len(self.nodes) != self.node_count:
            raise ValueError("explicit node count must match node_count")
```

Set `KitConfig.config_version = 2`. Remove `pbs_file` and all `qsub_*` fields from `WorkflowConfig`; use `scheduler.script` and `scheduler.tasks` at call sites.

- [ ] **Step 4: Run the defaults tests and verify GREEN**

Run the command from Step 2. Expected: both tests pass.

- [ ] **Step 5: Write failing migration tests**

```python
from vaspsolkit.config import migrate_config_data


def test_v1_slurm_config_migrates_to_v2() -> None:
    old = {
        "config_version": 1,
        "profile": "vaspsol-sweep",
        "workflow": {"qsub_ppn": 48, "qsub_walltime": "24:00:00"},
        "scheduler": {"kind": "slurm", "queue": "long", "cores": 48, "script": "job.slurm"},
    }
    migrated = migrate_config_data(old)
    assert migrated["config_version"] == 2
    assert migrated["scheduler"]["partition"] == "long"
    assert migrated["scheduler"]["tasks"] == 48
    assert migrated["scheduler"]["script"] == "job.slurm"
    assert "qsub_ppn" not in migrated["workflow"]


def test_v1_pbs_config_requires_manual_slurm_selection() -> None:
    old = {"config_version": 1, "scheduler": {"kind": "pbs", "script": "vasp.pbs"}}
    with pytest.raises(ValueError, match="select a SLURM profile"):
        migrate_config_data(old)
```

- [ ] **Step 6: Run migration tests and verify RED**

Expected: import failure because `migrate_config_data` does not exist.

- [ ] **Step 7: Implement pure-data migration and CLI preview**

Add `migrate_config_data(data: Mapping[str, Any]) -> Dict[str, Any]`. It must copy input data, map `queue -> partition`, `cores/qsub_ppn -> tasks/tasks_per_node`, `qsub_walltime -> walltime`, remove PBS-only workflow keys, and reject v1 PBS. Update `vaspsolkit migrate` to print a JSON diff and write only after confirmation or `--yes`.

- [ ] **Step 8: Run config tests and commit**

```bash
python -m pytest -q tests/test_vaspsolkit_config_v2.py
git add tests/test_vaspsolkit_config_v2.py vaspsolkit/config.py vaspsolkit/cli.py vaspsolkit.example.json
git commit -m "feat: introduce SLURM-only config v2"
```

Expected: config tests pass.

## Task 2: SLURM profile and script synchronization

**Files:**
- Create: `vaspsolkit/slurm_profiles.py`
- Create: `tests/test_vaspsolkit_slurm_profiles.py`
- Modify: `templates/slurm.vasp.sh`
- Later delete: `vaspsolkit/operations/scheduler_profiles.py`

- [ ] **Step 1: Write failing parser tests using the supplied script shape**

```python
REFERENCE = """#!/bin/bash
#SBATCH -J name
#SBATCH -N 1
#SBATCH --ntasks=96
#SBATCH --ntasks-per-node=96
#SBATCH -t 72:00:00
#SBATCH -p long
#SBATCH --nodelist=zx01
cd $SLURM_SUBMIT_DIR
ulimit -s unlimited
source /site/module-init.sh
module load vasp/site-version
mpirun -np 96 vasp_gam > vasp.log 2>&1
"""


def test_imported_local_profile_uses_standard_vasp() -> None:
    profile = import_slurm_profile(REFERENCE, name="local")
    assert profile.partition == "long"
    assert profile.tasks == 96
    assert profile.nodes == ("zx01",)
    assert profile.module_init == "/site/module-init.sh"
    assert profile.modules == ("vasp/site-version",)
    assert profile.launcher == "mpirun"
    assert profile.executable == "vasp_std"
```

- [ ] **Step 2: Verify parser RED**

Run:

```bash
python -m pytest -q tests/test_vaspsolkit_slurm_profiles.py::test_imported_local_profile_uses_standard_vasp
```

Expected: module import failure.

- [ ] **Step 3: Implement immutable SLURM profile parsing**

Create:

```python
@dataclass(frozen=True)
class SlurmProfile:
    name: str
    partition: str
    node_count: int
    tasks: int
    tasks_per_node: int
    walltime: str
    script: str
    nodes: tuple[str, ...] = ()
    launcher: str = "mpirun"
    executable: str = "vasp_std"
    module_init: str = ""
    modules: tuple[str, ...] = ()


def import_slurm_profile(script_text: str, *, name: str, script_name: str = "vasp.slurm") -> SlurmProfile:
    # Parse recognized #SBATCH directives and module setup.
    # Always set executable="vasp_std"; never copy a reference executable.
```

Use anchored regular expressions for both short and long directive forms. Reject duplicate conflicting directives and invalid positive counts.

- [ ] **Step 4: Verify parser GREEN**

Run the Step 2 command. Expected: pass.

- [ ] **Step 5: Write failing rewrite and diff tests**

```python
def test_auto_allocation_removes_inherited_nodelist_and_preserves_shell_body() -> None:
    profile = replace(import_slurm_profile(REFERENCE, name="local"), partition="compute", nodes=())
    rewritten = rewrite_slurm_resources(REFERENCE, profile)
    assert "#SBATCH -p compute" in rewritten
    assert "--nodelist" not in rewritten
    assert "ulimit -s unlimited" in rewritten
    assert "module load vasp/site-version" in rewritten
    assert "mpirun -np ${SLURM_NTASKS:-96} vasp_std > vasp.log 2>&1" in rewritten


def test_explicit_node_is_written_once() -> None:
    profile = replace(import_slurm_profile(REFERENCE, name="local"), partition="compute", nodes=("node11",))
    rewritten = rewrite_slurm_resources(REFERENCE, profile)
    assert rewritten.count("--nodelist=node11") == 1
```

- [ ] **Step 6: Verify rewrite RED**

Expected: missing `rewrite_slurm_resources`.

- [ ] **Step 7: Implement recognized-directive rewriting and unified diff**

Provide a rewrite function that normalizes CRLF only while editing, replaces each
recognized directive once, removes the node-list directive for automatic
allocation, inserts missing directives immediately after the shebang, replaces
the VASP launch command, and restores the original newline style. Expose these
signatures:

```python
def rewrite_slurm_resources(script_text: str, profile: SlurmProfile) -> str:
    if not isinstance(script_text, str):
        raise TypeError("script_text must be a string")
    profile.validate()
    newline = "\r\n" if "\r\n" in script_text else "\n"
    text = script_text.replace("\r\n", "\n")
    text = _rewrite_recognized_directives(text, profile)
    text = _rewrite_vasp_launch(text, profile)
    return text if newline == "\n" else text.replace("\n", "\r\n")

def slurm_script_diff(before: str, after: str, name: str = "vasp.slurm") -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{name}:before",
        tofile=f"{name}:after",
    ))
```

Preserve line-ending style, comments, module setup, and unrelated commands. Replace the recognized VASP launch line with the standard executable, but do not alter other shell commands.

- [ ] **Step 8: Update portable template and run tests**

The committed `templates/slurm.vasp.sh` must contain portable empty module guidance and:

```bash
mpirun -np "${SLURM_NTASKS}" vasp_std > vasp.log 2>&1
```

Run:

```bash
python -m pytest -q tests/test_vaspsolkit_slurm_profiles.py
```

Expected: all profile tests pass.

- [ ] **Step 9: Commit**

```bash
git add vaspsolkit/slurm_profiles.py tests/test_vaspsolkit_slurm_profiles.py templates/slurm.vasp.sh
git commit -m "feat: parse and synchronize SLURM scripts"
```

## Task 3: Complete the SLURM scheduler adapter

**Files:**
- Create: `tests/test_vaspsolkit_slurm_scheduler.py`
- Modify: `vaspsolkit/scheduler.py`

- [ ] **Step 1: Write failing submit-option tests**

```python
def test_submit_passes_reviewed_resources_to_sbatch() -> None:
    calls = []

    def runner(args, cwd=None):
        calls.append((list(args), cwd))
        return subprocess.CompletedProcess(args, 0, "4321;cluster\n", "")

    scheduler = SlurmScheduler(runner=runner)
    job_id = scheduler.submit(
        Path("/tmp/case"), "vasp.slurm", partition="compute",
        nodes=("node11",), node_count=1, tasks=96,
        tasks_per_node=96, walltime="72:00:00",
    )
    assert job_id == "4321"
    assert calls[0][0] == [
        "sbatch", "--parsable", "--partition", "compute",
        "--nodes", "1", "--ntasks", "96", "--ntasks-per-node", "96",
        "--time", "72:00:00", "--nodelist", "node11", "vasp.slurm",
    ]
```

- [ ] **Step 2: Verify submit RED**

Expected: current `SlurmScheduler.submit` ignores resource arguments.

- [ ] **Step 3: Implement deterministic `sbatch` arguments**

Change `SlurmScheduler.submit` to accept named resource arguments and append only non-empty options in the order asserted above. Automatic allocation must omit `--nodelist`.

- [ ] **Step 4: Verify submit GREEN**

Run the single test. Expected: pass.

- [ ] **Step 5: Write failing `squeue` then `sacct` tests**

```python
def test_status_uses_sacct_after_job_leaves_squeue() -> None:
    responses = iter([
        (0, "", ""),
        (0, "4321|COMPLETED|0:0\n", ""),
    ])
    def runner(args, cwd=None):
        returncode, stdout, stderr = next(responses)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    state = SlurmScheduler(runner=runner).status("4321")
    assert state.exists
    assert state.state == "COMPLETED"
    assert state.exit_code == "0:0"


@pytest.mark.parametrize("terminal", ["FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"])
def test_status_preserves_terminal_failure(terminal: str) -> None:
    responses = iter([(0, "", ""), (0, f"4321|{terminal}|1:0\n", "")])
    def runner(args, cwd=None):
        returncode, stdout, stderr = next(responses)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    assert SlurmScheduler(runner=runner).status("4321").state == terminal


def test_sacct_transport_failure_is_unknown_not_missing() -> None:
    responses = iter([(0, "", ""), (1, "", "controller unavailable")])
    def runner(args, cwd=None):
        returncode, stdout, stderr = next(responses)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    state = SlurmScheduler(runner=runner).status("4321")
    assert state.exists
    assert state.state == "UNKNOWN"
```

- [ ] **Step 6: Verify status RED**

Expected: current adapter reports `MISSING` when `squeue` is empty and never calls `sacct`.

- [ ] **Step 7: Add history reconciliation**

Extend `JobState` with `exit_code: str = ""`. Query exactly:

```python
["sacct", "-X", "-n", "-P", "-j", job_id, "--format=JobIDRaw,State,ExitCode"]
```

Select only the parent row whose `JobIDRaw == job_id`; ignore `.batch`, `.extern`, and array child rows. Strip suffixes such as `CANCELLED by 1000` to the canonical state while retaining raw output.

- [ ] **Step 8: Write failing partition/node inspection tests**

```python
SINFO = """node08|compute*|mixed|96|40/56/0/96
node11|compute*|idle|96|0/96/0/96
zx01|long|idle|96|0/96/0/96
"""


def test_inspect_nodes_filters_partition_and_parses_capacity() -> None:
    def runner(args, cwd=None):
        return subprocess.CompletedProcess(args, 0, SINFO, "")
    scheduler = SlurmScheduler(runner=runner)
    nodes = scheduler.inspect_nodes("compute")
    assert [node.name for node in nodes] == ["node08", "node11"]
    assert nodes[0].allocated_cores == 40
    assert nodes[0].idle_cores == 56
    assert nodes[1].state == "idle"
```

- [ ] **Step 9: Implement `SlurmNodeInfo`, partitions, and node inspection**

```python
@dataclass(frozen=True)
class SlurmNodeInfo:
    name: str
    partition: str
    state: str
    total_cores: int
    allocated_cores: int
    idle_cores: int
    other_cores: int

def inspect_partitions(self) -> List[str]:
    result = self.runner(["sinfo", "-h", "-o", "%P"], None)
    if result.returncode != 0:
        raise RuntimeError(f"sinfo failed: {result.stderr.strip()}")
    return sorted({line.strip().rstrip("*") for line in result.stdout.splitlines() if line.strip()})

def inspect_nodes(self, partition: str) -> List[SlurmNodeInfo]:
    result = self.runner(
        ["sinfo", "-N", "-h", "-p", partition, "-o", "%N|%P|%T|%c|%C"], None
    )
    if result.returncode != 0:
        raise RuntimeError(f"sinfo failed for partition {partition}: {result.stderr.strip()}")
    return parse_slurm_nodes(result.stdout, partition=partition)
```

Use `sinfo -N -h -p PARTITION -o %N|%P|%T|%c|%C`. Remove `*` from the default partition marker and de-duplicate nodes.

- [ ] **Step 10: Run scheduler tests and commit**

```bash
python -m pytest -q tests/test_vaspsolkit_slurm_scheduler.py
git add vaspsolkit/scheduler.py tests/test_vaspsolkit_slurm_scheduler.py
git commit -m "feat: complete SLURM scheduling and status history"
```

## Task 4: Partition-first interactive resource selection

**Files:**
- Modify: `vaspsolkit/submission_resources.py`
- Modify: `vaspsolkit/operations/actions.py`
- Modify: `tests/test_vaspsolkit_submission_resources.py`
- Modify: `tests/test_vaspsolkit_operations_actions.py`

- [ ] **Step 1: Replace PBS fixtures with failing partition-first tests**

```python
def test_specified_mode_selects_partition_before_node() -> None:
    class Scheduler:
        def inspect_partitions(self):
            return ["compute", "long"]
        def inspect_nodes(self, partition):
            assert partition == "compute"
            return [SlurmNodeInfo("node11", "compute", "idle", 96, 0, 96, 0)]

    answers = iter(["3", "compute", "node11", "96", "n"])
    selected = prompt_submission_resources(
        KitConfig(), input_fn=lambda _: next(answers),
        output=lambda _: None, scheduler_factory=lambda _: Scheduler(),
    )
    assert selected.partition == "compute"
    assert selected.nodes == ("node11",)
    assert selected.tasks == 96


def test_node_outside_partition_is_rejected_and_prompt_restarts() -> None:
    # Scheduler returns node11 for compute; user enters zx01 then cancels.
    class Scheduler:
        def inspect_partitions(self):
            return ["compute"]
        def inspect_nodes(self, partition):
            return [SlurmNodeInfo("node11", "compute", "idle", 96, 0, 96, 0)]
    answers = iter(["3", "compute", "zx01", "0"])
    output = []
    selected = prompt_submission_resources(
        KitConfig(), input_fn=lambda _: next(answers),
        output=output.append, scheduler_factory=lambda _: Scheduler(),
    )
    assert selected is None
    assert any("不属于分区 compute" in line for line in output)
```

- [ ] **Step 2: Verify resource selection RED**

Expected: specified nodes are limited to PBS and `ResourceRequest` has no partition/tasks fields.

- [ ] **Step 3: Make `ResourceRequest` SLURM-native**

Use these fields:

```python
@dataclass(frozen=True)
class ResourceRequest:
    allocation: str
    partition: str
    nodes: tuple[str, ...]
    node_count: int
    tasks: int
    tasks_per_node: int
    walltime: str
    script: str
    persist: bool = False
```

Validation must enforce automatic allocation has no nodes, specified allocation has exactly `node_count` nodes, and task capacity is valid.

- [ ] **Step 4: Implement partition-first selection**

Display partition choices returned by `inspect_partitions()`, then nodes from `inspect_nodes(partition)`. Reject states containing `down`, `drain`, `fail`, `maint`, `unknown`, or `reserved`. Validate requested tasks against `idle_cores` for one-node explicit selection.

- [ ] **Step 5: Update CLI resource flags**

Generate and parse:

```text
--resource-allocation specified
--resource-partition compute
--resource-node node11
--resource-node-count 1
--resource-tasks 96
--resource-tasks-per-node 96
```

Remove `--resource-cores` from public CLI behavior.

- [ ] **Step 6: Run focused tests and commit**

```bash
python -m pytest -q tests/test_vaspsolkit_submission_resources.py tests/test_vaspsolkit_operations_actions.py
git add vaspsolkit/submission_resources.py vaspsolkit/operations/actions.py tests/test_vaspsolkit_submission_resources.py tests/test_vaspsolkit_operations_actions.py
git commit -m "feat: add partition-first SLURM resource selection"
```

## Task 5: Preserve the durable workflow with SLURM resources

**Files:**
- Modify: `vaspsolkit/case_setup.py`
- Modify: `vaspsolkit/orchestrator.py`
- Modify: `vaspsolkit/workflow.py`
- Modify: `vaspsolkit/operations/controller.py`
- Modify: `vaspsolkit/operations/models.py`
- Modify: `vaspsolkit/operations/snapshot.py`
- Modify: workflow and operations tests that still construct PBS configs

- [ ] **Step 1: Add failing end-to-end dry-run tests**

```python
def test_slurm_v2_prepare_submit_and_check_dry_run(tmp_path: Path) -> None:
    case = tmp_path / "case"
    case.mkdir()
    for name in ("POSCAR", "INCAR", "POTCAR", "KPOINTS"):
        (case / name).write_text("input\n", encoding="utf-8")
    (case / "vasp.slurm").write_text(
        "#!/bin/bash\n#SBATCH -p compute\n#SBATCH -n 96\nmpirun -np 96 vasp_std\n",
        encoding="utf-8",
    )
    config = KitConfig()
    write_kit_config(case / "vaspsolkit.json", config)
    prepare_neutral(case, config)

    def forbidden_runner(args, cwd=None):
        raise AssertionError("dry-run must not contact SLURM")

    result = submit_neutral_job(
        case, config, scheduler=SlurmScheduler(runner=forbidden_runner),
        confirmed=True, dry_run=True, require_prepared=True,
    )
    assert result == "DRY-RUN:neutral"
    assert (case / "vasp.slurm").is_file()
    assert not (case / "vasp.pbs").exists()
```

- [ ] **Step 2: Verify workflow RED**

Expected: code reads `workflow.pbs_file`, writes qsub summaries, or expects `cores/queue`.

- [ ] **Step 3: Replace workflow script/resource access**

At every preparation and submission boundary, use `config.scheduler.script`, `partition`, `node_count`, `tasks`, and `tasks_per_node`. Submission summaries must say `sbatch × N`. Durable receipt fingerprints must include all SLURM resource fields so changing a partition or node invalidates a stale plan.

- [ ] **Step 4: Preserve fail-closed semantics**

Update user-facing barriers to say “不要再次 sbatch”. Keep these invariants unchanged:

- existing `SUBMITTING` or `ACCEPTED` receipt blocks submission;
- scheduler `UNKNOWN` never causes resubmission;
- script/config fingerprint mismatch blocks execution;
- dry-run does not create a durable receipt.

- [ ] **Step 5: Replace snapshot script detection**

Import `import_slurm_profile` in `operations/snapshot.py`. Script validation requires either a `#SBATCH` directive or an executable shell command. Snapshot scheduler views expose `partition`, `tasks`, `tasks_per_node`, and `resource_syntax="slurm"`.

- [ ] **Step 6: Run workflow/operations tests**

```bash
python -m pytest -q \
  tests/test_vaspsolkit_case_setup.py \
  tests/test_vaspsolkit_operations_neutral_submit.py \
  tests/test_vaspsolkit_operations_charge_submit.py \
  tests/test_vaspsolkit_operations_snapshot.py \
  tests/test_vaspsolkit_operations_polling.py
```

Expected: all pass with SLURM fixtures.

- [ ] **Step 7: Commit**

```bash
git add vaspsolkit/case_setup.py vaspsolkit/orchestrator.py vaspsolkit/workflow.py vaspsolkit/operations tests
git commit -m "refactor: carry SLURM resources through durable workflow"
```

## Task 6: SLURM diagnostics, repair, and initialization

**Files:**
- Modify: `vaspsolkit/scheduler_diagnostics.py`
- Modify: `vaspsolkit/cli.py`
- Modify: `vaspsolkit/guide_model.py`
- Modify: `vaspsolkit/terminal_menu_renderer.py`
- Modify: related guide, menu, onboarding, and file-action tests

- [ ] **Step 1: Write failing diagnostic tests**

```python
def test_scheduler_checks_require_slurm_commands(tmp_path: Path) -> None:
    checks = check_scheduler_script(tmp_path, SchedulerConfig(), which=fake_which)
    command_checks = {check.key: check.status for check in checks}
    assert set(command_checks) >= {
        "command-sbatch", "command-squeue", "command-sacct",
        "command-sinfo", "command-scancel",
    }


def test_submit_error_uses_slurm_language() -> None:
    info = classify_submit_error(RuntimeError("sbatch: Batch script contains DOS line breaks"))
    assert info.title == "SLURM submit failed"
    assert info.repair_action == "fix-line-endings"
```

- [ ] **Step 2: Verify diagnostic RED**

Expected: checks and suggestions still mention PBS/qsub.

- [ ] **Step 3: Implement SLURM diagnostic checks**

Check command availability through an injectable `which`, script/resource consistency through `import_slurm_profile`, and selected node membership through scheduler inspection when requested. Classify common errors including invalid partition, invalid node, requested node configuration unavailable, association/QOS rejection, time limit, and DOS line endings.

- [ ] **Step 4: Add confirmation-gated script sync repair**

Add action `sync-slurm-resources`. It reads the script, calls `rewrite_slurm_resources`, returns a diff in preview mode, and writes atomically only with `confirmed=True`.

- [ ] **Step 5: Update initialization flow**

`vaspsolkit init` offers only `slurm` and `custom`. For SLURM it offers:

1. portable profile;
2. import local `vasp.slurm` profile;
3. manual values.

Imported profiles force `executable=vasp_std`, show the resulting config/script diff, and require confirmation. Default non-interactive initialization writes v2 `compute/96/72:00:00` values.

- [ ] **Step 6: Run diagnostic and UI tests**

```bash
python -m pytest -q \
  tests/test_vaspsolkit_guide.py \
  tests/test_vaspsolkit_interactive_menu.py \
  tests/test_vaspsolkit_operations_file_actions.py \
  tests/test_vaspsolkit_operations_onboarding.py \
  tests/test_vaspsolkit_terminal_menu_renderer.py
```

- [ ] **Step 7: Commit**

```bash
git add vaspsolkit/scheduler_diagnostics.py vaspsolkit/cli.py vaspsolkit/guide_model.py vaspsolkit/terminal_menu_renderer.py tests
git commit -m "feat: add SLURM diagnostics and profile onboarding"
```

## Task 7: Remove PBS runtime and rewrite public documentation

**Files:**
- Delete: `vaspsolkit/pbs.py`
- Delete: `vaspsolkit/operations/scheduler_profiles.py`
- Delete: `templates/pbs.vasp.pbs`
- Delete: `tests/test_vaspsolkit_operations_scheduler_profiles.py`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/maintainer/PUBLIC_RELEASE_SCOPE.md`
- Modify: `examples/minimal-case/README.md`
- Modify: `.github/workflows/tests.yml`
- Modify: `tests/test_public_release.py`

- [ ] **Step 1: Write a failing public-boundary test**

```python
def test_runtime_tree_is_slurm_only() -> None:
    forbidden = ("PBSScheduler", "PBSNodeInfo", "qsub", "qstat", "qdel", "pbsnodes", "#PBS", "vasp.pbs")
    violations = []
    for path in Path("vaspsolkit").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{path}:{token}")
    assert violations == []
    assert not Path("vaspsolkit/pbs.py").exists()
    assert not Path("templates/pbs.vasp.pbs").exists()
```

- [ ] **Step 2: Run the boundary test and verify RED**

Expected: many PBS runtime violations.

- [ ] **Step 3: Delete PBS implementation and replace residual vocabulary**

Remove PBS imports, branches, data classes, templates, tests, command summaries, and help text. Mentions are allowed only in migration documentation/tests that explain rejection of legacy configs.

- [ ] **Step 4: Rewrite README workflow examples**

Document exact commands for:

```bash
vaspsolkit init --scheduler slurm --script vasp.slurm
vaspsolkit plan
vaspsolkit prepare-neutral
vaspsolkit submit-neutral
vaspsolkit status
vaspsolkit prepare-charge
vaspsolkit submit
vaspsolkit postprocess
```

Explain automatic `compute` allocation, partition-first explicit selection, `squeue` plus `sacct`, `vasp_std`, local profile import, and the v1 PBS migration stop.

- [ ] **Step 5: Update public CI gate**

Keep build/artifact scans and add the SLURM-only boundary test to the release job. Do not make docs fail merely for explaining legacy PBS migration.

- [ ] **Step 6: Run boundary and documentation tests**

```bash
python -m pytest -q tests/test_public_release.py tests/test_vaspsolkit_guide.py
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove PBS runtime and documentation"
```

## Task 8: Release 0.3.0 verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `vaspsolkit/__init__.py`
- Modify: `CITATION.cff`
- Modify: `tests/test_public_release.py`

- [ ] **Step 1: Update the version consistency test to expect 0.3.0**

```python
def test_release_version_is_slurm_v03() -> None:
    assert project_version() == package_version() == citation_version() == "0.3.0"
```

- [ ] **Step 2: Verify version test RED**

Expected: current version is 0.2.1.

- [ ] **Step 3: Update release metadata**

Set version `0.3.0` in all three files and set `CITATION.cff` release date to the actual release date. Do not add a repository URL or author identity different from the existing public metadata unless supplied by the maintainer.

- [ ] **Step 4: Run the full suite from a writable state root**

```bash
VASPSOLKIT_STATE_ROOT=/tmp/vaspsolkit-v03-state \
PYTHONDONTWRITEBYTECODE=1 \
python -m pytest -q -p no:cacheprovider
```

Expected: all tests pass with no warnings attributable to VASPsolKit.

- [ ] **Step 5: Compile and check the diff**

```bash
python -m compileall -q vaspsolkit tests
git diff --check
rg -n "PBSScheduler|PBSNodeInfo|qsub|qstat|qdel|pbsnodes|vasp\.pbs" vaspsolkit templates README.md
```

Expected: compile and diff checks pass; the final search returns no runtime/public-workflow matches.

- [ ] **Step 6: Build from Git HEAD and scan artifacts**

```bash
python tools/build_release.py --outdir /tmp/vaspsolkit-0.3.0-release
PUBLIC_RELEASE_ARTIFACTS=/tmp/vaspsolkit-0.3.0-release \
python -m pytest -q tests/test_public_release.py
```

Expected: one wheel and one sdist; artifact scan passes.

- [ ] **Step 7: Install the wheel in isolation**

```bash
python -m pip install --no-deps --target /tmp/vaspsolkit-0.3.0-install \
  /tmp/vaspsolkit-0.3.0-release/vaspsolkit-0.3.0-py3-none-any.whl
PYTHONPATH=/tmp/vaspsolkit-0.3.0-install python -m vaspsolkit --help
```

Expected: help lists SLURM/custom configuration and contains no PBS option.

- [ ] **Step 8: Run live read-only SLURM smoke checks**

```bash
sinfo -N -h -p compute -o '%N|%P|%T|%c|%C'
squeue -h -o '%i|%T|%j|%P|%D|%C|%R'
```

Then run `vaspsolkit plan` against a temporary example case without submitting. Expected: compute nodes are parsed, explicit node choices are partition-consistent, and no scheduler mutation occurs.

- [ ] **Step 9: Commit release metadata**

```bash
git add pyproject.toml vaspsolkit/__init__.py CITATION.cff tests/test_public_release.py
git commit -m "chore: prepare VASPsolKit 0.3.0"
```

- [ ] **Step 10: Request code review before push**

Review specifically for duplicate submission risk, `UNKNOWN` handling, config migration data loss, script rewrite scope, node/partition mismatch, and residual PBS runtime references. Fix findings with failing regression tests before creating a tag or pushing.
