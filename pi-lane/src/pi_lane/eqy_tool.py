#!/usr/bin/env python3
"""Restricted make/awk launcher for EQY-generated jobs."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Rule:
    dependencies: list[str]
    recipes: list[str] = field(default_factory=list)


def logical_lines(text: str) -> list[str]:
    result: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = pending + raw
        if line.endswith("\\"):
            pending = line[:-1] + "\n"
            continue
        result.append(line)
        pending = ""
    if pending:
        result.append(pending)
    return result


def parse_makefile(path: Path) -> tuple[str, dict[str, Rule], set[str]]:
    default = "all"
    rules: dict[str, Rule] = {}
    phony: set[str] = set()
    active: Rule | None = None
    for line in logical_lines(path.read_text(encoding="utf-8")):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("\t"):
            if active is None:
                raise ValueError("recipe without a rule")
            active.recipes.append(line[1:])
            continue
        active = None
        match = re.fullmatch(r"\.DEFAULT_GOAL\s*:?=\s*(\S+)", line)
        if match:
            default = match.group(1)
            continue
        if ":=" in line or ("=" in line and ":" not in line.split("=", 1)[0]):
            continue
        if ":" not in line:
            raise ValueError(f"unsupported makefile line: {line}")
        target_text, dependency_text = line.split(":", 1)
        targets = target_text.split()
        dependencies = dependency_text.split()
        if targets == [".PHONY"]:
            phony.update(dependencies)
            continue
        if len(targets) != 1:
            raise ValueError("multiple-target rules are unsupported")
        active = Rule(dependencies)
        rules[targets[0]] = active
    return default, rules, phony


def expand_recipe(recipe: str, *, target: str, dependencies: list[str]) -> str:
    command = recipe.lstrip("@")
    command = command.replace("$(MAKE)", "make")
    command = command.replace("$@", target).replace("$^", " ".join(dependencies))
    return command.replace("$$", "$")


def run_target(
    target: str,
    rules: dict[str, Rule],
    phony: set[str],
    completed: set[str],
    active: set[str],
) -> None:
    if target in completed:
        return
    if target in active:
        raise RuntimeError(f"dependency cycle at {target}")
    rule = rules.get(target)
    if rule is None:
        if Path(target).exists():
            completed.add(target)
            return
        raise RuntimeError(f"no rule to make target {target}")
    active.add(target)
    for dependency in rule.dependencies:
        run_target(dependency, rules, phony, completed, active)
    should_run = target in phony or not Path(target).exists()
    if not should_run and rule.dependencies:
        target_mtime = Path(target).stat().st_mtime_ns
        should_run = any(
            Path(dependency).exists()
            and Path(dependency).stat().st_mtime_ns > target_mtime
            for dependency in rule.dependencies
        )
    if should_run:
        for recipe in rule.recipes:
            command = expand_recipe(
                recipe, target=target, dependencies=rule.dependencies
            )
            result = subprocess.run(["bash", "-c", command], check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"recipe for {target} exited with code {result.returncode}"
                )
    active.remove(target)
    completed.add(target)


def awk_main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "{print $1}":
        raise ValueError("unsupported awk invocation")
    with Path(argv[1]).open(encoding="utf-8") as source:
        for line in source:
            fields = line.split()
            if fields:
                print(fields[0])
    return 0


def main(argv: list[str]) -> int:
    if Path(sys.argv[0]).name == "awk":
        return awk_main(argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-C", dest="directory")
    parser.add_argument("-f", dest="makefile", default="Makefile")
    parser.add_argument("targets", nargs="*")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        raise ValueError(f"unsupported make arguments: {' '.join(unknown)}")
    if args.directory:
        os.chdir(args.directory)
    default, rules, phony = parse_makefile(Path(args.makefile))
    targets = args.targets or [default]
    completed: set[str] = set()
    for target in targets:
        run_target(target, rules, phony, completed, set())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"eqy-tool: {exc}", file=sys.stderr)
        raise SystemExit(2)
