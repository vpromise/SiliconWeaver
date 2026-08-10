from __future__ import annotations

import json
import unittest

from rtl_closure.feedback import (
    apply_block_edits,
    build_failure_packet,
    dependency_slice,
    extract_rtl_blocks,
)
from rtl_closure.repair import candidate_improves, verification_score


class FeedbackTests(unittest.TestCase):
    def test_slice_tracks_intra_block_and_index_dependencies(self):
        rtl = """module demo(input wire clk, input wire [1:0] idx,
          input wire d, output reg y);
          reg [3:0] mem;
          reg q;
          always @(posedge clk) begin
            mem[idx] <= d;
            q <= mem[idx];
            y <= q;
          end
        endmodule"""
        blocks = extract_rtl_blocks(rtl)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(set(blocks[0].writes), {"mem", "q", "y"})
        self.assertIn("idx", blocks[0].reads)
        self.assertIn("q", blocks[0].reads)
        self.assertEqual(dependency_slice(blocks, "y"), blocks)

    def test_relational_less_equal_is_not_authorized_as_a_write(self):
        rtl = (
            "module d(input clk,a,b,output reg y); "
            "always @(posedge clk) begin if (a <= b) y <= a; end endmodule"
        )
        block = extract_rtl_blocks(rtl)[0]
        self.assertEqual(block.writes, ("y",))
        self.assertIn("a", block.reads)
        self.assertIn("b", block.reads)
        with self.assertRaisesRegex(ValueError, "write-set"):
            apply_block_edits(
                rtl,
                [
                    {
                        "block_id": "B001",
                        "replacement": (
                            "always @(posedge clk) begin if (a <= b) begin "
                            "a <= b; y <= a; end end"
                        ),
                    }
                ],
                [block],
            )

    def test_concatenated_lvalue_records_each_written_signal(self):
        rtl = (
            "module d(input clk,a,b,output reg y,z); "
            "always @(posedge clk) {y, z} <= {a, b}; endmodule"
        )
        block = extract_rtl_blocks(rtl)[0]
        self.assertEqual(set(block.writes), {"y", "z"})
        self.assertTrue({"a", "b"} <= set(block.reads))

    def test_failure_packet_finds_one_cycle_late_behavior(self):
        actual_trace = []
        for ordinal, (expected, actual) in enumerate(
            [(0, 0), (1, 0), (2, 1), (3, 2)]
        ):
            actual_trace.append(
                {
                    "ordinal": ordinal,
                    "scenario_id": "TEST_LATENCY",
                    "cycle": ordinal,
                    "inputs": {"enable": 1},
                    "expected": {"y": expected},
                    "actual": {"y": actual},
                }
            )
        result = {
            "passed": False,
            "failure_class": "value_mismatch",
            "first_failure_ordinal": 1,
            "mismatches": [
                {
                    "ordinal": 1,
                    "scenario_id": "TEST_LATENCY",
                    "requirement_ids": ["REQ_LATENCY"],
                    "cycle": 1,
                    "signal": "y",
                    "expected": 1,
                    "actual": 0,
                    "inputs": {"enable": 1},
                }
            ],
            "actual_trace": actual_trace,
        }
        rtl = "module d(input clk, output reg y); always @(posedge clk) y <= 0; endmodule"
        packet = build_failure_packet(result, rtl, trace_window_cycles=1)
        self.assertEqual(packet["failure_class"], "off_by_one_late")
        self.assertEqual(packet["first_failure_cycle"], 1)
        self.assertEqual(len(packet["trace_window"]), 3)
        self.assertEqual(packet["suspect_blocks"][0]["block_id"], "B001")

    def test_local_edit_cannot_expand_write_set(self):
        rtl = "module d(input clk, input a, output reg y); always @(posedge clk) y <= a; endmodule"
        blocks = extract_rtl_blocks(rtl)
        candidate = apply_block_edits(
            rtl,
            [{"block_id": "B001", "replacement": "always @(posedge clk) y <= ~a;"}],
            blocks,
        )
        self.assertIn("y <= ~a", candidate)
        with self.assertRaises(ValueError):
            apply_block_edits(
                rtl,
                [
                    {
                        "block_id": "B001",
                        "replacement": "always @(posedge clk) begin y <= a; q <= a; end",
                    }
                ],
                blocks,
            )

    def test_local_edit_cannot_change_clock_edge_or_sensitivity(self):
        rtl = (
            "module d(input clk,a,output reg y); "
            "always @(posedge clk) y <= a; endmodule"
        )
        blocks = extract_rtl_blocks(rtl)
        with self.assertRaisesRegex(ValueError, "event control"):
            apply_block_edits(
                rtl,
                [
                    {
                        "block_id": "B001",
                        "replacement": "always @(negedge clk) y <= a;",
                    }
                ],
                blocks,
            )

    def test_local_edit_cannot_smuggle_code_around_authorized_block(self):
        rtl = "module d(input clk, input a, output reg y); always @(posedge clk) y <= a; endmodule"
        blocks = extract_rtl_blocks(rtl)
        for replacement in (
            "wire hidden; always @(posedge clk) y <= ~a;",
            "always @(posedge clk) y <= ~a; wire hidden;",
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, "outside the authorized block"):
                    apply_block_edits(
                        rtl,
                        [{"block_id": "B001", "replacement": replacement}],
                        blocks,
                    )

    def test_block_extraction_keeps_outer_if_else_and_case_complete(self):
        conditional = (
            "module d(input clk,a,b,c,output reg y); "
            "always @(posedge clk) if (a) y <= b; else y <= c; endmodule"
        )
        blocks = extract_rtl_blocks(conditional)
        self.assertEqual(len(blocks), 1)
        self.assertIn("else y <= c;", blocks[0].text)

        case_rtl = (
            "module d(input [1:0] s,input a,b,output reg y); "
            "always @* case (s) 0: y = a; default: y = b; endcase endmodule"
        )
        blocks = extract_rtl_blocks(case_rtl)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].text.rstrip().endswith("endcase"))

    def test_candidate_ranking_rejects_historical_regression(self):
        champion = {
            "passed": False,
            "suite_sha256": "suite-a",
            "first_failure_ordinal": 2,
            "mismatched": 5,
            "passed_check_ids": ["A", "B"],
        }
        later_but_regressed = {
            "passed": False,
            "suite_sha256": "suite-a",
            "first_failure_ordinal": 5,
            "mismatched": 1,
            "passed_check_ids": ["A"],
        }
        accepted, reason = candidate_improves(champion, later_but_regressed)
        self.assertFalse(accepted)
        self.assertIn("historical regression", reason)
        improved = dict(later_but_regressed, passed_check_ids=["A", "B", "C"])
        self.assertTrue(candidate_improves(champion, improved)[0])
        self.assertGreater(verification_score(improved), verification_score(champion))

    def test_unknown_driver_does_not_authorize_every_rtl_block(self):
        rtl = """module d(input clk, input a, output reg y);
        reg q;
        always @(posedge clk) q <= a;
        always @(posedge clk) y <= q;
        endmodule"""
        blocks = extract_rtl_blocks(rtl)
        self.assertEqual(dependency_slice(blocks, "missing_signal"), [])

    def test_failure_packet_includes_internal_state_delta_and_failure_cluster(self):
        result = {
            "passed": False,
            "source": "simulation",
            "failure_class": "value_mismatch",
            "suite_sha256": "suite",
            "coverage": {"observed_trace_rows": 2},
            "first_failure_ordinal": 1,
            "mismatches": [
                {
                    "check_id": "A",
                    "ordinal": 1,
                    "scenario_index": 0,
                    "scenario_id": "TEST",
                    "requirement_ids": ["REQ"],
                    "cycle": 1,
                    "phase": "post_rising_edge",
                    "signal": "y",
                    "expected": 1,
                    "actual": 0,
                    "inputs": {"a": 1},
                },
                {
                    "check_id": "B",
                    "ordinal": 1,
                    "scenario_index": 0,
                    "scenario_id": "TEST",
                    "requirement_ids": ["REQ"],
                    "cycle": 1,
                    "phase": "post_rising_edge",
                    "signal": "z",
                    "expected": 1,
                    "actual": 0,
                    "inputs": {"a": 1},
                },
            ],
            "actual_trace": [
                {
                    "ordinal": 0,
                    "scenario_id": "TEST",
                    "cycle": 0,
                    "expected": {"y": 0},
                    "actual": {"y": 0},
                    "internal": {"state": 0},
                },
                {
                    "ordinal": 1,
                    "scenario_id": "TEST",
                    "cycle": 1,
                    "expected": {"y": 1},
                    "actual": {"y": 0},
                    "internal": {"state": 1},
                },
            ],
        }
        rtl = "module d(input clk, output reg y); always @(posedge clk) y <= 0; endmodule"
        packet = build_failure_packet(result, rtl, trace_window_cycles=1)
        self.assertEqual(packet["schema_version"], 2)
        self.assertEqual(packet["scenario_index"], 0)
        self.assertEqual(len(packet["mismatch_cluster"]), 2)
        self.assertEqual(
            packet["internal_state_delta"],
            [{"signal": "state", "before": 0, "at_failure": 1}],
        )

    def test_failure_packet_summarizes_formal_evidence_without_wavejson(self):
        counterexamples = [
            {
                "property_index": index + 1,
                "property_id": f"P{index + 1}",
                "scenario_id": "TEST",
                "requirement_ids": ["REQ"],
                "cycle": index,
                "phase": "formal",
                "signal": "y",
                "expected": 1,
                "actual": 0,
                "inputs": {"a": 1},
                "input_trace": [{"a": 0}] * 64,
                "wavejson": {"secret_wave_payload": "x" * 1024},
                "artifact": f"/tmp/cex-{index + 1}.json",
            }
            for index in range(40)
        ]
        result = {
            "passed": False,
            "source": "formal",
            "failure_class": "formal_counterexample",
            "first_failure_ordinal": 1,
            "mismatched": 40,
            "mismatches": [
                {
                    "check_id": "formal:P1",
                    "ordinal": 1,
                    "scenario_index": 0,
                    "scenario_id": "TEST",
                    "requirement_ids": ["REQ"],
                    "cycle": 0,
                    "phase": "formal",
                    "signal": "y",
                    "expected": 1,
                    "actual": 0,
                    "inputs": {"a": 1},
                }
            ],
            "actual_trace": [],
            "formal": {
                "status": "fail",
                "provider": "yosys_bounded_trace",
                "property_count": 40,
                "counterexamples": counterexamples,
                "artifact": "/tmp/formal-report.json",
            },
            "verilator_lint": {
                "status": "pass",
                "provider": "verilator_lint",
            },
        }
        packet = build_failure_packet(
            result,
            "module d(input a, output y); assign y = a; endmodule",
        )
        rendered = json.dumps(packet)
        self.assertNotIn("wavejson", rendered)
        self.assertNotIn("input_trace", rendered)
        self.assertNotIn("secret_wave_payload", rendered)
        self.assertEqual(packet["formal"]["counterexample_count"], 40)
        self.assertEqual(len(packet["formal"]["counterexamples"]), 32)
        self.assertTrue(packet["formal"]["counterexamples_truncated"])
        self.assertEqual(
            packet["formal"]["counterexamples"][0]["artifact"],
            "/tmp/cex-1.json",
        )
        self.assertEqual(packet["verilator_lint"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
