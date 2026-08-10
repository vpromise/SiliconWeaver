"""Local best-of-N RTL generation loop with explicit cross-verification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from .extraction import extract_python, extract_verilog


class GenerationBackend(Protocol):
    def generate(
        self,
        prompt: str,
        n: int = 1,
        temperature: float = 0.6,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
    ) -> list[str]: ...


Verifier = Callable[[str, str, str, int, int], dict[str, Any]]
_INVALID_RTL = "No complete Verilog module could be extracted."
_INVALID_PYTHON = "No valid Python model could be extracted."


@dataclass
class TurnLog:
    turn: int
    n: int
    best_pair: int
    best_match_rate: float
    all_match_rates: list[float]
    rtl_lengths: list[int]
    python_lengths: list[int]
    best_rtl_so_far: str = ""
    best_match_rate_so_far: float = -1.0


@dataclass
class InferenceResult:
    task_id: str
    verilog: str
    python: str
    matched: bool
    best_match_rate: float
    best_turn: int
    turns_used: int
    turn_logs: list[TurnLog] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "verilog": self.verilog,
            "python": self.python,
            "matched": self.matched,
            "best_match_rate": self.best_match_rate,
            "best_turn": self.best_turn,
            "turns_used": self.turns_used,
            "turn_logs": [asdict(turn) for turn in self.turn_logs],
        }


def _rtl_prompt(question: str, turn: int, previous: str, feedback: str) -> str:
    history = ""
    if turn and previous:
        bounded = previous[:6000]
        history = f"\nPrevious complete RTL:\n```systemverilog\n{bounded}\n```\n"
    diagnostic = f"\nVerification feedback:\n{feedback[:4000]}\n" if feedback else ""
    return f"""Implement the following hardware requirement as synthesizable SystemVerilog.
Return the complete design inside one systemverilog code fence. Do not change
the requested interface, use simulation-only constructs, or omit submodules.

Requirement:
{question.strip()}
{history}{diagnostic}
Iteration: {turn + 1}
"""


def _python_prompt(question: str, turn: int, previous: str, feedback: str) -> str:
    history = ""
    if turn and previous:
        history = f"\nPrevious Python model:\n```python\n{previous[:6000]}\n```\n"
    diagnostic = f"\nVerification feedback:\n{feedback[:4000]}\n" if feedback else ""
    return f"""Implement a deterministic Python reference model for this hardware requirement.
Define class TopModule with eval(self, inputs: dict) -> dict. One eval call is
one rising clock edge. Mask every state value and output to its declared width.
Return only the complete model in one python code fence.

Requirement:
{question.strip()}
{history}{diagnostic}
Iteration: {turn + 1}
"""


def _feedback(result: dict[str, Any], *, perspective: str) -> str:
    if result.get("meta_error"):
        return f"Verification could not run: {result['meta_error']}"
    own_error = result.get("sv_error" if perspective == "rtl" else "py_error")
    if own_error:
        return str(own_error)[:3000]
    peer_error = result.get("py_error" if perspective == "rtl" else "sv_error")
    if peer_error:
        return "The paired candidate failed; preserve correct behavior and produce a complete replacement."
    mismatches = result.get("mismatches")
    if not isinstance(mismatches, list) or not mismatches:
        return ""
    lines = [
        f"Observed {result.get('mismatched', '?')}/{result.get('total_checks', '?')} mismatches."
    ]
    for mismatch in mismatches[:5]:
        if isinstance(mismatch, dict):
            lines.append(str(mismatch)[:800])
    return "\n".join(lines)


def _normalized_outputs(values: list[str], count: int) -> list[str]:
    bounded = [value if isinstance(value, str) else "" for value in values[:count]]
    return bounded + [""] * (count - len(bounded))


def run_problem(
    *,
    task_id: str,
    question: str,
    ref_sv: str,
    v_backend: GenerationBackend,
    verifier: Verifier,
    p_backend: GenerationBackend | None = None,
    n: int = 10,
    max_turns: int = 5,
    num_verify_tests: int = 30,
    temperature: float = 0.6,
    seed: int = 42,
) -> InferenceResult:
    """Generate paired candidates and retain only strict verification improvements."""

    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if max_turns < 1:
        raise ValueError(f"max_turns must be >= 1, got {max_turns}")
    python_backend = p_backend or v_backend
    best_rtl = ""
    best_python = ""
    best_rate = -1.0
    best_turn = -1
    rtl_carry = ""
    python_carry = ""
    rtl_feedback = ""
    python_feedback = ""
    logs: list[TurnLog] = []
    matched = False

    for turn in range(max_turns):
        with ThreadPoolExecutor(max_workers=2) as pool:
            rtl_future = pool.submit(
                v_backend.generate,
                _rtl_prompt(question, turn, rtl_carry, rtl_feedback),
                n,
                temperature,
            )
            python_future = pool.submit(
                python_backend.generate,
                _python_prompt(question, turn, python_carry, python_feedback),
                n,
                temperature,
            )
            rtl_responses = _normalized_outputs(rtl_future.result(), n)
            python_responses = _normalized_outputs(python_future.result(), n)

        rtl_codes = [extract_verilog(value) or _INVALID_RTL for value in rtl_responses]
        python_codes = [
            extract_python(value) or _INVALID_PYTHON for value in python_responses
        ]
        results: list[dict[str, Any] | None] = [None] * n
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = {}
            for index in range(n):
                if rtl_codes[index] == _INVALID_RTL or python_codes[index] == _INVALID_PYTHON:
                    results[index] = {
                        "match_rate": 0.0,
                        "mismatches": [],
                        "meta_error": "invalid extracted code",
                    }
                    continue
                future = pool.submit(
                    verifier,
                    ref_sv,
                    rtl_codes[index],
                    python_codes[index],
                    num_verify_tests,
                    seed + turn,
                )
                futures[future] = index
            for future in as_completed(futures):
                results[futures[future]] = future.result()

        rates = [
            float(result.get("match_rate", -1.0)) if isinstance(result, dict) else -1.0
            for result in results
        ]
        valid_pairs = [
            rtl_codes[index] != _INVALID_RTL
            and python_codes[index] != _INVALID_PYTHON
            for index in range(n)
        ]
        selected = max(
            range(n),
            key=lambda index: (rates[index], valid_pairs[index]),
        )
        selected_result = results[selected] or {}
        selected_rate = rates[selected]
        selected_rtl = rtl_codes[selected]
        selected_python = python_codes[selected]
        if valid_pairs[selected] and selected_rate > best_rate:
            best_rate = selected_rate
            best_rtl = selected_rtl
            best_python = selected_python
            best_turn = turn
        if selected_rtl != _INVALID_RTL:
            rtl_carry = selected_rtl
        if selected_python != _INVALID_PYTHON:
            python_carry = selected_python
        rtl_feedback = _feedback(selected_result, perspective="rtl")
        python_feedback = _feedback(selected_result, perspective="python")
        logs.append(
            TurnLog(
                turn=turn,
                n=n,
                best_pair=selected,
                best_match_rate=selected_rate,
                all_match_rates=rates,
                rtl_lengths=[len(code) for code in rtl_codes],
                python_lengths=[len(code) for code in python_codes],
                best_rtl_so_far=best_rtl,
                best_match_rate_so_far=best_rate,
            )
        )
        if selected_rate >= 1.0:
            matched = True
            break

    return InferenceResult(
        task_id=task_id,
        verilog=best_rtl or rtl_carry,
        python=best_python or python_carry,
        matched=matched,
        best_match_rate=best_rate,
        best_turn=best_turn,
        turns_used=len(logs),
        turn_logs=logs,
    )
