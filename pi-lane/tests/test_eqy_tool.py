from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from pi_lane.eqy_tool import awk_main, main


class EqyToolTests(unittest.TestCase):
    def test_runs_eqy_style_dependency_and_recipe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "strategies.mk").write_text(
                ".DEFAULT_GOAL := all\n\n"
                "status:\n"
                "\t@echo PASS > $@\n\n"
                ".PHONY: all\n"
                "all: status\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                self.assertEqual(
                    main(["-C", str(root), "-f", "strategies.mk"]), 0
                )
            finally:
                os.chdir(previous)
            self.assertEqual((root / "status").read_text(), "PASS\n")

    def test_awk_compatibility_is_restricted_to_first_field(self):
        with tempfile.TemporaryDirectory() as temp:
            status = Path(temp) / "status"
            status.write_text("PASS details\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(awk_main(["{print $1}", str(status)]), 0)
            self.assertEqual(output.getvalue(), "PASS\n")
            with self.assertRaisesRegex(ValueError, "unsupported awk"):
                awk_main(["{print $2}", str(status)])


if __name__ == "__main__":
    unittest.main()
