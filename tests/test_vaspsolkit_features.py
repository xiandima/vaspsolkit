from __future__ import annotations

import json
import csv
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


class VaspsolkitConfigAndStateTests(unittest.TestCase):
    def test_relaxation_profiles_distinguish_neutral_and_charge_initialization(self):
        from vaspsolkit.inputs import apply_incar_profile

        neutral = apply_incar_profile("ENCUT = 400\n", "vaspsol-neutral-relax")
        charge = apply_incar_profile("ENCUT = 400\n", "vaspsol-charge-relax")

        self.assertIn("IBRION = 2", neutral)
        self.assertIn("NSW = 200", neutral)
        self.assertIn("ICHARG = 2", neutral)
        self.assertIn("LCHARG = .TRUE.", neutral)
        self.assertIn("IBRION = 2", charge)
        self.assertIn("NSW = 200", charge)
        self.assertIn("ISTART = 0", charge)
        self.assertIn("ICHARG = 1", charge)

    def test_state_round_trip_preserves_relaxation_provenance(self):
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vaspsolkit.state.json"
            state = WorkflowState(
                stage="neutral_prepared",
                neutral=JobRecord(
                    folder=".",
                    status="PREPARED",
                    metadata={"stage": "neutral_relax", "source_poscar_sha256": "abc"},
                ),
            )
            state.save(path)
            loaded = WorkflowState.load(path)

        self.assertEqual(loaded.neutral.metadata["stage"], "neutral_relax")
        self.assertEqual(loaded.neutral.metadata["source_poscar_sha256"], "abc")
    def test_flat_pbs_shaped_config_requires_slurm_profile_selection(self):
        from vaspsolkit.config import load_kit_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vaspsolkit.json"
            path.write_text(
                json.dumps(
                    {
                        "folders": ["1", "2", "3", "4", "5"],
                        "nelect_offsets": [-1.0, -0.5, 0.0, 0.5, 1.0],
                        "sbatch_queue": "normal",
                        "sbatch_ppn": 48,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "select a SLURM profile"):
                load_kit_config(path)

    def test_charge_sweep_requires_exactly_one_neutral_point(self):
        from vaspsolkit.config import WorkflowConfig

        valid = {"folders": ["1", "2", "3"]}
        WorkflowConfig(nelect_offsets=[-1.0, 0.0, 1.0], **valid).validate(require_neutral=True)
        with self.assertRaisesRegex(ValueError, "exactly one neutral"):
            WorkflowConfig(nelect_offsets=[-1.0, 0.5, 1.0], **valid).validate(require_neutral=True)
        with self.assertRaisesRegex(ValueError, "exactly one neutral"):
            WorkflowConfig(nelect_offsets=[0.0, 0.0, 1.0], **valid).validate(require_neutral=True)

    def test_workflow_state_round_trip_preserves_job_diagnostics(self):
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vaspsolkit.state.json"
            state = WorkflowState(
                stage="monitor",
                jobs={
                    "3": JobRecord(
                        folder="charge_sweep/3",
                        status="NEEDS_REVIEW",
                        job_id="123.server",
                        restart_count=1,
                        diagnostics=["electronic_not_converged"],
                    )
                },
            )
            state.save(path)

            loaded = WorkflowState.load(path)

        self.assertEqual(loaded.stage, "monitor")
        self.assertEqual(loaded.jobs["3"].job_id, "123.server")
        self.assertEqual(loaded.jobs["3"].restart_count, 1)
        self.assertEqual(loaded.jobs["3"].diagnostics, ["electronic_not_converged"])

    def test_workflow_state_round_trip_preserves_neutral_job(self):
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vaspsolkit.state.json"
            state = WorkflowState(
                stage="neutral_submitted",
                neutral=JobRecord(folder=".", status="SUBMITTED", job_id="126544.server"),
            )
            state.save(path)

            loaded = WorkflowState.load(path)

        self.assertIsNotNone(loaded.neutral)
        self.assertEqual(loaded.neutral.folder, ".")
        self.assertEqual(loaded.neutral.status, "SUBMITTED")
        self.assertEqual(loaded.neutral.job_id, "126544.server")


class VaspsolkitInputTests(unittest.TestCase):
    def test_neutral_vaspsol_plan_preserves_user_tags_and_adds_only_missing_required_tags(self):
        from vaspsolkit.inputs import plan_neutral_vaspsol_update

        original = (
            "ENCUT = 450\nGGA = RP\nIVDW = 12\nIBRION = 1\n"
            "NSW = 88\nEDIFFG = -0.03\n"
        )

        plan = plan_neutral_vaspsol_update(original)

        self.assertFalse(plan.duplicates)
        self.assertFalse(plan.conflicts)
        for line in original.splitlines():
            self.assertIn(line, plan.candidate)
        self.assertIn(("LSOL", ".TRUE."), plan.additions)
        self.assertIn("LSOL = .TRUE.", plan.candidate)
        self.assertIn("ISTART = 0", plan.candidate)
        self.assertIn("ICHARG = 2", plan.candidate)

    def test_neutral_vaspsol_plan_reports_duplicate_and_conflicting_required_tags(self):
        from vaspsolkit.inputs import plan_neutral_vaspsol_update

        plan = plan_neutral_vaspsol_update(
            "IBRION = 2\nNSW = 100\nLSOL = .FALSE.\nEB_k = 80\nEB_K = 80\n"
        )

        self.assertIn("EB_K", plan.duplicates)
        self.assertIn(("LSOL", ".FALSE.", ".TRUE."), plan.conflicts)

    def test_potcar_validation_matches_poscar_order_and_suggests_encut(self):
        from vaspsolkit.inputs import suggest_encut, validate_potcar_order

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "POSCAR").write_text(
                "sample\n1.0\n1 0 0\n0 1 0\n0 0 1\nNi N C\n1 2 3\nDirect\n",
                encoding="utf-8",
            )
            (root / "POTCAR").write_text(
                "TITEL  = PAW_PBE Ni_pv 06Sep2000\nENMAX = 269.865 eV\n"
                "TITEL  = PAW_PBE N 08Apr2002\nENMAX = 400.000 eV\n"
                "TITEL  = PAW_PBE C 08Apr2002\nENMAX = 400.000 eV\n",
                encoding="utf-8",
            )

            order = validate_potcar_order(root / "POSCAR", root / "POTCAR")
            encut = suggest_encut(root / "POTCAR")

        self.assertEqual(order, ["Ni", "N", "C"])
        self.assertEqual(encut, 520)

    def test_potcar_validation_rejects_mismatched_element_order(self):
        from vaspsolkit.inputs import validate_potcar_order

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "POSCAR").write_text(
                "sample\n1.0\n1 0 0\n0 1 0\n0 0 1\nNi N\n1 1\nDirect\n",
                encoding="utf-8",
            )
            (root / "POTCAR").write_text(
                "TITEL = PAW_PBE N\nTITEL = PAW_PBE Ni_pv\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "element order"):
                validate_potcar_order(root / "POSCAR", root / "POTCAR")

    def test_static_profile_preserves_existing_encut_and_overrides_stage_tags(self):
        from vaspsolkit.inputs import apply_incar_profile

        text = apply_incar_profile(
            "ENCUT = 400\nNSW = 50\n",
            "static",
            overrides={"ISPIN": "2"},
            suggested_encut=520,
        )

        self.assertIn("ENCUT = 400", text)
        self.assertIn("IBRION = -1", text)
        self.assertIn("NSW = 0", text)
        self.assertIn("ISPIN = 2", text)

    def test_charge_incar_uses_chgcar_without_wavecar(self):
        from vaspsolkit.workflow import _write_charge_incar

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "INCAR"
            path.write_text(
                "ISTART = 1\nICHARG = 2\nLWAVE = .TRUE.\n"
                "IBRION = 1\nNSW = 88\nEDIFFG = -0.03\n",
                encoding="utf-8",
            )
            _write_charge_incar(path, 100.5)
            text = path.read_text(encoding="utf-8")

        self.assertIn("ISTART = 0", text)
        self.assertIn("ICHARG = 1", text)
        self.assertIn("LWAVE = .TRUE.", text)
        self.assertIn("IBRION = 1", text)
        self.assertIn("NSW = 88", text)
        self.assertIn("EDIFFG = -0.03", text)


class VaspsolkitSchedulerTests(unittest.TestCase):
    def test_pbs_submission_uses_workflow_capacity_and_selected_node_slots(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import submit_ready_jobs
        from vaspsolkit.state import JobRecord, WorkflowState
        from vaspsolkit.scheduler import SlurmScheduler

        calls = []

        def runner(args, cwd=None):
            import subprocess

            calls.append((list(args), cwd))
            if args[0] == "squeue":
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="\n".join(
                        f"126{i}.node01.example.invalid other-{i} testuser 00:00:00 R normal"
                        for i in range(12)
                    ),
                    stderr="",
                )
            if args[:2] == ["sinfo", "-a"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=(
                        "node18.example.invalid\n"
                        "     state = free\n"
                        "     np = 96\n"
                        "     jobs = 0-47/119000.node01.example.invalid\n"
                    ),
                    stderr="",
                )
            if args[0] == "sbatch":
                return subprocess.CompletedProcess(args, 0, stdout="101\n", stderr="")
            raise AssertionError(f"unexpected command: {args}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = {}
            for name in ("1", "2", "3"):
                folder = root / "charge_sweep" / name
                folder.mkdir(parents=True)
                for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                    (folder / filename).write_text("input\n", encoding="utf-8")
                jobs[name] = JobRecord(folder=str(folder.relative_to(root)))
            state = WorkflowState(stage="prepared", jobs=jobs)
            config = KitConfig(
                workflow=WorkflowConfig(folders=["1", "2", "3"], nelect_offsets=[-1.0, 0.0, 1.0]),
                scheduler=SchedulerConfig(
                    kind="slurm",
                    script="vasp.slurm",
                    tasks=48,
                    max_inflight=5,
                    nodes=["node18.example.invalid"],
                ),
            )

            submitted = submit_ready_jobs(
                root,
                config,
                state,
                scheduler=SlurmScheduler(runner=runner),
                confirmed=True,
            )

        self.assertEqual(set(submitted), {"1", "2", "3"})
        sbatch = next(args for args, _ in calls if args[0] == "sbatch")
        self.assertIn("--partition", sbatch)
        self.assertIn("--ntasks", sbatch)

    def test_scheduler_configure_interactive_saves_nodes_cores_without_prompting_capacity(self):
        from vaspsolkit.cli import main
        from vaspsolkit.scheduler import SlurmScheduler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "vaspsolkit.json"
            config_path.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "profile": "vaspsol-sweep",
                        "workflow": {},
                        "scheduler": {"kind": "slurm", "script": "vasp.slurm"},
                    }
                ),
                encoding="utf-8",
            )
            output = []
            inputs = iter(["normal", "node18.example.invalid,node19.example.invalid", "24", "48:00:00"])

            class FakeNode:
                def __init__(self, name):
                    self.name = name
                    self.state = "idle"
                    self.total_cores = 48
                    self.allocated_cores = 0
                    self.idle_cores = 48
                    self.other_cores = 0

            scheduler = SlurmScheduler()
            with patch.object(scheduler, "inspect_partitions", return_value=["normal"]), patch.object(scheduler, "inspect_nodes", return_value=[FakeNode("node18.example.invalid"), FakeNode("node19.example.invalid")]), patch(
                "vaspsolkit.cli.scheduler_from_config", return_value=scheduler
            ):
                result = main(
                    ["configure-scheduler", "--workdir", str(root)],
                    input_fn=lambda prompt: next(inputs),
                    output=output.append,
                )

            saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(saved["scheduler"]["nodes"], ["node18.example.invalid", "node19.example.invalid"])
        self.assertEqual(saved["scheduler"]["tasks"], 24)
        self.assertIsNone(saved["scheduler"]["max_inflight"])

    def test_pbs_script_diagnostics_detect_dos_line_endings(self):
        from vaspsolkit.config import SchedulerConfig
        from vaspsolkit.scheduler_diagnostics import diagnose_slurm_script

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vasp.slurm").write_bytes(b"#!/bin/bash\r\n#SLURM -q normal\r\n")

            checks = diagnose_slurm_script(root, SchedulerConfig(kind="slurm", script="vasp.slurm"))

        by_code = {check.code: check for check in checks}
        self.assertEqual(by_code["script-line-endings"].status, "FAIL")
        self.assertEqual(by_code["script-line-endings"].repair_action, "fix-line-endings")

    def test_classify_sbatch_dos_line_ending_error(self):
        from vaspsolkit.scheduler_diagnostics import classify_submit_error

        card = classify_submit_error(
            RuntimeError("sbatch failed in /case: sbatch: script is written in DOS/Windows text format")
        )

        self.assertEqual(card.cause_code, "dos-line-endings")
        self.assertEqual(card.repair_action, "fix-line-endings")

    def test_submission_batches_respect_existing_inflight_jobs(self):
        from vaspsolkit.scheduler import plan_submission_batches

        batches = plan_submission_batches(["1", "2", "3", "4", "5"], max_inflight=2, active=1)

        self.assertEqual(batches, [["1"], ["2", "3"], ["4", "5"]])

    def test_scheduler_has_no_application_submission_limit_by_default(self):
        from vaspsolkit.config import SchedulerConfig
        from vaspsolkit.scheduler import plan_submission_batches

        config = SchedulerConfig()
        batches = plan_submission_batches(
            ["1", "2", "3", "4", "5"],
            max_inflight=config.max_inflight,
            active=99,
        )

        self.assertIsNone(config.max_inflight)
        self.assertEqual(batches, [["1", "2", "3", "4", "5"]])

    def test_submission_batches_expose_zero_capacity_without_submitting_future_batch(self):
        from vaspsolkit.scheduler import plan_submission_batches

        batches = plan_submission_batches(["1", "2", "3"], max_inflight=2, active=2)

        self.assertEqual(batches, [[], ["1", "2"], ["3"]])

    def test_slurm_scheduler_submits_and_queries_without_node_binding(self):
        from vaspsolkit.scheduler import SlurmScheduler

        calls = []

        def runner(args, cwd=None):
            import subprocess

            calls.append((list(args), cwd))
            if args[0] == "sbatch":
                return subprocess.CompletedProcess(args, 0, "4321\n", "")
            return subprocess.CompletedProcess(args, 0, "RUNNING\n", "")

        scheduler = SlurmScheduler(runner=runner)
        job_id = scheduler.submit(Path("/tmp/case"), "vasp.slurm")
        state = scheduler.status(job_id)

        self.assertEqual(job_id, "4321")
        self.assertEqual(state.state, "RUNNING")
        self.assertEqual(calls[0][0], ["sbatch", "--parsable", "vasp.slurm"])
        self.assertNotIn("--nodelist", calls[0][0])

    def test_custom_scheduler_extracts_job_id_and_unknown_status_is_preserved(self):
        from vaspsolkit.config import SchedulerConfig
        from vaspsolkit.scheduler import CustomScheduler

        def runner(args, cwd=None):
            import subprocess

            if args[0] == "local-submit":
                return subprocess.CompletedProcess(args, 0, "JOB=77\n", "")
            return subprocess.CompletedProcess(args, 2, "", "connection failed")

        config = SchedulerConfig(
            kind="custom",
            submit_command=["local-submit", "{script}"],
            status_command=["local-status", "{job_id}"],
            cancel_command=["local-cancel", "{job_id}"],
            job_id_pattern=r"JOB=(?P<job_id>\d+)",
        )
        scheduler = CustomScheduler(config, runner=runner)

        job_id = scheduler.submit(Path("/tmp/case"), "submit.sh")
        state = scheduler.status(job_id)

        self.assertEqual(job_id, "77")
        self.assertTrue(state.exists)
        self.assertEqual(state.state, "UNKNOWN")

    def test_pbs_connection_failure_is_unknown_not_missing(self):
        from vaspsolkit.scheduler import SlurmScheduler

        def runner(args, cwd=None):
            import subprocess

            return subprocess.CompletedProcess(args, 2, "", "cannot connect to server")

        state = SlurmScheduler(runner=runner).status("123.server")

        self.assertTrue(state.exists)
        self.assertEqual(state.state, "UNKNOWN")


class VaspsolkitConvergenceTests(unittest.TestCase):
    def test_benign_zbrent_line_search_messages_do_not_override_convergence(self):
        from vaspsolkit.convergence import check_job

        for message in (
            "ZBRENT: can't locate minimum, use default step",
            "ZBRENT: extrapolating",
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "OUTCAR").write_text(
                    "reached required accuracy - stopping structural energy minimisation\n",
                    encoding="utf-8",
                )
                (root / "job.log").write_text(message + "\n", encoding="utf-8")

                result = check_job(root, scheduler_state="MISSING")

                self.assertEqual(result.status, "CONVERGED")
                self.assertNotIn("zbrent_error", result.diagnostics)

    def test_fatal_zbrent_bracketing_remains_needs_review(self):
        from vaspsolkit.convergence import check_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "OUTCAR").write_text(
                "reached required accuracy - stopping structural energy minimisation\n",
                encoding="utf-8",
            )
            (root / "job.log").write_text(
                "ZBRENT: fatal error in bracketing\n",
                encoding="utf-8",
            )

            result = check_job(root, scheduler_state="MISSING")

        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertIn("zbrent_error", result.diagnostics)

    def test_checker_reports_nelm_and_vaspsol_failure(self):
        from vaspsolkit.convergence import check_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "INCAR").write_text("NELECT = 10\nNELM = 3\n", encoding="utf-8")
            (root / "OUTCAR").write_text(
                "NELECT = 10\nNELM = 3\nIteration 1( 3)\n"
                "MINIMIZE_L: failed to converge\n",
                encoding="utf-8",
            )

            result = check_job(root, scheduler_state="MISSING")

        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertIn("electronic_nelm_reached", result.diagnostics)
        self.assertIn("vaspsol_minimize_l_failed", result.diagnostics)

    def test_unknown_scheduler_state_never_becomes_failed_or_resubmittable(self):
        from vaspsolkit.convergence import check_job

        with tempfile.TemporaryDirectory() as tmp:
            result = check_job(Path(tmp), scheduler_state="UNKNOWN")

        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.can_resubmit)

    def test_repair_requires_confirmation_archives_outputs_and_never_adds_wavecar(self):
        from vaspsolkit.convergence import DiagnosticResult, apply_repair, propose_repair

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "INCAR").write_text("ISTART = 1\nICHARG = 1\nLWAVE = .TRUE.\n", encoding="utf-8")
            (root / "OUTCAR").write_text("old output\n", encoding="utf-8")
            (root / "CHGCAR").write_text("charge seed\n", encoding="utf-8")
            proposal = propose_repair(
                root,
                DiagnosticResult(status="NEEDS_REVIEW", diagnostics=["electronic_nelm_reached"]),
            )

            with self.assertRaisesRegex(PermissionError, "confirmation"):
                apply_repair(root, proposal, confirmed=False)
            archive = apply_repair(root, proposal, confirmed=True)
            incar = (root / "INCAR").read_text(encoding="utf-8")

            self.assertIn("ISTART = 0", incar)
            self.assertIn("ICHARG = 1", incar)
            self.assertIn("LWAVE = .FALSE.", incar)
            self.assertFalse((root / "WAVECAR").exists())
            self.assertTrue((archive / "OUTCAR").exists())
            self.assertTrue((archive / "CHGCAR").exists())
            self.assertTrue((root / "CHGCAR").exists())


class VaspsolkitPostprocessTests(unittest.TestCase):
    def _write_summary(self, path: Path, potentials):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["folder", "converged", "fit_included", "delta_electrons", "u_vs_she", "energy_at_potential"],
            )
            writer.writeheader()
            for index, potential in enumerate(potentials, 1):
                writer.writerow(
                    {
                        "folder": str(index),
                        "converged": 1,
                        "fit_included": 1,
                        "delta_electrons": potential,
                        "u_vs_she": potential,
                        "energy_at_potential": -((potential - 0.2) ** 2) - 10.0,
                    }
                )

    def test_five_point_postprocess_writes_plot_data_and_report(self):
        from vaspsolkit.postprocess import postprocess_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "summary.csv"
            self._write_summary(summary, [-1.0, -0.5, 0.0, 0.5, 1.0])

            result = postprocess_summary(summary, root / "results")

            analysis = json.loads((root / "results" / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(len(analysis["points"]), 5)
            self.assertAlmostEqual(analysis["energy_fit"]["u0"], 0.2)
            self.assertTrue((root / "results" / "eu_curve.csv").exists())
            self.assertGreater((root / "results" / "eu_curve.png").stat().st_size, 0)
            self.assertTrue((root / "results" / "report.md").exists())
            self.assertEqual(result.point_count, 5)

    def test_four_point_postprocess_is_rejected(self):
        from vaspsolkit.postprocess import postprocess_summary

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "summary.csv"
            self._write_summary(summary, [-1.0, -0.5, 0.0, 0.5])
            with self.assertRaisesRegex(ValueError, "exactly five"):
                postprocess_summary(summary, root / "results")

    def test_reaction_spec_combines_system_fits_and_rejects_unsafe_formula(self):
        from vaspsolkit.reaction import evaluate_formula, run_reaction_spec

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, c in (("slab", -10.0), ("star_A", -12.0)):
                path = root / f"{name}.json"
                path.write_text(
                    json.dumps({"energy_fit": {"a": -1.0, "b": 0.0, "c": c}}),
                    encoding="utf-8",
                )
            spec = root / "reaction.json"
            spec.write_text(
                json.dumps(
                    {
                        "name": "example",
                        "systems": {"slab": "slab.json", "star_A": "star_A.json"},
                        "constants": {"G_A": -0.5},
                        "curves": [{"name": "A adsorption", "formula": "E(\"star_A\", U) - E(\"slab\", U) - G_A"}],
                        "grid": {"u_min": -1.0, "u_max": 1.0, "points": 5},
                    }
                ),
                encoding="utf-8",
            )

            outputs = run_reaction_spec(spec, root / "reaction-results")

            self.assertTrue(outputs.csv_path.exists())
            self.assertTrue(outputs.plot_path.exists())
            self.assertAlmostEqual(evaluate_formula("1 + 2 * U", {}, {}, 0.5), 2.0)
            with self.assertRaisesRegex(ValueError, "not allowed"):
                evaluate_formula("__import__('os').system('id')", {}, {}, 0.0)


class VaspsolkitOrchestratorTests(unittest.TestCase):
    def test_prepare_neutral_job_archives_old_outputs_and_writes_relaxation_incar(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import prepare_neutral_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename, content in {
                "POSCAR": "initial structure\n",
                "POTCAR": "potcar\n",
                "KPOINTS": "kpoints\n",
                "INCAR": "IBRION = 1\nNSW = 88\nEDIFFG = -0.03\nICHARG = 2\n",
                "vasp.slurm": "#!/bin/bash\n",
                "CONTCAR": "old static result\n",
                "OUTCAR": "old static result\n",
                "CHGCAR": "old charge\n",
                "LOCPOT": "old locpot\n",
            }.items():
                (root / filename).write_text(content, encoding="utf-8")
            (root / "charge_sweep" / "1").mkdir(parents=True)
            (root / "charge_sweep" / "1" / "OUTCAR").write_text("old charge point\n", encoding="utf-8")
            config = KitConfig(
                workflow=WorkflowConfig(nelect_ref=10.0),
                scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"),
            )

            state = prepare_neutral_job(root, config)

            incar = (root / "INCAR").read_text(encoding="utf-8")
            archives = list((root / ".vaspsolkit" / "archive").glob("*"))
            archived_charge = any((archive / "charge_sweep").exists() for archive in archives)

        self.assertEqual(state.stage, "neutral_prepared")
        self.assertEqual(state.neutral.status, "PREPARED")
        self.assertEqual(state.neutral.metadata["stage"], "neutral_relax")
        self.assertIn("IBRION = 1", incar)
        self.assertIn("NSW = 88", incar)
        self.assertIn("EDIFFG = -0.03", incar)
        self.assertIn("ICHARG = 2", incar)
        self.assertTrue(archives)
        self.assertTrue(archived_charge)

    def test_prepare_charge_jobs_uses_neutral_contcar_and_chgcar_for_relaxation(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import prepare_kit_jobs
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename, content in {
                "INCAR": "IBRION = 1\nNSW = 88\nICHARG = 2\n",
                "POTCAR": "potcar\n",
                "KPOINTS": "kpoints\n",
                "CONTCAR": "neutral optimized structure\n",
                "CHGCAR": "neutral optimized charge\n",
                "LOCPOT": "neutral locpot\n",
                "OUTCAR": "neutral outcar\n",
                "vasp.slurm": "#!/bin/bash\n",
            }.items():
                (root / filename).write_text(content, encoding="utf-8")
            state = WorkflowState(
                stage="neutral_converged",
                neutral=JobRecord(
                    folder=".",
                    status="CONVERGED",
                    metadata={"stage": "neutral_relax"},
                ),
            )
            state.save(root / "vaspsolkit.state.json")
            config = KitConfig(
                workflow=WorkflowConfig(
                    folders=["1", "2", "3"],
                    nelect_offsets=[-1.0, 0.0, 1.0],
                    nelect_ref=10.0,
                ),
                scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"),
            )

            prepared = prepare_kit_jobs(root, config)
            child_incar = (root / "charge_sweep" / "1" / "INCAR").read_text(encoding="utf-8")
            child_poscar = (root / "charge_sweep" / "1" / "POSCAR").read_text(encoding="utf-8")
            child_chgcar = (root / "charge_sweep" / "1" / "CHGCAR").read_text(encoding="utf-8")

        self.assertEqual(prepared.stage, "charge_prepared")
        self.assertEqual(child_poscar, "neutral optimized structure\n")
        self.assertEqual(child_chgcar, "neutral optimized charge\n")
        self.assertIn("IBRION = 1", child_incar)
        self.assertIn("NSW = 88", child_incar)
        self.assertIn("ISTART = 0", child_incar)
        self.assertIn("ICHARG = 1", child_incar)
        self.assertIn("NELECT = 9.0000", child_incar)

    def test_check_prepared_jobs_requires_neutral_sources_and_marks_validation(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import check_prepared_jobs, prepare_kit_jobs
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename, content in {
                "INCAR": "IBRION = 1\nNSW = 88\nICHARG = 2\n",
                "POTCAR": "potcar\n",
                "KPOINTS": "kpoints\n",
                "CONTCAR": "neutral optimized structure\n",
                "CHGCAR": "neutral optimized charge\n",
                "LOCPOT": "neutral locpot\n",
                "OUTCAR": "neutral outcar\n",
                "vasp.slurm": "#!/bin/bash\n",
            }.items():
                (root / filename).write_text(content, encoding="utf-8")
            WorkflowState(
                stage="neutral_converged",
                neutral=JobRecord(folder=".", status="CONVERGED", metadata={"stage": "neutral_relax"}),
            ).save(root / "vaspsolkit.state.json")
            config = KitConfig(
                workflow=WorkflowConfig(folders=["1"], nelect_offsets=[0.0], nelect_ref=10.0),
                scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"),
            )
            state = prepare_kit_jobs(root, config)

            checked = check_prepared_jobs(root, config, state)

        self.assertTrue(checked.prepared_checked)
    def test_slurm_prepare_creates_five_jobs_without_wavecar(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import prepare_kit_jobs
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in {
                "INCAR": "ENCUT = 400\n",
                "POTCAR": "potcar\n",
                "KPOINTS": "kpoints\n",
                "CONTCAR": "relaxed\n",
                "CHGCAR": "charge\n",
                "LOCPOT": "locpot\n",
                "OUTCAR": "outcar\n",
                "WAVECAR": "wave\n",
                "vasp.slurm": "#!/bin/bash\nsrun vasp_std\n",
            }.items():
                (root / name).write_text(content, encoding="utf-8")
            config = KitConfig(
                workflow=WorkflowConfig(nelect_ref=10.0),
                scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm", max_inflight=2),
            )
            WorkflowState(
                stage="neutral_converged",
                neutral=JobRecord(folder=".", status="CONVERGED"),
            ).save(root / "vaspsolkit.state.json")

            state = prepare_kit_jobs(root, config)

            self.assertEqual(len(state.jobs), 5)
            neutral = root / "charge_sweep" / "3"
            self.assertTrue((neutral / "vasp.slurm").exists())
            self.assertTrue((neutral / "CHGCAR").exists())
            self.assertFalse((neutral / "WAVECAR").exists())
            self.assertIn("ISTART = 0", (neutral / "INCAR").read_text(encoding="utf-8"))

    def test_submit_ready_jobs_respects_capacity_and_records_job_ids(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import submit_ready_jobs
        from vaspsolkit.scheduler import QueueEntry
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.submitted = []

            def inspect(self):
                return [QueueEntry(job_id="running", state="RUNNING")]

            def submit(self, folder, script, **kwargs):
                self.submitted.append(Path(folder).name)
                return f"job-{Path(folder).name}"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = {}
            for name in ("1", "2", "3"):
                folder = root / "charge_sweep" / name
                folder.mkdir(parents=True)
                for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                    (folder / filename).write_text("input\n", encoding="utf-8")
                jobs[name] = JobRecord(folder=str(folder.relative_to(root)))
            state = WorkflowState(stage="prepared", jobs=jobs)
            config = KitConfig(
                workflow=WorkflowConfig(folders=["1", "2", "3"], nelect_offsets=[-1.0, 0.0, 1.0]),
                scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm", max_inflight=2),
            )
            scheduler = FakeScheduler()

            submitted = submit_ready_jobs(root, config, state, scheduler=scheduler, confirmed=True)

            self.assertEqual(submitted, {"1": "job-1"})
            self.assertEqual(state.jobs["1"].status, "SUBMITTED")
            self.assertEqual(state.jobs["2"].status, "PREPARED")

    def test_submit_selected_jobs_only_submits_user_selected_ready_jobs(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import submit_selected_jobs
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.submitted = []

            def submit(self, folder, script, **kwargs):
                self.submitted.append((Path(folder).name, script, kwargs))
                return f"job-{Path(folder).name}"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("1", "2", "3"):
                folder = root / "charge_sweep" / name
                folder.mkdir(parents=True)
                for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "CHGCAR", "vasp.slurm"):
                    (folder / filename).write_text("ok\n", encoding="utf-8")
            state = WorkflowState(
                stage="charge_ready",
                prepared_checked=True,
                jobs={
                    name: JobRecord(
                        folder=str(Path("charge_sweep") / name),
                        status="PREPARED",
                    )
                    for name in ("1", "2", "3")
                },
            )
            config = KitConfig(
                workflow=WorkflowConfig(job_root="charge_sweep"),
                scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm", max_inflight=1),
            )
            scheduler = FakeScheduler()

            submitted = submit_selected_jobs(
                root,
                config,
                state,
                ["2"],
                scheduler=scheduler,
                confirmed=True,
            )

        self.assertEqual(submitted, {"2": "job-2"})
        self.assertEqual([item[0] for item in scheduler.submitted], ["2"])
        self.assertEqual(state.jobs["1"].status, "PREPARED")
        self.assertEqual(state.jobs["2"].status, "SUBMITTED")

    def test_dry_run_submission_does_not_mutate_job_state(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig, WorkflowConfig
        from vaspsolkit.orchestrator import submit_ready_jobs
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def inspect(self):
                return []

            def submit(self, folder, script, **kwargs):
                return "DRY-RUN:1"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "charge_sweep" / "1"
            folder.mkdir(parents=True)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "submit.sh"):
                (folder / filename).write_text("input\n", encoding="utf-8")
            state = WorkflowState(jobs={"1": JobRecord(folder="charge_sweep/1")})
            config = KitConfig(
                profile="static",
                workflow=WorkflowConfig(folders=["1"], nelect_offsets=[0.0]),
                scheduler=SchedulerConfig(kind="slurm", script="submit.sh"),
            )

            result = submit_ready_jobs(
                root,
                config,
                state,
                scheduler=FakeScheduler(),
                dry_run=True,
            )

            self.assertEqual(result, {"1": "DRY-RUN:1"})
            self.assertEqual(state.jobs["1"].status, "PREPARED")
            self.assertEqual(state.jobs["1"].job_id, "")

    def test_submit_neutral_records_job_id_and_does_not_poll(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import submit_neutral_job

        class FakeScheduler:
            def __init__(self):
                self.submits = 0
                self.status_calls = 0

            def submit(self, folder, script, **kwargs):
                self.submits += 1
                return "123"

            def status(self, job_id):
                self.status_calls += 1
                raise AssertionError("neutral submission must not poll")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))
            scheduler = FakeScheduler()

            state = submit_neutral_job(root, config, scheduler=scheduler, confirmed=True)

            self.assertEqual(scheduler.submits, 1)
            self.assertEqual(scheduler.status_calls, 0)
            self.assertEqual(state.neutral.job_id, "123")
            self.assertEqual(state.neutral.status, "SUBMITTED")

    def test_submit_neutral_preserves_relaxation_provenance(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import submit_neutral_job
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def submit(self, folder, script, **kwargs):
                return "neutral-456"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename, content in {
                "INCAR": "IBRION = 2\nNSW = 200\nICHARG = 2\n",
                "POSCAR": "poscar\n",
                "POTCAR": "potcar\n",
                "KPOINTS": "kpoints\n",
                "vasp.slurm": "#!/bin/bash\n",
            }.items():
                (root / filename).write_text(content, encoding="utf-8")
            WorkflowState(
                stage="neutral_prepared",
                neutral=JobRecord(
                    folder=".",
                    status="PREPARED",
                    metadata={"stage": "neutral_relax", "profile": "vaspsol-neutral-relax"},
                ),
            ).save(root / "vaspsolkit.state.json")
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))

            state = submit_neutral_job(
                root,
                config,
                scheduler=FakeScheduler(),
                confirmed=True,
                require_prepared=True,
            )

        self.assertEqual(state.neutral.job_id, "neutral-456")
        self.assertEqual(state.neutral.metadata["stage"], "neutral_relax")

    def test_submit_neutral_rejects_duplicate_recorded_job(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import submit_neutral_job

        class FakeScheduler:
            def submit(self, folder, script, **kwargs):
                return "123"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))
            scheduler = FakeScheduler()
            submit_neutral_job(root, config, scheduler=scheduler, confirmed=True)

            with self.assertRaisesRegex(RuntimeError, "already recorded"):
                submit_neutral_job(root, config, scheduler=scheduler, confirmed=True)

    def test_check_neutral_treats_partial_outcar_as_running(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import check_neutral_job, submit_neutral_job
        from vaspsolkit.scheduler import JobState

        class FakeScheduler:
            def submit(self, folder, script, **kwargs):
                return "123"

            def status(self, job_id):
                return JobState(job_id=job_id, exists=True, state="R")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            (root / "OUTCAR").write_text(
                "NELECT = 64.0000 total number of electrons\n"
                "free  energy   TOTEN  = -324.5787 eV\n",
                encoding="utf-8",
            )
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))
            scheduler = FakeScheduler()
            submit_neutral_job(root, config, scheduler=scheduler, confirmed=True)

            state = check_neutral_job(root, config, scheduler=scheduler)

            self.assertEqual(state.neutral.status, "RUNNING")

    def test_check_neutral_can_adopt_existing_job_id(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import check_neutral_job
        from vaspsolkit.scheduler import JobState

        class FakeScheduler:
            def status(self, job_id):
                self.seen = job_id
                return JobState(job_id=job_id, exists=True, state="Q")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            scheduler = FakeScheduler()
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))

            state = check_neutral_job(
                root,
                config,
                scheduler=scheduler,
                job_id="126544.server",
            )

            self.assertEqual(scheduler.seen, "126544.server")
            self.assertEqual(state.neutral.job_id, "126544.server")
            self.assertEqual(state.neutral.status, "QUEUED")

    def test_check_neutral_accepts_user_selected_valid_ionic_optimizer(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import check_neutral_job
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def status(self, job_id):
                return JobState(job_id=job_id, exists=True, state="Q")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "INCAR").write_text("IBRION = 1\nNSW = 88\n", encoding="utf-8")
            WorkflowState(
                stage="neutral_submitted",
                neutral=JobRecord(
                    folder=".",
                    status="SUBMITTED",
                    job_id="neutral-1",
                    metadata={"stage": "neutral_relax"},
                ),
            ).save(root / "vaspsolkit.state.json")
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))

            state = check_neutral_job(
                root,
                config,
                scheduler=FakeScheduler(),
                require_relaxation=True,
            )

        self.assertEqual(state.neutral.status, "QUEUED")

    def test_check_neutral_accepts_complete_supplied_outputs_without_job_id(self):
        from vaspsolkit.config import KitConfig, SchedulerConfig
        from vaspsolkit.orchestrator import check_neutral_job

        class FakeScheduler:
            def status(self, job_id):
                raise AssertionError("no scheduler query is needed for supplied outputs")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            (root / "OUTCAR").write_text(
                "NSW = 0\nIBRION = -1\n"
                "NELECT = 64.0000 total number of electrons\n"
                "E-fermi : -5.0000\n"
                "free  energy   TOTEN  = -324.5787 eV\n"
                "aborting loop because EDIFF is reached\n",
                encoding="utf-8",
            )
            for filename in ("CONTCAR", "CHGCAR", "LOCPOT"):
                (root / filename).write_text("complete\n", encoding="utf-8")
            config = KitConfig(scheduler=SchedulerConfig(kind="slurm", script="vasp.slurm"))

            state = check_neutral_job(root, config, scheduler=FakeScheduler())

            self.assertEqual(state.neutral.status, "CONVERGED")
            self.assertEqual(state.neutral.job_id, "")


class VaspsolkitCliTests(unittest.TestCase):
    def test_submit_neutral_resource_flags_reach_reviewed_request(self):
        from vaspsolkit.cli import main
        from vaspsolkit.config import KitConfig, write_kit_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kit_config(root / "vaspsolkit.json", KitConfig())
            with patch("vaspsolkit.cli._submit_neutral_once", return_value=0) as submit:
                code = main(
                    [
                        "submit-neutral",
                        "--workdir",
                        str(root),
                        "--yes",
                        "--resource-allocation",
                        "specified",
                        "--resource-node",
                        "node24",
                        "--resource-tasks",
                        "40",
                        "--save-resources",
                    ]
                )

        self.assertEqual(code, 0)
        resources = submit.call_args.kwargs["resources"]
        self.assertEqual(resources.nodes, ("node24",))
        self.assertEqual(resources.tasks, 40)
        self.assertTrue(resources.persist)

    def test_submit_selected_resource_flags_reach_reviewed_request(self):
        from vaspsolkit.cli import main
        from vaspsolkit.config import KitConfig, write_kit_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kit_config(root / "vaspsolkit.json", KitConfig())
            with patch("vaspsolkit.cli._submit_selected_once", return_value=0) as submit:
                code = main(
                    [
                        "submit-selected",
                        "--workdir",
                        str(root),
                        "--yes",
                        "2",
                        "--resource-allocation",
                        "auto",
                        "--resource-tasks",
                        "32",
                    ]
                )

        self.assertEqual(code, 0)
        resources = submit.call_args.kwargs["resources"]
        self.assertEqual(resources.allocation, "auto")
        self.assertEqual(resources.nodes, ())
        self.assertEqual(resources.tasks, 32)
        self.assertFalse(resources.persist)

    def test_auto_resource_flags_reject_named_node_before_submission(self):
        from vaspsolkit.cli import main
        from vaspsolkit.config import KitConfig, write_kit_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kit_config(root / "vaspsolkit.json", KitConfig())
            with patch("vaspsolkit.cli._submit_neutral_once") as submit:
                with self.assertRaisesRegex(
                    ValueError, "auto allocation cannot specify nodes"
                ):
                    main(
                        [
                            "submit-neutral",
                            "--workdir",
                            str(root),
                            "--yes",
                            "--resource-allocation",
                            "auto",
                            "--resource-node",
                            "node24",
                        ]
                    )

        submit.assert_not_called()

    def test_numbered_menu_exposes_the_staged_workflow_actions(self):
        from vaspsolkit.menu_actions import MENU_ACTIONS

        commands = {item.command for item in MENU_ACTIONS if item.command}
        codes = {item.code for item in MENU_ACTIONS}

        self.assertIn("prepare-neutral", commands)
        self.assertIn("submit-neutral", commands)
        self.assertIn("prepare-charge", commands)
        self.assertIn("check-prepared", commands)
        self.assertIn("submit-selected", commands)
        self.assertIn("monitor", commands)
        self.assertNotIn("run", commands)
        self.assertTrue(
            {"01", "02", "03", "10", "20", "30", "40", "50", "60", "90", "00"}
            <= codes
        )

    def test_monitor_does_not_submit_prepared_charge_jobs(self):
        from vaspsolkit.cli import main
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.submit_calls = 0

            def inspect(self):
                return []

            def status(self, job_id):
                raise AssertionError("monitor should not query prepared jobs")

            def submit(self, folder, script, **kwargs):
                self.submit_calls += 1
                return "unexpected"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vaspsolkit.json").write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "profile": "vaspsol-sweep",
                        "workflow": {},
                        "scheduler": {"kind": "slurm", "script": "vasp.slurm"},
                    }
                ),
                encoding="utf-8",
            )
            WorkflowState(
                stage="charge_ready",
                prepared_checked=True,
                jobs={"1": JobRecord(folder="charge_sweep/1")},
            ).save(root / "vaspsolkit.state.json")
            scheduler = FakeScheduler()
            output = []
            with patch("vaspsolkit.cli.scheduler_from_config", return_value=scheduler):
                result = main(["monitor", "--workdir", str(root)], output=output.append)

        self.assertEqual(result, 0)
        self.assertEqual(scheduler.submit_calls, 0)
        self.assertTrue(any("PREPARED" in line for line in output))

    def test_monitor_refreshes_neutral_scheduler_state_without_convergence_check(self):
        from vaspsolkit.cli import main
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def inspect(self):
                return []

            def status(self, job_id):
                return type("Status", (), {"state": "R"})()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vaspsolkit.json").write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "profile": "vaspsol-sweep",
                        "workflow": {},
                        "scheduler": {"kind": "slurm", "script": "vasp.slurm"},
                    }
                ),
                encoding="utf-8",
            )
            WorkflowState(
                stage="neutral_submitted",
                neutral=JobRecord(
                    folder=".",
                    status="SUBMITTED",
                    job_id="neutral-1",
                    metadata={"stage": "neutral_relax"},
                ),
            ).save(root / "vaspsolkit.state.json")
            output = []
            with patch("vaspsolkit.cli.scheduler_from_config", return_value=FakeScheduler()):
                result = main(["monitor", "--workdir", str(root)], output=output.append)

        self.assertEqual(result, 0)
        self.assertTrue(any("neutral: RUNNING job=neutral-1" in line for line in output))

    def test_bare_command_opens_current_directory_menu_in_tty(self):
        from vaspsolkit.cli import main

        with patch("vaspsolkit.cli._has_interactive_tty", return_value=True), patch(
            "vaspsolkit.cli._open_menu", return_value=0
        ) as open_menu:
            result = main([])

        self.assertEqual(result, 0)
        args, kwargs = open_menu.call_args
        self.assertEqual(args[0], Path("."))
        self.assertIs(kwargs["input_fn"], input)

    def test_submit_selected_cli_passes_explicit_charge_names(self):
        from vaspsolkit.cli import main
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vaspsolkit.json").write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "workflow": {},
                        "scheduler": {"kind": "slurm", "script": "vasp.slurm"},
                    }
                ),
                encoding="utf-8",
            )
            WorkflowState(
                jobs={"1": JobRecord("charge_sweep/1", status="PREPARED")},
                prepared_checked=True,
            ).save(root / "vaspsolkit.state.json")
            output = []
            with patch("vaspsolkit.cli._submit_selected_once", return_value=0) as submit:
                result = main(
                    ["submit-selected", "--workdir", str(root), "1", "--yes"],
                    output=output.append,
                )

        self.assertEqual(result, 0)
        self.assertEqual(submit.call_args.args[3], ["1"])
        self.assertEqual(submit.call_args.kwargs["resources"].allocation, "auto")

    def test_migrate_command_writes_nested_config(self):
        from vaspsolkit.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "vaspsolflow.json"
            target = root / "vaspsolkit.json"
            legacy.write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "workflow": {"folders": ["1", "2", "3"], "nelect_offsets": [-1, 0, 1]},
                        "scheduler": {"kind": "slurm", "partition": "compute", "tasks": 48},
                    }
                ),
                encoding="utf-8",
            )

            result = main(
                ["migrate", "--input", str(legacy), "--output", str(target), "--yes"]
            )

            migrated = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(migrated["config_version"], 2)
            self.assertIn("workflow", migrated)
            self.assertIn("scheduler", migrated)
            self.assertEqual(migrated["scheduler"]["partition"], "compute")

    def test_noninteractive_init_applies_profile_and_writes_state(self):
        from vaspsolkit.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "POSCAR").write_text(
                "sample\n1\n1 0 0\n0 1 0\n0 0 1\nC\n1\nDirect\n0 0 0\n",
                encoding="utf-8",
            )
            (root / "POTCAR").write_text(
                "TITEL = PAW_PBE C 08Apr2002\nENMAX = 400.0 eV\n",
                encoding="utf-8",
            )
            (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
            (root / "INCAR").write_text("ISPIN = 1\n", encoding="utf-8")
            (root / "vasp.slurm").write_text("#!/bin/bash\nsrun vasp_std\n", encoding="utf-8")

            result = main(
                [
                    "init",
                    "--workdir",
                    str(root),
                    "--profile",
                    "static",
                    "--scheduler",
                    "slurm",
                    "--script",
                    "vasp.slurm",
                    "--she-reference",
                    "4.70",
                    "--yes",
                ]
            )

            config = json.loads((root / "vaspsolkit.json").read_text(encoding="utf-8"))
            incar = (root / "INCAR").read_text(encoding="utf-8")
            self.assertEqual(result, 0)
            self.assertEqual(config["profile"], "static")
            self.assertEqual(config["scheduler"]["kind"], "slurm")
            self.assertIn("ENCUT = 520", incar)
            self.assertIn("NSW = 0", incar)
            self.assertTrue((root / "vaspsolkit.state.json").exists())

    def test_init_with_existing_incar_skips_profile_and_preserves_user_settings(self):
        from vaspsolkit.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "POSCAR").write_text(
                "sample\n1\n1 0 0\n0 1 0\n0 0 1\nC\n1\nDirect\n0 0 0\n",
                encoding="utf-8",
            )
            (root / "POTCAR").write_text(
                "TITEL = PAW_PBE C 08Apr2002\nENMAX = 400.0 eV\n",
                encoding="utf-8",
            )
            (root / "KPOINTS").write_text("Gamma\n0\nGamma\n1 1 1\n0 0 0\n", encoding="utf-8")
            (root / "INCAR").write_text(
                "ENCUT = 450\nGGA = RP\nIBRION = 1\nNSW = 88\nEDIFFG = -0.03\n",
                encoding="utf-8",
            )
            (root / "vasp.slurm").write_text("#!/bin/bash\n", encoding="utf-8")

            result = main(
                [
                    "init",
                    "--workdir",
                    str(root),
                    "--scheduler",
                    "slurm",
                    "--script",
                    "vasp.slurm",
                    "--she-reference",
                    "4.70",
                    "--yes",
                ],
                input_fn=lambda prompt: self.fail(f"unexpected prompt: {prompt}"),
            )

            incar = (root / "INCAR").read_text(encoding="utf-8")
            config = json.loads((root / "vaspsolkit.json").read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(config["profile"], "vaspsol-sweep")
        self.assertEqual(config["workflow"]["neutral_profile"], "vaspsol-neutral-relax")
        self.assertIn("ENCUT = 450", incar)
        self.assertIn("IBRION = 1", incar)
        self.assertIn("NSW = 88", incar)
        self.assertIn("LSOL = .TRUE.", incar)
        self.assertNotIn("ENCUT = 520", incar)

    def test_submit_neutral_cli_returns_without_scheduler_polling(self):
        from vaspsolkit.cli import main
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.submit_calls = 0
                self.status_calls = 0

            def submit(self, folder, script, **kwargs):
                self.submit_calls += 1
                return "123"

            def status(self, job_id):
                self.status_calls += 1
                raise AssertionError("submit-neutral must not poll")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            (root / "vaspsolkit.json").write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "profile": "vaspsol-sweep",
                        "workflow": {},
                        "scheduler": {"kind": "slurm", "script": "vasp.slurm"},
                    }
                ),
                encoding="utf-8",
            )
            WorkflowState(
                stage="neutral_prepared",
                neutral=JobRecord(
                    folder=".",
                    status="PREPARED",
                    metadata={"stage": "neutral_relax"},
                ),
            ).save(root / "vaspsolkit.state.json")
            scheduler = FakeScheduler()
            output = []
            with patch("vaspsolkit.cli.scheduler_from_config", return_value=scheduler):
                result = main(["submit-neutral", "--workdir", str(root), "--yes"], output=output.append)

            self.assertEqual(result, 0)
            self.assertEqual(scheduler.submit_calls, 1)
            self.assertEqual(scheduler.status_calls, 0)
            self.assertTrue(any("123" in line for line in output))

    def test_run_dispatcher_submits_neutral_once_and_returns(self):
        from vaspsolkit.cli import main
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.submit_calls = 0
                self.status_calls = 0

            def submit(self, folder, script, **kwargs):
                self.submit_calls += 1
                return "123"

            def status(self, job_id):
                self.status_calls += 1
                raise AssertionError("non-blocking run must not poll")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for filename in ("INCAR", "POSCAR", "POTCAR", "KPOINTS", "vasp.slurm"):
                (root / filename).write_text("input\n", encoding="utf-8")
            (root / "vaspsolkit.json").write_text(
                json.dumps(
                    {
                        "config_version": 1,
                        "profile": "vaspsol-sweep",
                        "workflow": {},
                        "scheduler": {"kind": "slurm", "script": "vasp.slurm"},
                    }
                ),
                encoding="utf-8",
            )
            WorkflowState(
                stage="neutral_prepared",
                neutral=JobRecord(
                    folder=".",
                    status="PREPARED",
                    metadata={"stage": "neutral_relax"},
                ),
            ).save(root / "vaspsolkit.state.json")
            scheduler = FakeScheduler()
            with patch("vaspsolkit.cli.scheduler_from_config", return_value=scheduler):
                result = main(["run", "--workdir", str(root), "--yes"])

            self.assertEqual(result, 0)
            self.assertEqual(scheduler.submit_calls, 1)
            self.assertEqual(scheduler.status_calls, 0)


if __name__ == "__main__":
    unittest.main()


class VaspsolkitResetQueuedTests(unittest.TestCase):
    def test_refresh_state_recovers_missing_unstarted_job_to_prepared(self):
        from vaspsolkit.config import KitConfig
        from vaspsolkit.orchestrator import refresh_state
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def status(self, job_id):
                return JobState(job_id=job_id, exists=False, state="MISSING")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "vaspsolkit.state.json"
            state = WorkflowState(
                stage="monitor",
                prepared_checked=True,
                jobs={
                    "2": JobRecord(
                        folder="charge_sweep/2",
                        status="QUEUED",
                        job_id="127913.node01.example.invalid",
                    )
                },
            )
            (root / "charge_sweep" / "2").mkdir(parents=True)
            state.save(state_path)

            refresh_state(root, KitConfig(), state, scheduler=FakeScheduler())
            loaded = WorkflowState.load(state_path)

        self.assertEqual(loaded.jobs["2"].status, "PREPARED")
        self.assertEqual(loaded.jobs["2"].job_id, "")
        self.assertEqual(loaded.jobs["2"].diagnostics, [])

    def test_refresh_state_keeps_missing_job_with_runtime_output_for_review(self):
        from vaspsolkit.config import KitConfig
        from vaspsolkit.orchestrator import refresh_state
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def status(self, job_id):
                return JobState(job_id=job_id, exists=False, state="MISSING")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "vaspsolkit.state.json"
            folder = root / "charge_sweep" / "2"
            folder.mkdir(parents=True)
            (folder / "OSZICAR").write_text("1 F= -12.3\n", encoding="utf-8")
            state = WorkflowState(
                jobs={
                    "2": JobRecord(
                        folder="charge_sweep/2",
                        status="QUEUED",
                        job_id="127913.node01.example.invalid",
                    )
                }
            )
            state.save(state_path)

            refresh_state(root, KitConfig(), state, scheduler=FakeScheduler())
            loaded = WorkflowState.load(state_path)

        self.assertEqual(loaded.jobs["2"].status, "NEEDS_REVIEW")
        self.assertEqual(loaded.jobs["2"].job_id, "127913.node01.example.invalid")
        self.assertIn("outcar_missing", loaded.jobs["2"].diagnostics)

    def test_reset_queued_jobs_skips_scancel_for_missing_unstarted_job(self):
        from vaspsolkit.orchestrator import reset_queued_jobs
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.cancelled = []

            def status(self, job_id):
                return JobState(job_id=job_id, exists=False, state="MISSING")

            def cancel(self, job_id):
                self.cancelled.append(job_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "vaspsolkit.state.json"
            state = WorkflowState(
                stage="monitor",
                prepared_checked=True,
                jobs={
                    "2": JobRecord(
                        folder="charge_sweep/2",
                        status="QUEUED",
                        job_id="127913.node01.example.invalid",
                    )
                },
            )
            (root / "charge_sweep" / "2").mkdir(parents=True)
            state.save(state_path)
            scheduler = FakeScheduler()

            reset_queued_jobs(root, state, ["2"], scheduler=scheduler, confirmed=True)
            loaded = WorkflowState.load(state_path)

        self.assertEqual(scheduler.cancelled, [])
        self.assertEqual(loaded.jobs["2"].status, "PREPARED")
        self.assertEqual(loaded.jobs["2"].job_id, "")

    def test_reset_queued_jobs_refuses_running_preflight_without_cancelling_any_job(self):
        from vaspsolkit.orchestrator import reset_queued_jobs
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.cancelled = []

            def status(self, job_id):
                state = "Q" if job_id == "queued.job" else "R"
                return JobState(job_id=job_id, exists=True, state=state)

            def cancel(self, job_id):
                self.cancelled.append(job_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = WorkflowState(
                jobs={
                    "2": JobRecord(folder="charge_sweep/2", status="QUEUED", job_id="queued.job"),
                    "3": JobRecord(folder="charge_sweep/3", status="QUEUED", job_id="running.job"),
                }
            )
            state.save(root / "vaspsolkit.state.json")
            scheduler = FakeScheduler()

            with self.assertRaisesRegex(RuntimeError, "RUNNING/UNKNOWN"):
                reset_queued_jobs(root, state, ["2", "3"], scheduler=scheduler, confirmed=True)

        self.assertEqual(scheduler.cancelled, [])
        self.assertEqual(state.jobs["2"].status, "QUEUED")
        self.assertEqual(state.jobs["3"].status, "QUEUED")

    def test_reset_queued_jobs_recovers_when_scancel_races_with_missing_job(self):
        from vaspsolkit.orchestrator import reset_queued_jobs
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.cancelled = []
                self.status_calls = 0

            def status(self, job_id):
                self.status_calls += 1
                state = "MISSING" if self.status_calls >= 3 else "Q"
                return JobState(job_id=job_id, exists=state != "MISSING", state=state)

            def cancel(self, job_id):
                self.cancelled.append(job_id)
                raise RuntimeError(f"scancel failed for {job_id}: nonexistent job id")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "vaspsolkit.state.json"
            state = WorkflowState(
                jobs={
                    "2": JobRecord(
                        folder="charge_sweep/2",
                        status="QUEUED",
                        job_id="127913.node01.example.invalid",
                    )
                }
            )
            (root / "charge_sweep" / "2").mkdir(parents=True)
            state.save(state_path)
            scheduler = FakeScheduler()

            reset_queued_jobs(root, state, ["2"], scheduler=scheduler, confirmed=True)
            loaded = WorkflowState.load(state_path)

        self.assertEqual(scheduler.cancelled, ["127913.node01.example.invalid"])
        self.assertEqual(loaded.jobs["2"].status, "PREPARED")
        self.assertEqual(loaded.jobs["2"].job_id, "")

    def test_reset_queued_jobs_cancels_and_marks_prepared(self):
        from vaspsolkit.orchestrator import reset_queued_jobs
        from vaspsolkit.scheduler import JobState
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.cancelled = []

            def cancel(self, job_id):
                self.cancelled.append(job_id)

            def status(self, job_id):
                return JobState(job_id=job_id, exists=True, state="Q")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "vaspsolkit.state.json"
            state = WorkflowState(
                stage="monitor",
                prepared_checked=True,
                jobs={
                    "2": JobRecord(
                        folder="charge_sweep/2",
                        status="QUEUED",
                        job_id="127913.node01.example.invalid",
                        diagnostics=["waiting"],
                    )
                },
            )
            state.save(state_path)
            scheduler = FakeScheduler()

            reset = reset_queued_jobs(
                root,
                state,
                ["2"],
                scheduler=scheduler,
                confirmed=True,
            )
            loaded = WorkflowState.load(state_path)

        self.assertEqual(reset, {"2": "127913.node01.example.invalid"})
        self.assertEqual(scheduler.cancelled, ["127913.node01.example.invalid"])
        self.assertEqual(loaded.jobs["2"].status, "PREPARED")
        self.assertEqual(loaded.jobs["2"].job_id, "")
        self.assertEqual(loaded.jobs["2"].diagnostics, [])
        self.assertEqual(loaded.stage, "charge_ready")
        self.assertTrue(loaded.prepared_checked)

    def test_reset_queued_jobs_rejects_running_jobs(self):
        from vaspsolkit.orchestrator import reset_queued_jobs
        from vaspsolkit.state import JobRecord, WorkflowState

        class FakeScheduler:
            def __init__(self):
                self.cancelled = []

            def cancel(self, job_id):
                self.cancelled.append(job_id)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = WorkflowState(
                stage="monitor",
                jobs={
                    "2": JobRecord(
                        folder="charge_sweep/2",
                        status="RUNNING",
                        job_id="127913.node01.example.invalid",
                    )
                },
            )
            state.save(root / "vaspsolkit.state.json")
            scheduler = FakeScheduler()

            with self.assertRaisesRegex(RuntimeError, "not QUEUED/SUBMITTED"):
                reset_queued_jobs(root, state, ["2"], scheduler=scheduler, confirmed=True)

        self.assertEqual(scheduler.cancelled, [])

    def test_reset_queued_cli_passes_selected_jobs_and_reports_result(self):
        from vaspsolkit.cli import main
        from vaspsolkit.config import KitConfig, write_kit_config
        from vaspsolkit.state import JobRecord, WorkflowState

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_kit_config(root / "vaspsolkit.json", KitConfig())
            WorkflowState(
                jobs={"2": JobRecord(folder="charge_sweep/2", status="QUEUED", job_id="old.job")}
            ).save(root / "vaspsolkit.state.json")
            output = []
            with patch("vaspsolkit.cli.scheduler_from_config", return_value=object()) as scheduler_factory:
                with patch("vaspsolkit.cli.reset_queued_jobs", return_value={"2": "old.job"}) as reset:
                    code = main(
                        ["reset-queued", "--workdir", str(root), "2", "--yes"],
                        output=lambda value: output.append(str(value)),
                    )

        self.assertEqual(code, 0)
        scheduler_factory.assert_called_once()
        args, kwargs = reset.call_args
        self.assertEqual(args[0], root.resolve())
        self.assertEqual(args[2], ["2"])
        self.assertTrue(kwargs["confirmed"])
        self.assertIn("2: reset old_job=old.job", output)

    def test_select_node_cli_reuses_scheduler_configuration_flow(self):
        from vaspsolkit.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = []
            with patch("vaspsolkit.cli._cmd_configure_scheduler", return_value=0) as configure:
                code = main(
                    ["select-node", "--workdir", str(root)],
                    output=lambda value: output.append(str(value)),
                )

        self.assertEqual(code, 0)
        configure.assert_called_once()
