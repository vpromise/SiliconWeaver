from __future__ import annotations

import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from rtl_closure.backend import _chat_messages
from rtl_closure.oracle import (
    PYTHON_ORACLE_SYSTEM_PROMPT,
    _build_python_oracle_prompt,
)


class IndependenceTests(unittest.TestCase):
    def test_chat_messages_preserve_system_user_order(self):
        self.assertEqual(
            _chat_messages("user prompt", "system prompt"),
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
        )
        self.assertEqual(
            _chat_messages("user prompt"),
            [{"role": "user", "content": "user prompt"}],
        )

    def test_oracle_prompt_binds_interface_and_skeleton(self):
        prompt = _build_python_oracle_prompt(
            "Implement a counter.",
            "module top_module(input clk, output logic count); endmodule",
            "class TopModule:\n    def eval(self, inputs): ...",
        )

        self.assertIn("Implement a counter.", prompt)
        self.assertIn("module top_module", prompt)
        self.assertIn("class TopModule", prompt)
        self.assertIn("one rising clock edge", PYTHON_ORACLE_SYSTEM_PROMPT)

    def test_runtime_dependencies_are_declared_locally(self):
        repo = Path(__file__).parents[1]
        project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            project["project"]["dependencies"],
            ["openai>=3.3.1,<4", "PyYAML>=5,<7"],
        )

    def test_cli_imports_from_source_tree(self):
        repo = Path(__file__).parents[1]
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(repo / 'src')!r}); "
            "import rtl_closure.cli as cli; "
            "assert cli.__file__.endswith('rtl_closure/cli.py')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
