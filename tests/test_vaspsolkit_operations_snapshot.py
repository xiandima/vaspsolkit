from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from unittest.mock import patch


class WorkbenchSnapshotTests(unittest.TestCase):
    def _write_base_inputs(self, root: Path) -> None:
        (root / "POSCAR").write_text(
            "PtO\n1\n1 0 0\n0 1 0\n0 0 1\nPt O\n64 1\nDirect\n",
            encoding="utf-8",
        )
        for name in ("INCAR", "KPOINTS", "POTCAR", "vasp.slurm"):
            (root / name).write_text("input\n", encoding="utf-8")

    def _write_config(self, root: Path) -> None:
        from vaspsolkit.config import KitConfig, WorkflowConfig, write_kit_config

        write_kit_config(root / "vaspsolkit.json", KitConfig(workflow=WorkflowConfig(she_reference_confirmed=True)))

    def test_minimal_case_has_six_pages_and_no_fabricated_queue_jobs(self):
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)

            snapshot = build_workbench_snapshot(root)

        self.assertEqual(snapshot.workdir, root.resolve())
        self.assertEqual(snapshot.system_text, "Pt O · 65 atoms")
        self.assertEqual(
            tuple(item.key for item in snapshot.navigation),
            ("workspace", "inputs", "tasks", "queue", "results", "settings", "exit"),
        )
        self.assertEqual(snapshot.neutral.status, "NOT_PREPARED")
        self.assertEqual(snapshot.charge_jobs, ())
        self.assertEqual(snapshot.queue_rows, ())
        self.assertIsInstance(snapshot.scheduler.nodes, tuple)
        self.assertFalse(any("example.invalid" in repr(value) for value in (snapshot,)))
        self.assertNotIn("demo", repr(snapshot).lower())

    def test_custom_config_path_controls_scheduler_snapshot(self):
        from vaspsolkit.config import KitConfig, write_kit_config
        from vaspsolkit.operations.controller import WorkbenchController

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            default = KitConfig()
            default.scheduler.partition = "default-q"
            write_kit_config(root / "vaspsolkit.json", default)
            custom = KitConfig()
            custom.scheduler.partition = "custom-q"
            custom_path = root / "cluster.json"
            write_kit_config(custom_path, custom)

            snapshot = WorkbenchController(
                root, config_path=custom_path
            ).snapshot()

        self.assertEqual(snapshot.scheduler.partition, "custom-q")

    def test_real_state_populates_neutral_charge_and_queue_records(self):
        from vaspsolkit.orchestrator import STATE_FILENAME
        from vaspsolkit.state import JobRecord, WorkflowState
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            (root / "charge_sweep" / "1").mkdir(parents=True)
            WorkflowState(
                stage="charge_ready",
                neutral=JobRecord(folder=".", status="CONVERGED", job_id="neutral.real"),
                prepared_checked=True,
                jobs={
                    "1": JobRecord(
                        folder="charge_sweep/1", status="QUEUED", job_id="charge.real"
                    )
                },
            ).save(root / STATE_FILENAME)

            snapshot = build_workbench_snapshot(root)

        self.assertEqual(snapshot.neutral.job_id, "neutral.real")
        self.assertIsInstance(snapshot.scheduler.nodes, tuple)
        self.assertEqual(tuple(job.name for job in snapshot.charge_jobs), ("1",))
        self.assertEqual(snapshot.charge_jobs[0].folder, root / "charge_sweep" / "1")
        self.assertEqual(
            tuple((job.name, job.job_id, job.status) for job in snapshot.queue_rows),
            (
                ("neutral", "neutral.real", "CONVERGED"),
                ("1", "charge.real", "QUEUED"),
            ),
        )

    def test_scheduler_overlay_changes_display_only_for_recorded_job(self):
        from vaspsolkit.orchestrator import STATE_FILENAME
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                neutral=JobRecord(folder=".", status="SUBMITTED", job_id="101.server"),
                jobs={"1": JobRecord(folder="charge_sweep/1", status="QUEUED", job_id="102.server")},
            ).save(root / STATE_FILENAME)

            snapshot = build_workbench_snapshot(
                root,
                scheduler_overlay={
                    "101.server": JobState("101.server", True, "R"),
                    "102.server": JobState("102.server", False, "MISSING"),
                    "unrecorded": JobState("unrecorded", True, "Q"),
                },
                last_refresh="2026-07-24T12:00:00+08:00",
                refresh_error="partial scheduler response",
            )

        self.assertEqual(snapshot.neutral.status, "R")
        self.assertEqual(snapshot.charge_jobs[0].status, "MISSING")
        self.assertEqual(len(snapshot.queue_rows), 2)
        self.assertEqual(snapshot.scheduler.last_refresh, "2026-07-24T12:00:00+08:00")
        self.assertEqual(snapshot.scheduler.refresh_error, "partial scheduler response")

    def test_scheduler_overlay_requires_matching_recorded_job_id(self):
        from vaspsolkit.orchestrator import STATE_FILENAME
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                neutral=JobRecord(folder=".", status="SUBMITTED", job_id="neutral-id"),
                jobs={"collision": JobRecord(folder="charge_sweep/collision", status="PREPARED")},
            ).save(root / STATE_FILENAME)

            snapshot = build_workbench_snapshot(
                root,
                scheduler_overlay={
                    "neutral-id": JobState("different-id", True, "R"),
                    "collision": JobState("collision", True, "Q"),
                },
            )

        self.assertEqual(snapshot.neutral.status, "SUBMITTED")
        self.assertIsNone(snapshot.neutral.scheduler_state)
        self.assertEqual(snapshot.charge_jobs[0].status, "PREPARED")
        self.assertIsNone(snapshot.charge_jobs[0].scheduler_state)

    def test_configured_paths_cannot_probe_outside_case(self):
        from vaspsolkit.config import KitConfig, write_kit_config
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "case"
            root.mkdir()
            self._write_base_inputs(root)
            outside = parent / "outside"
            outside.mkdir()
            outside_script = outside / "outside.slurm"
            outside_script.write_text("secret\n", encoding="utf-8")
            (outside / "summary.csv").write_text("secret\n", encoding="utf-8")
            config = KitConfig()
            config.scheduler.script = str(outside_script.resolve())
            config.workflow.results_root = "../outside"
            config.workflow.summary_file = "summary.csv"
            config.workflow.analysis_file = str((outside / "analysis.json").resolve())
            write_kit_config(root / "vaspsolkit.json", config)

            original_is_file = Path.is_file

            def case_only_is_file(path):
                resolved = path.resolve()
                try:
                    resolved.relative_to(root.resolve())
                except ValueError as exc:
                    raise AssertionError(f"outside Case probe: {resolved}") from exc
                return original_is_file(path)

            with patch.object(Path, "is_file", case_only_is_file):
                snapshot = build_workbench_snapshot(root)

        script_rows = [row for row in snapshot.input_rows if row.name == str(outside_script.resolve())]
        self.assertEqual(len(script_rows), 1)
        self.assertFalse(script_rows[0].exists)
        self.assertEqual(script_rows[0].status, "ERROR")
        self.assertTrue(all(not row.exists for row in snapshot.result_rows))
        self.assertTrue(all(row.status == "ERROR" for row in snapshot.result_rows))

    def test_check_prepared_recommendation_has_exactly_one_current_step(self):
        from vaspsolkit.orchestrator import STATE_FILENAME
        from vaspsolkit.state import JobRecord, WorkflowState
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                stage="charge_ready",
                neutral=JobRecord(folder=".", status="CONVERGED"),
                prepared_checked=False,
                jobs={"1": JobRecord(folder="charge_sweep/1", status="PREPARED")},
            ).save(root / STATE_FILENAME)

            snapshot = build_workbench_snapshot(root)

        self.assertEqual(snapshot.recommendation.name, "check-prepared")
        current = tuple(step.key for step in snapshot.workflow_steps if step.state == "current")
        self.assertEqual(current, ("charge-prepare",))
        self.assertEqual(snapshot.scheduler.last_refresh, "")

    def test_job_folders_cannot_escape_case_by_relative_absolute_or_symlink_paths(self):
        from vaspsolkit.orchestrator import STATE_FILENAME
        from vaspsolkit.state import JobRecord, WorkflowState
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "case"
            root.mkdir()
            outside = parent / "outside-job"
            outside.mkdir()
            (root / "job-link").symlink_to(outside, target_is_directory=True)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                neutral=JobRecord(folder="../outside-job", status="PREPARED"),
                jobs={
                    "absolute": JobRecord(folder=str(outside.resolve()), status="QUEUED"),
                    "symlink": JobRecord(folder="job-link", status="CONVERGED"),
                },
            ).save(root / STATE_FILENAME)

            snapshot = build_workbench_snapshot(root)

        jobs = (snapshot.neutral,) + snapshot.charge_jobs
        self.assertTrue(all(job.status == "ERROR" for job in jobs))
        self.assertTrue(all(job.folder == root.resolve() for job in jobs))
        self.assertTrue(all("invalid folder" in job.diagnostics for job in jobs))

    def test_broken_or_looping_result_symlink_is_reported_without_crashing(self):
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            (root / "results").symlink_to("results", target_is_directory=True)

            snapshot = build_workbench_snapshot(root)

        self.assertTrue(all(row.status == "ERROR" for row in snapshot.result_rows))
        self.assertTrue(all(not row.exists for row in snapshot.result_rows))

    def test_rows_reflect_real_file_existence_without_writes_or_commands(self):
        from vaspsolkit.operations.snapshot import build_workbench_snapshot

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            (root / "results").mkdir()
            (root / "results" / "summary.csv").write_text("name,value\n", encoding="utf-8")
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            with patch("subprocess.run", side_effect=AssertionError("external command called")):
                snapshot = build_workbench_snapshot(root)

            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

        inputs = {row.name: row.exists for row in snapshot.input_rows}
        results = {row.name: row.exists for row in snapshot.result_rows}
        self.assertEqual(before, after)
        self.assertTrue(inputs["POSCAR"])
        self.assertFalse(inputs["vaspsolkit.json"])
        self.assertTrue(results["summary.csv"])
        self.assertFalse(results["analysis.json"])

    def test_presentation_models_are_frozen_dataclasses(self):
        from vaspsolkit.operations.models import (
            InputCheckRow,
            JobView,
            NavigationItem,
            NeutralOutputView,
            RecommendationView,
            ResultRow,
            SchedulerView,
            WorkbenchSnapshot,
            WorkflowStep,
        )

        classes = (
            NavigationItem,
            WorkflowStep,
            JobView,
            SchedulerView,
            RecommendationView,
            InputCheckRow,
            ResultRow,
            NeutralOutputView,
            WorkbenchSnapshot,
        )
        self.assertTrue(all(is_dataclass(model) for model in classes))
        with self.assertRaises(FrozenInstanceError):
            NavigationItem("workspace", "工作区", "Workspace", "1").key = "changed"


if __name__ == "__main__":
    unittest.main()
