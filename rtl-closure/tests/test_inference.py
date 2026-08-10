from __future__ import annotations

import unittest

from rtl_closure.inference import run_problem


GOOD_RTL = """```systemverilog
module top_module(input logic a, output logic y);
  assign y = a;
endmodule
```"""
BAD_RTL = """```systemverilog
module top_module(input logic a, output logic y);
  assign y = 1'b0;
endmodule
```"""
GOOD_PYTHON = """```python
class TopModule:
    def eval(self, inputs):
        return {"y": inputs["a"]}
```"""
BAD_PYTHON = """```python
class TopModule:
    def eval(self, inputs):
        return {"y": 0}
```"""


class SequenceBackend:
    def __init__(self, turns: list[list[str]]):
        self.turns = turns
        self.calls: list[tuple[str, int, float]] = []

    def generate(
        self,
        prompt: str,
        n: int = 1,
        temperature: float = 0.6,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
    ) -> list[str]:
        del max_tokens, system_prompt
        self.calls.append((prompt, n, temperature))
        return self.turns[len(self.calls) - 1]


class InferenceTests(unittest.TestCase):
    def test_best_of_n_selects_verified_pair(self):
        rtl_backend = SequenceBackend([[BAD_RTL, GOOD_RTL]])
        python_backend = SequenceBackend([[BAD_PYTHON, GOOD_PYTHON]])

        def verifier(ref_sv, dut_sv, dut_py, num_tests, seed):
            del ref_sv, num_tests, seed
            matched = "assign y = a" in dut_sv and 'inputs["a"]' in dut_py
            return {
                "match_rate": 1.0 if matched else 0.25,
                "mismatches": [] if matched else [{"signal": "y"}],
                "total_checks": 4,
                "mismatched": 0 if matched else 3,
            }

        result = run_problem(
            task_id="case-1",
            question="Pass a to y.",
            ref_sv="module top_module(input logic a, output logic y); endmodule",
            v_backend=rtl_backend,
            p_backend=python_backend,
            verifier=verifier,
            n=2,
            max_turns=2,
            num_verify_tests=9,
            temperature=0.2,
            seed=17,
        )

        self.assertTrue(result.matched)
        self.assertEqual(result.best_match_rate, 1.0)
        self.assertEqual(result.best_turn, 0)
        self.assertEqual(result.turns_used, 1)
        self.assertIn("assign y = a", result.verilog)
        self.assertIn('inputs["a"]', result.python)
        self.assertEqual(rtl_backend.calls[0][1:], (2, 0.2))

    def test_valid_zero_score_beats_unextractable_tie(self):
        rtl_backend = SequenceBackend([["not RTL", BAD_RTL]])
        python_backend = SequenceBackend([[GOOD_PYTHON, BAD_PYTHON]])

        result = run_problem(
            task_id="case-2",
            question="Pass a to y.",
            ref_sv="module top_module(input logic a, output logic y); endmodule",
            v_backend=rtl_backend,
            p_backend=python_backend,
            verifier=lambda *_args: {"match_rate": 0.0, "mismatches": []},
            n=2,
            max_turns=1,
        )

        self.assertFalse(result.matched)
        self.assertIn("module top_module", result.verilog)
        self.assertNotIn("No complete", result.verilog)

    def test_all_invalid_responses_do_not_become_artifacts(self):
        backend = SequenceBackend([["plain explanation"]])
        result = run_problem(
            task_id="case-3",
            question="Pass a to y.",
            ref_sv="module top_module(input logic a, output logic y); endmodule",
            v_backend=backend,
            p_backend=SequenceBackend([["def broken(:"]]),
            verifier=lambda *_args: {"match_rate": 1.0},
            n=1,
            max_turns=1,
        )

        self.assertFalse(result.matched)
        self.assertEqual(result.verilog, "")
        self.assertEqual(result.python, "")
        self.assertEqual(result.best_turn, -1)


if __name__ == "__main__":
    unittest.main()
