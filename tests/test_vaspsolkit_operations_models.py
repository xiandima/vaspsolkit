from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkbenchModelTests(unittest.TestCase):
    def _write_case(self, root: Path) -> None:
        (root / "POSCAR").write_text(
            "PtO\n1\n1 0 0\n0 1 0\n0 0 1\nPt O\n64 1\nDirect\n",
            encoding="utf-8",
        )
        (root / "INCAR").write_text("IBRION = 2\nNSW = 100\n", encoding="utf-8")
        (root / "KPOINTS").write_text("Gamma\n", encoding="utf-8")
        (root / "POTCAR").write_text("potcar\n", encoding="utf-8")
        (root / "vasp.pbs").write_text("#!/bin/bash\n", encoding="utf-8")

    def test_model_exposes_navigation_and_real_read_only_case_data(self):
        from vaspsolkit.operations.models import build_workbench_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)

            model = build_workbench_model(root)

        self.assertEqual(
            [item.key for item in model.navigation],
            ["overview", "inputs", "neutral", "charges", "queue", "results", "settings"],
        )
        self.assertEqual(model.system_text, "Pt O · 65 atoms")
        self.assertTrue(model.read_only_prototype)
        self.assertFalse(model.external_commands_enabled)
        self.assertEqual(len(model.workflow_steps), 7)
        self.assertEqual(model.charge_rows, ())
        self.assertEqual(model.case_queue_rows, ())
        self.assertEqual(model.node_rows, ())
        self.assertEqual(model.global_queue_rows, ())
        self.assertEqual(model.queue_tabs, ("case", "nodes", "global"))
        self.assertEqual(len(model.result_rows), 2)
        self.assertEqual(len(model.activities), 3)
        self.assertNotIn("example.invalid", repr(model))
        self.assertFalse(any(row.demo for row in model.result_rows))

    def test_translation_keeps_technical_terms_and_rejects_unknown_language(self):
        from vaspsolkit.operations.i18n import tr

        self.assertEqual(tr("zh", "nav.charges"), "带电点")
        self.assertEqual(tr("en", "nav.charges"), "Charge points")
        self.assertEqual(tr("zh", "nav.workspace"), "首页")
        self.assertEqual(tr("en", "nav.tasks"), "Tasks")
        self.assertEqual(tr("zh", "technical.nelect"), "NELECT")
        with self.assertRaisesRegex(ValueError, "unsupported language"):
            tr("fr", "nav.charges")

    def test_unreadable_incar_does_not_crash_read_only_model(self):
        from vaspsolkit.operations.models import build_workbench_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)
            original = Path.read_text

            def guarded_read(path, *args, **kwargs):
                if path.name == "INCAR":
                    raise PermissionError("denied")
                return original(path, *args, **kwargs)

            with patch.object(Path, "read_text", guarded_read):
                model = build_workbench_model(root)

        self.assertIn("INCAR: 存在", model.input_details)

    def test_model_inspection_neither_changes_case_nor_calls_external_commands(self):
        from vaspsolkit.operations.models import build_workbench_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_case(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with patch("subprocess.run", side_effect=AssertionError("external command called")):
                model = build_workbench_model(root)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

        self.assertEqual(before, after)
        self.assertTrue(model.read_only_prototype)


if __name__ == "__main__":
    unittest.main()
