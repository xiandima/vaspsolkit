# VASPsolKit SLURM-only scheduler design

Date: 2026-08-30
Target release: 0.3.0
Status: approved for implementation

## Goal

Replace the built-in PBS workflow with a SLURM-native implementation. VASPsolKit
will support SLURM and user-defined custom schedulers only. The default server
profile targets the local `compute` partition while preserving explicit partition
and node selection.

The user-supplied `vasp.slurm` reference script defines the site's resource and
module conventions, but its non-standard VASP executable is not reused.
VASPsolKit will launch the standard executable with:

```bash
mpirun -np {tasks} vasp_std
```

## Scope

The change covers configuration, initialization, script handling, scheduler
commands, resource selection, status reconciliation, diagnostics, repair,
documentation, examples, and tests.

PBS classes, templates, configuration fields, CLI wording, diagnostics, and test
fixtures will be removed. `custom` remains available for non-SLURM systems.

## Configuration v2

`config_version` becomes `2`. The scheduler model uses SLURM terminology:

```json
{
  "scheduler": {
    "kind": "slurm",
    "partition": "compute",
    "nodes": [],
    "node_count": 1,
    "tasks": 96,
    "tasks_per_node": 96,
    "walltime": "72:00:00",
    "script": "vasp.slurm",
    "launcher": "mpirun",
    "executable": "vasp_std",
    "module_init": "",
    "modules": [],
    "max_inflight": null
  }
}
```

Site-specific module initialization and module names live in a user-selected
profile or case configuration, not as assumptions in the generic scheduler core.
The committed portable profile leaves module fields empty. Importing the local
server profile extracts these values from the user-supplied reference script and
writes them only to the user's local configuration.

The workflow model replaces PBS-specific names such as `pbs_file`, `qsub_ppn`,
`qsub_queue`, `qsub_min_node`, and `qsub_walltime` with scheduler-neutral or
SLURM-specific fields. The submission script is sourced from
`scheduler.script`; workflow code no longer keeps a second script field.

### Migration

- A v1 configuration whose scheduler is already `slurm` is migrated to v2 with
  explicit defaults and a written preview.
- A v1 configuration whose scheduler is `pbs` is not silently translated. The
  command stops with an actionable message requiring selection of a SLURM
  profile and review of the generated script.
- Existing calculation outputs and workflow state are not deleted during
  migration.

## Profiles

VASPsolKit provides two SLURM profile paths:

1. A portable profile with no site module assumptions.
2. A local server profile derived from the supplied reference script.

The local profile defaults to:

- partition: `compute`
- node count: `1`
- tasks: `96`
- tasks per node: `96`
- walltime: `72:00:00`
- script: `vasp.slurm`
- executable: `vasp_std`
- launch command: `mpirun -np {tasks} {executable}`
- node binding: none

Profiles are immutable defaults. Case-specific choices produce an override and
do not mutate the reusable profile unless the user explicitly chooses to save
the new values.

## SLURM script model

The parser recognizes both short and long forms of these directives:

- job name: `-J`, `--job-name`
- partition: `-p`, `--partition`
- node count: `-N`, `--nodes`
- total tasks: `-n`, `--ntasks`
- tasks per node: `--ntasks-per-node`
- walltime: `-t`, `--time`
- node list: `-w`, `--nodelist`
- output: `-o`, `--output`

The synchronizer changes only recognized resource directives. Module setup,
environment variables, `ulimit`, comments, redirections, and unrelated shell
commands are preserved.

For automatic allocation the synchronizer removes any inherited `--nodelist`.
For explicit allocation it writes exactly one validated `--nodelist` directive.
The generated launch line is:

```bash
mpirun -np ${SLURM_NTASKS:-96} vasp_std > vasp.log 2>&1
```

Before changing a user script, the CLI shows a unified diff and requires
confirmation. A rejected preview leaves the script unchanged.

## Resource selection

Automatic allocation submits to the selected partition without a node list. The
local default partition is `compute`.

Explicit allocation follows this order:

1. Query partitions with `sinfo` and select a partition.
2. Query nodes in that partition.
3. Display node name, state, total cores, allocated cores, idle cores, and other
   cores.
4. Select one node and the requested task count.
5. Validate that the node belongs to the partition, is schedulable, and has
   sufficient idle cores at the time of review.

The selected partition and node are passed to `sbatch` and synchronized into the
script preview. A node from another partition is rejected. Queue changes after
the preview remain the scheduler's responsibility; VASPsolKit does not promise
that inspected capacity will remain available.

## Scheduler operations

The SLURM adapter implements:

- submit: `sbatch --parsable`
- active queue: `squeue`
- completed history: `sacct -X`
- node and partition inspection: `sinfo`
- cancel: `scancel`

Resource overrides use explicit `sbatch` options as the final source of truth,
while the script is synchronized for reproducibility and human review.

Job IDs returned as `jobid;cluster` are normalized to `jobid`. Array and step
records are not mistaken for the parent job.

## Status reconciliation

Status lookup first queries `squeue`. If the job is absent from the active queue,
VASPsolKit queries:

```bash
sacct -X -n -P -j JOB_ID --format=JobIDRaw,State,ExitCode
```

Parent-job states are normalized as follows:

- `PENDING`, `CONFIGURING`, `RUNNING`, `COMPLETING`, `SUSPENDED` remain active.
- `COMPLETED` is a scheduler success but does not by itself prove VASP
  convergence; output checks still decide `CONVERGED`.
- `FAILED`, `CANCELLED`, `TIMEOUT`, `OUT_OF_MEMORY`, `NODE_FAIL`, and
  `PREEMPTED` become explicit failure/review states with the exit code retained.
- A communication or parsing failure becomes `UNKNOWN`.
- A job absent from both successful `squeue` and `sacct` queries becomes
  `MISSING`.

`UNKNOWN` and unreadable history never trigger automatic resubmission.

## Diagnostics and repair

Submission diagnostics use SLURM wording and validate:

- script existence, UTF-8 readability, LF line endings, and shebang
- `#SBATCH` resource consistency
- selected partition/node compatibility
- `sbatch`, `squeue`, `sacct`, `sinfo`, and `scancel` availability
- job ID parsing and raw scheduler errors

Repair remains preview-first and confirmation-gated. It can normalize line
endings, add a shebang, make a script executable, and synchronize recognized
SLURM resources. It does not invent module names or silently change the VASP
executable.

## Removal boundary

The 0.3.0 source tree will not contain runtime references to:

- `PBSScheduler`, `PBSNodeInfo`, or PBS resource parsers
- `qsub`, `qstat`, `qdel`, or `pbsnodes`
- `#PBS` templates or validators
- `vasp.pbs` as a default input
- PBS-only configuration keys

Migration documentation may mention old names only to explain why an old config
is rejected.

## Tests and acceptance

Tests are written before production changes and cover:

- parsing the supplied reference script while selecting `vasp_std`
- portable and local SLURM profiles
- v1 SLURM migration and v1 PBS rejection
- default `compute` automatic allocation
- partition-first explicit node selection
- rejection of a node outside its partition
- `sinfo` node capacity parsing
- `sbatch` resource options and job ID normalization
- `squeue` active states and `sacct` terminal states
- `UNKNOWN` behavior on scheduler failures
- script diff, confirmation, synchronization, and inherited node removal
- prepare, submit, monitor, cancel, and dry-run paths
- absence of PBS runtime symbols, commands, templates, and defaults

The release is accepted when the full suite, source leak scan, wheel/sdist build,
artifact scan, isolated install, and GitHub Actions all pass. The release version
is `0.3.0` because configuration v2 and PBS removal are breaking changes.
