from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import unittest

from rtl_closure import cli


class CliTests(unittest.TestCase):
    def test_version_reports_distribution_version(self) -> None:
        parser = cli.build_parser()
        parser.prog = "rtl-closure"
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "rtl-closure 0.6.1\n")


if __name__ == "__main__":
    unittest.main()
