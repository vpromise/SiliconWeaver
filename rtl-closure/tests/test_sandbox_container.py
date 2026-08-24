from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path

from rtl_closure.contract import content_hash
from rtl_closure.errors import ContainerInfrastructureError
from rtl_closure.logic_value import from_binary, from_integer
from rtl_closure.sandbox import (
    SANDBOX_IMAGE,
    _evaluate_architecture_netlists,
    _normalize_verifier_module_name,
    _observable_equivalence_metadata,
    _run_container,
    contract_cross_verify,
    normalize_top_module,
    run_architecture_audit,
    run_synthesis_equivalence,
    yosys_check,
)


def docker_ready() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


class SandboxContainerTests(unittest.TestCase):
    @staticmethod
    def _reset_preparation() -> dict:
        return {
            "schema_version": 1,
            "interfaces": {
                "gold": {
                    "inputs": {"clk": 1, "rst_n": 1},
                    "outputs": {"result": 4},
                    "observable_input_dependency": {"clk": True, "rst_n": True},
                },
                "gate": {
                    "inputs": {"clk": 1, "rst_n": 1},
                    "outputs": {"result": 4},
                    "observable_input_dependency": {"clk": True, "rst_n": True},
                },
            },
        }

    def test_cross_verify_retains_complete_replayable_counterexample(self):
        reference = """module demo(
  input logic clk,
  input logic a,
  output logic y
);
endmodule
"""
        stimuli_count = 8
        sv_trace = [{"y": 0} for _ in range(stimuli_count)]
        py_trace = [{"y": 1} for _ in range(stimuli_count)]
        with patch(
            "rtl_closure.sandbox.simulate_verilog",
            return_value=(sv_trace, None),
        ), patch(
            "rtl_closure.sandbox.simulate_python",
            return_value=(py_trace, None),
        ):
            result = contract_cross_verify(
                reference,
                "module demo(input logic clk, input logic a, output logic y); endmodule",
                "class TopModule: pass",
                clock_port="clk",
                reset=None,
                num_tests=stimuli_count,
                seed=17,
            )

        self.assertEqual(result["mismatched"], stimuli_count)
        self.assertEqual(len(result["mismatches"]), stimuli_count)
        self.assertEqual(len(result["stimuli"]), stimuli_count)
        self.assertEqual(
            result["verilog_trace"],
            [{"y": from_integer(0, 1)} for _ in range(stimuli_count)],
        )
        self.assertEqual(
            result["python_trace"],
            [{"y": from_integer(1, 1)} for _ in range(stimuli_count)],
        )

    def test_cross_verify_preserves_unknown_and_high_impedance_values(self):
        reference = "module demo(input logic a, output logic y); endmodule"
        with patch(
            "rtl_closure.sandbox.simulate_verilog",
            return_value=([{"y": from_binary("z", 1)}], None),
        ), patch(
            "rtl_closure.sandbox.simulate_python",
            return_value=([{"y": None}], None),
        ):
            result = contract_cross_verify(
                reference,
                reference,
                "class TopModule: pass",
                clock_port=None,
                reset=None,
                num_tests=1,
                seed=17,
            )

        self.assertEqual(result["transcript_schema_version"], 2)
        self.assertEqual(result["mismatched"], 1)
        self.assertEqual(result["verilog_trace"][0]["y"], from_binary("z", 1))
        self.assertEqual(result["python_trace"][0]["y"], from_integer(None, 1))

    def test_top_normalizers_ignore_comments_and_preserve_preceding_helpers(self):
        rtl = """// This module top_module is the requested design.
module helper(input logic a, output logic y);
  assign y = a;
endmodule : helper
module top_module(input logic clk, input logic a, output logic y);
  helper child(.a(a), .y(y));
endmodule : top_module
"""
        published = normalize_top_module(rtl, "demo")
        self.assertIn("// This module top_module", published)
        self.assertIn("module helper", published)
        self.assertIn("module demo", published)
        self.assertIn("endmodule : demo", published)
        verified = _normalize_verifier_module_name(
            published,
            source_name="demo",
        )
        self.assertIn("module helper", verified)
        self.assertIn("module TopModule", verified)
        self.assertNotIn("module demo(", verified)

    @unittest.skipUnless(docker_ready(), "LibreLane sandbox image is unavailable")
    def test_contract_cross_verify_accepts_comment_and_helper_before_contract_top(self):
        reference = """module demo(
  input logic clk,
  input logic a,
  output logic y
);
endmodule
"""
        rtl = """// This module implements the requested passthrough.
module helper(input logic a, output logic y);
  assign y = a;
endmodule
module demo(input logic clk, input logic a, output logic y);
  helper child(.a(a), .y(y));
endmodule
"""
        python = """class TopModule:
    def eval(self, inputs):
        return {"y": inputs.get("a", 0) & 1}
"""
        result = contract_cross_verify(
            reference,
            rtl,
            python,
            clock_port="clk",
            reset=None,
            num_tests=4,
            seed=7,
        )
        self.assertEqual(result["match_rate"], 1.0, result)

    @unittest.skipUnless(docker_ready(), "LibreLane sandbox image is unavailable")
    def test_yosys_policy_rejects_initializer_on_user_defined_type(self):
        rtl = """module demo(input logic clk, output logic q);
typedef logic [7:0] word_t;
word_t count = 0;
always_ff @(posedge clk) begin
  count <= count + 1'b1;
  q <= count[0];
end
endmodule"""
        with self.assertRaisesRegex(RuntimeError, "synthesized initialization"):
            yosys_check(rtl, "demo")

    def test_release_gate_container_is_removed_after_timeout(self):
        timeout = subprocess.TimeoutExpired(["docker", "run"], 1)
        with patch("rtl_closure.sandbox._run", side_effect=timeout) as run:
            with patch("rtl_closure.sandbox._force_remove_container") as remove:
                with self.assertRaises(ContainerInfrastructureError) as raised:
                    _run_container(
                        ["docker", "run", "--rm", "example"],
                        1,
                        purpose="test",
                    )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "run", "--name"])
        remove.assert_called_once_with(command[3])
        self.assertEqual(raised.exception.provider, "test")
        self.assertEqual(
            raised.exception.failure_code,
            "infrastructure/test_container_runtime",
        )
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(raised.exception.repairable_by_rtl)

    def test_docker_daemon_failure_is_typed_and_non_repairable(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=125,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
        with patch("rtl_closure.sandbox._run", return_value=completed), patch(
            "rtl_closure.sandbox._force_remove_container"
        ):
            with self.assertRaises(ContainerInfrastructureError) as raised:
                _run_container(
                    ["docker", "run", "--rm", "example"], 1, purpose="yosys"
                )

        self.assertFalse(raised.exception.repairable_by_rtl)
        self.assertEqual(raised.exception.failure_gate, "container_runtime")
        self.assertEqual(
            raised.exception.failure_code, "infrastructure/yosys_container_runtime"
        )

    def test_cross_verify_timeouts_are_typed_and_never_become_rtl_errors(self):
        reference = (
            "module demo(input logic clk, input logic a, output logic y); "
            "endmodule"
        )
        timeout = subprocess.TimeoutExpired(["docker", "run"], 1)
        with patch("rtl_closure.sandbox._run", side_effect=timeout), patch(
            "rtl_closure.sandbox._force_remove_container"
        ):
            with self.assertRaises(ContainerInfrastructureError) as sv_error:
                contract_cross_verify(
                    reference,
                    reference,
                    "class TopModule: pass",
                    clock_port="clk",
                    reset=None,
                    num_tests=1,
                )
        self.assertEqual(sv_error.exception.provider, "sv-compile")
        self.assertFalse(sv_error.exception.repairable_by_rtl)

        with patch(
            "rtl_closure.sandbox.simulate_verilog",
            return_value=([{"y": 0}], None),
        ), patch("rtl_closure.sandbox._run", side_effect=timeout), patch(
            "rtl_closure.sandbox._force_remove_container"
        ):
            with self.assertRaises(ContainerInfrastructureError) as py_error:
                contract_cross_verify(
                    reference,
                    reference,
                    "class TopModule: pass",
                    clock_port="clk",
                    reset=None,
                    num_tests=1,
                )
        self.assertEqual(py_error.exception.provider, "py-oracle")
        self.assertTrue(py_error.exception.retryable)
        self.assertFalse(py_error.exception.repairable_by_rtl)

    def test_architecture_and_equivalence_timeouts_remain_typed(self):
        rtl = "module demo(input logic a, output logic y); assign y = a; endmodule"
        timeout = subprocess.TimeoutExpired(["docker", "run"], 1)
        with patch("rtl_closure.sandbox._run", side_effect=timeout), patch(
            "rtl_closure.sandbox._force_remove_container"
        ):
            with self.assertRaises(ContainerInfrastructureError) as architecture:
                run_architecture_audit(
                    rtl,
                    "demo",
                    {"required_operators": {"$and": 1}},
                    timeout=1,
                )
        self.assertEqual(architecture.exception.provider, "architecture")
        self.assertFalse(architecture.exception.repairable_by_rtl)

        with patch("rtl_closure.sandbox._run", side_effect=timeout), patch(
            "rtl_closure.sandbox._force_remove_container"
        ):
            with self.assertRaises(ContainerInfrastructureError) as equivalence:
                run_synthesis_equivalence(rtl, "demo", timeout=1)
        self.assertEqual(equivalence.exception.provider, "synth-equiv-prepare")
        self.assertTrue(equivalence.exception.retryable)
        self.assertFalse(equivalence.exception.repairable_by_rtl)

    def test_synthesis_equivalence_returns_bound_evidence(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "Found 12 $equiv cells in equiv:\n"
                "Number of proven $equiv cells: 12\n"
            ),
            stderr="",
        )
        rtl = "module demo(input wire a, output wire y); assign y = a; endmodule"
        with patch(
            "rtl_closure.sandbox._prepare_observable_equivalence",
            return_value=(["state"], 12, ["y"], "preparation"),
        ), patch("rtl_closure.sandbox._run_container", return_value=completed) as run:
            report, log = run_synthesis_equivalence(rtl, "demo")
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["rtl_sha256"], content_hash(rtl))
        self.assertEqual(report["equivalence_cells"], 12)
        self.assertEqual(report["proven_cells"], 12)
        self.assertIn("proven", log)
        script = run.call_args.args[0][-1]
        self.assertIn(
            "synth -flatten -encfile /tmp/rtl-closure-fsm.enc -top demo",
            script,
        )
        self.assertIn(
            "equiv_make -encfile /tmp/rtl-closure-fsm.enc "
            "gold gate equiv",
            script,
        )
        self.assertEqual(report["comparison_semantics"]["observable_output_bits"], 12)
        self.assertEqual(report["comparison_semantics"]["output_ports"], ["y"])
        self.assertGreaterEqual(script.count("async2sync"), 2)
        self.assertIn("equiv_status -assert @observable_equiv", script)

    def test_synthesis_equivalence_binds_and_proves_reset_base(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "SAT proof finished - no model found: SUCCESS!\n"
                "Found 4 $equiv cells in equiv:\n"
                "Number of proven $equiv cells: 4\n"
            ),
            stderr="",
        )
        reset = {"port": "rst_n", "active_low": True, "asynchronous": True}
        with patch(
            "rtl_closure.sandbox._prepare_observable_equivalence",
            return_value=(
                ["state"],
                4,
                ["result"],
                "preparation",
                self._reset_preparation(),
            ),
        ), patch("rtl_closure.sandbox._run_container", return_value=completed) as run:
            report, _ = run_synthesis_equivalence(
                "module demo; endmodule",
                "demo",
                clock_port="clk",
                reset=reset,
            )
        self.assertEqual(
            report["scope"],
            "post_reset_observable_outputs_rtl_vs_generic_synthesized_netlist",
        )
        self.assertEqual(report["clock_port"], "clk")
        self.assertEqual(report["reset"], reset)
        self.assertEqual(report["reset_base_case"]["status"], "pass")
        script = run.call_args.args[0][-1]
        self.assertIn("equiv_miter -assert equiv_reset_base", script)
        self.assertIn("-set-at 1 rst_n 0", script)
        self.assertIn("-set-at 10 rst_n 1", script)
        self.assertIn("-prove-skip 2 -prove-asserts", script)

    def test_synthesis_equivalence_rejects_reset_schedule_without_miter_port(self):
        reset = {"port": "rst_n", "active_low": True, "asynchronous": False}
        preparation = self._reset_preparation()
        preparation["interfaces"]["gate"]["inputs"].pop("rst_n")
        with patch(
            "rtl_closure.sandbox._prepare_observable_equivalence",
            return_value=(["state"], 4, ["result"], "preparation", preparation),
        ), patch("rtl_closure.sandbox._run_container") as run:
            report, _ = run_synthesis_equivalence(
                "module demo; endmodule", "demo", clock_port="clk", reset=reset
            )

        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["reason_code"], "reset_or_clock_port_missing_from_miter")
        run.assert_not_called()

    def test_synthesis_equivalence_failed_proof_is_inconclusive_without_cex(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="unproven equivalence cell"
        )
        with patch(
            "rtl_closure.sandbox._prepare_observable_equivalence",
            return_value=([], 1, ["y"], "preparation"),
        ), patch("rtl_closure.sandbox._run_container", return_value=completed):
            report, _ = run_synthesis_equivalence("module demo; endmodule", "demo")
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(
            report["reason_code"],
            "equivalence_proof_failed_without_replayable_counterexample",
        )
        self.assertIn("replay_script", report["proof_evidence"])

    def test_synthesis_equivalence_captures_replayable_miter_counterexample(self):
        def run(command, _timeout, *, purpose):
            if purpose == "synth-equiv-cex":
                mount = command[command.index("-v") + 1]
                work = Path(mount.rsplit(":/work:", 1)[0])
                (work / "equivalence-cex.json").write_text(
                    json.dumps({"signal": "equiv_counterexample"}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=1,
                    stdout="SAT model found",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="unproven equivalence cell",
                stderr="",
            )

        with patch(
            "rtl_closure.sandbox._prepare_observable_equivalence",
            return_value=([], 1, ["y"], "preparation"),
        ), patch("rtl_closure.sandbox._run_container", side_effect=run):
            report, _ = run_synthesis_equivalence("module demo; endmodule", "demo")
        self.assertEqual(report["status"], "counterexample")
        self.assertEqual(report["verdict"], "counterexample")
        self.assertEqual(
            report["counterexample"]["replay_status"], "miter_observable_mismatch"
        )
        self.assertEqual(
            report["counterexample"]["artifact"], {"signal": "equiv_counterexample"}
        )

    def test_observable_equivalence_records_outputs_and_support_names(self):
        ports = {
            "clk": {"direction": "input", "bits": [1]},
            "y": {"direction": "output", "bits": [2, 3]},
        }
        gold = {
            "modules": {
                "gold": {
                    "ports": ports,
                    "netnames": {"clk": {}, "y": {}, "state": {}, "$tmp": {}},
                    "cells": {"u_state": {}, "$auto": {}},
                }
            }
        }
        gate = {
            "modules": {
                "gate": {
                    "ports": ports,
                    "netnames": {"clk": {}, "y": {}, "state": {}, "$other": {}},
                    "cells": {"u_state": {}, "$mapped": {}},
                }
            }
        }
        supporting_names, output_bits, output_ports = _observable_equivalence_metadata(
            gold, gate
        )
        self.assertEqual(supporting_names, ["state", "u_state"])
        self.assertEqual(output_bits, 2)
        self.assertEqual(output_ports, ["y"])

    def test_architecture_audit_checks_reachable_hierarchy_and_synth_stats(self):
        hierarchy = {
            "modules": {
                "demo": {
                    "cells": {
                        "u_control": {"type": "control"},
                        "multiply": {"type": "$mul"},
                    }
                },
                "control": {"cells": {}},
            }
        }
        synthesized = {
            "modules": {
                "demo": {
                    "cells": {
                        "ff0": {
                            "type": "$_DFF_P_",
                            "connections": {"Q": [10]},
                        },
                        "ff1": {
                            "type": "$_DFF_P_",
                            "connections": {"Q": [11]},
                        },
                        "and0": {"type": "$_AND_", "connections": {"Y": [12]}},
                    }
                }
            }
        }
        constraints = {
            "required_modules": ["control"],
            "forbidden_operators": ["divide"],
            "min_sequential_bits": 2,
            "max_synth_cells": 3,
        }
        report = _evaluate_architecture_netlists(
            hierarchy,
            synthesized,
            top="demo",
            constraints=constraints,
            rtl_sha256="rtl-hash",
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["observed"]["sequential_bits"], 2)
        self.assertEqual(report["observed"]["operator_cells"]["multiply"], 1)

        constraints["forbidden_operators"] = ["multiply"]
        report = _evaluate_architecture_netlists(
            hierarchy,
            synthesized,
            top="demo",
            constraints=constraints,
            rtl_sha256="rtl-hash",
        )
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["checks"][1]["id"], "forbidden_operator:multiply")


if __name__ == "__main__":
    unittest.main()
