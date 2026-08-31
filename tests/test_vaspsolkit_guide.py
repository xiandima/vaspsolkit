from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class GuideModelTests(unittest.TestCase):
    def _write_base_inputs(self, root: Path, script: str = "vasp.slurm") -> None:
        for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR", script):
            (root / name).write_text("input\n", encoding="utf-8")

    def _write_config(self, root: Path, script: str = "vasp.slurm") -> None:
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig, write_kit_config

        write_kit_config(
            root / "vaspsolkit.json",
            KitConfig(
                workflow=WorkflowConfig(she_reference_confirmed=True),
                scheduler=SchedulerConfig(kind="slurm", script=script, nodes=["node27.example.invalid"]),
            ),
        )

    def test_complete_base_inputs_without_config_recommends_initialization(self):
        from vaspsolkit.guide_model import build_snapshot, recommend_action

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)

            snapshot = build_snapshot(root)
            action = recommend_action(snapshot)

        self.assertEqual(action.name, "init")
        self.assertIn("初始化", action.title_zh)
        self.assertIn("vaspsolkit.json", action.reason_zh)

    def test_missing_base_inputs_blocks_mutating_action(self):
        from vaspsolkit.guide_model import build_snapshot, recommend_action

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "POSCAR").write_text("input\n", encoding="utf-8")

            snapshot = build_snapshot(root)
            action = recommend_action(snapshot)

        self.assertEqual(action.name, "fix-inputs")
        self.assertEqual(action.effect, "read-only")
        self.assertIsNone(action.cli_command)

    def test_configured_case_without_neutral_state_recommends_prepare_neutral(self):
        from vaspsolkit.guide_model import build_snapshot, recommend_action

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)

            snapshot = build_snapshot(root)
            action = recommend_action(snapshot)

        self.assertEqual(action.name, "prepare-neutral")
        self.assertEqual(action.cli_command, "prepare-neutral")
        self.assertTrue(action.requires_confirmation)

    def test_neutral_prepared_recommends_submit_neutral(self):
        from vaspsolkit.guide_model import build_snapshot, recommend_action
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                stage="neutral_prepared",
                neutral=JobRecord(folder=".", status="PREPARED", metadata={"stage": "neutral_relax"}),
            ).save(root / "vaspsolkit.state.json")

            action = recommend_action(build_snapshot(root))

        self.assertEqual(action.name, "submit-neutral")
        self.assertEqual(action.effect, "external")

    def test_ready_charges_recommend_selected_submit(self):
        from vaspsolkit.guide_model import build_snapshot, recommend_action
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                stage="charge_ready",
                neutral=JobRecord(folder=".", status="CONVERGED", metadata={"stage": "neutral_relax"}),
                prepared_checked=True,
                jobs={"1": JobRecord(folder="charge_sweep/1", status="PREPARED")},
            ).save(root / "vaspsolkit.state.json")

            action = recommend_action(build_snapshot(root))

        self.assertEqual(action.name, "submit-selected")
        self.assertEqual(action.selectable_jobs, ("1",))

    def test_queued_charges_recommend_scheduler_sync_before_reset(self):
        from vaspsolkit.guide_model import build_snapshot, recommend_action
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_base_inputs(root)
            self._write_config(root)
            WorkflowState(
                stage="monitor",
                neutral=JobRecord(folder=".", status="CONVERGED", metadata={"stage": "neutral_relax"}),
                prepared_checked=True,
                jobs={
                    "2": JobRecord(folder="charge_sweep/2", status="QUEUED", job_id="127913.node"),
                    "3": JobRecord(folder="charge_sweep/3", status="RUNNING", job_id="127914.node"),
                },
            ).save(root / "vaspsolkit.state.json")

            action = recommend_action(build_snapshot(root))

        self.assertEqual(action.name, "monitor")
        self.assertEqual(action.cli_command, "monitor")
        self.assertEqual(action.selectable_jobs, ())

    def test_action_cli_argv_adds_confirmation_and_selected_jobs(self):
        from vaspsolkit.guide_model import GuideAction, action_cli_argv

        action = GuideAction(
            name="reset-queued",
            title_zh="重置排队任务",
            reason_zh="测试",
            effect="external",
            cli_command="reset-queued",
            selectable_jobs=("2", "3"),
        )

        argv = action_cli_argv(Path("/case"), action, selected_jobs=["2"])

        self.assertEqual(argv, ["reset-queued", "--workdir", "/case", "--yes", "2"])




class BeginnerCliWiringTests(unittest.TestCase):
    def test_empty_argv_without_tty_fails_safely(self):
        from vaspsolkit.cli import main

        output = []
        with patch("vaspsolkit.cli._has_interactive_tty", return_value=False):
            code = main([], output=output.append)

        self.assertEqual(code, 2)
        self.assertIn("未检测到交互终端", "\n".join(output))

    def test_wizard_command_is_numbered_menu_alias(self):
        from vaspsolkit.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("vaspsolkit.cli._open_menu", return_value=0) as open_menu:
                code = main(["wizard", "--workdir", str(root), "--once"])

        self.assertEqual(code, 0)
        args, kwargs = open_menu.call_args
        self.assertEqual(args[0], root)
        self.assertIsNone(kwargs["config_path"])

