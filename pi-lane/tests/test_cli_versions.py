from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import unittest

from pi_lane import cli, orchestrator_cli, orchestrator_server


class CliVersionTests(unittest.TestCase):
    def assert_version(self, builder, command: str) -> None:
        parser = builder()
        parser.prog = command
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), f"{command} 0.6.0\n")

    def test_public_commands_report_distribution_version(self) -> None:
        for builder, command in (
            (cli.build_parser, "lane-agent"),
            (orchestrator_cli.build_parser, "pi-lanectl"),
            (orchestrator_server.build_parser, "pi-lane-orchestratord"),
        ):
            with self.subTest(command=command):
                self.assert_version(builder, command)


if __name__ == "__main__":
    unittest.main()
