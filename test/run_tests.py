#!/usr/bin/env python3
#
# Copyright (C) 2026 SpacemiT (Hangzhou) Technology Co. Ltd.
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - depends on system image.
    print("scripts/test/run_tests.py requires PyYAML. Install python3-yaml.", file=sys.stderr)
    raise SystemExit(2) from exc


VALID_CATEGORIES = {"functional", "performance", "stability"}
VALID_SCOPES = {"pr", "scheduled", "release", "manual"}
PYENV_GIT_URL = "https://github.com/pyenv/pyenv.git"
PYENV_VERSION_RE = re.compile(r"^(?:python)?(?P<version>\d+\.\d+\.\d+)$")


@dataclass
class TestCase:
    module: str
    name: str
    description: str
    category: str
    scopes: list[str]
    timeout_s: int
    workdir: str
    command: str
    requires: dict[str, Any]
    python_env: dict[str, Any] | None
    allow_failure: bool
    artifacts: list[str]


def repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def module_safe_name(module: str) -> str:
    return module.strip("/").replace("/", "__").replace(" ", "_")


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def test_yaml_path(root: pathlib.Path, module: str) -> pathlib.Path | None:
    path = root / module / "test.yaml"
    return path if path.exists() else None


def as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError(f"expected string or list, got {type(value).__name__}")


def load_test_cases(root: pathlib.Path, module: str) -> list[TestCase]:
    config_path = test_yaml_path(root, module)
    if config_path is None:
        return []

    data = load_yaml(config_path)
    declared_module = str(data.get("module") or module)
    raw_tests = data.get("tests") or []
    if not isinstance(raw_tests, list):
        raise ValueError(f"{config_path}: tests must be a list")

    cases: list[TestCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tests, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path}: tests[{index}] must be a mapping")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"{config_path}: tests[{index}] missing name")
        if name in seen:
            raise ValueError(f"{config_path}: duplicate test name: {name}")
        seen.add(name)

        description = str(raw.get("description") or "").strip()
        if not description:
            raise ValueError(f"{config_path}: {name}: description must not be empty")

        category = str(raw.get("category") or "").strip()
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"{config_path}: {name}: invalid category {category!r}; "
                f"expected one of {sorted(VALID_CATEGORIES)}"
            )

        scopes = as_str_list(raw.get("scopes"))
        if not scopes:
            raise ValueError(f"{config_path}: {name}: scopes must not be empty")
        invalid_scopes = [scope for scope in scopes if scope not in VALID_SCOPES]
        if invalid_scopes:
            raise ValueError(
                f"{config_path}: {name}: invalid scopes {invalid_scopes}; "
                f"expected one of {sorted(VALID_SCOPES)}"
            )

        command = str(raw.get("command") or "").strip()
        if not command:
            raise ValueError(f"{config_path}: {name}: command must not be empty")

        requires = raw.get("requires") or {}
        if not isinstance(requires, dict):
            raise ValueError(f"{config_path}: {name}: requires must be a mapping")

        python_env = raw.get("python_env")
        if python_env is not None and not isinstance(python_env, dict):
            raise ValueError(f"{config_path}: {name}: python_env must be a mapping")
        cases.append(
            TestCase(
                module=declared_module,
                name=name,
                description=description,
                category=category,
                scopes=scopes,
                timeout_s=int(raw.get("timeout_s") or 300),
                workdir=str(raw.get("workdir") or "module"),
                command=command,
                requires=requires,
                python_env=python_env,
                allow_failure=bool(raw.get("allow_failure") or False),
                artifacts=as_str_list(raw.get("artifacts")),
            )
        )
    return cases


def resolve_workdir(root: pathlib.Path, case: TestCase) -> pathlib.Path:
    if case.workdir == "sdk":
        return root
    if case.workdir == "module":
        return root / case.module
    workdir = pathlib.Path(case.workdir)
    if workdir.is_absolute():
        return workdir
    return root / workdir


def base_env(root: pathlib.Path, case: TestCase, artifact_dir: pathlib.Path, target: str) -> dict[str, str]:
    env = os.environ.copy()
    output_root = root / "output"
    staging = output_root / "staging"
    rootfs = output_root / "rootfs"
    env.update(
        {
            "SROBOTIS_ROOT": str(root),
            "SROBOTIS_OUTPUT_ROOT": str(output_root),
            "SROBOTIS_OUTPUT_STAGING": str(staging),
            "SROBOTIS_OUTPUT_ROOTFS": str(rootfs),
            "SROBOTIS_TEST_MODULE": case.module,
            "SROBOTIS_TEST_NAME": case.name,
            "SROBOTIS_TEST_CATEGORY": case.category,
            "SROBOTIS_TEST_ARTIFACT_DIR": str(artifact_dir),
            "CI_BUILD_TARGET": target or env.get("CI_BUILD_TARGET", ""),
        }
    )

    path_parts = [str(staging / "bin")]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = os.pathsep.join(path_parts)

    ld_parts = [str(staging / "lib")]
    if env.get("LD_LIBRARY_PATH"):
        ld_parts.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = os.pathsep.join(ld_parts)

    python_parts = []
    if (staging / "python").exists():
        python_parts.append(str(staging / "python"))
    if env.get("PYTHONPATH"):
        python_parts.append(env["PYTHONPATH"])
    if python_parts:
        env["PYTHONPATH"] = os.pathsep.join(python_parts)
    return env


def check_requires(root: pathlib.Path, requires: dict[str, Any], env: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for name in as_str_list(requires.get("commands")):
        if shutil.which(name, path=env.get("PATH")) is None:
            missing.append(f"command not found: {name}")
    for name in as_str_list(requires.get("env")):
        if not env.get(name):
            missing.append(f"environment variable not set: {name}")
    for raw_path in as_str_list(requires.get("files")):
        path = pathlib.Path(raw_path)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            missing.append(f"file not found: {raw_path}")
    return missing


def python_env_hash(root: pathlib.Path, case: TestCase, python_identity: str = "") -> str:
    h = hashlib.sha256()
    h.update(case.module.encode())
    h.update(json.dumps(case.python_env or {}, sort_keys=True).encode())
    h.update(python_identity.encode())
    for rel in ("pyproject.toml", "requirements.txt", "tests/requirements.txt"):
        path = root / case.module / rel
        if path.exists():
            h.update(rel.encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:16]


def python_env_spec(case: TestCase) -> str:
    if not case.python_env:
        return "python3"
    return str(case.python_env.get("python") or "python3").strip() or "python3"


def pyenv_root(env: dict[str, str]) -> pathlib.Path:
    return pathlib.Path(
        env.get("SROBOTIS_TEST_PYENV_ROOT")
        or os.environ.get("SROBOTIS_TEST_PYENV_ROOT")
        or "~/.pyenv"
    ).expanduser()


def pyenv_env(env: dict[str, str], root_path: pathlib.Path) -> dict[str, str]:
    new_env = dict(env)
    new_env["PYENV_ROOT"] = str(root_path)
    new_env["PATH"] = os.pathsep.join([str(root_path / "bin"), new_env.get("PATH", "")])
    return new_env


def normalize_pyenv_version(spec: str) -> str | None:
    match = PYENV_VERSION_RE.fullmatch(spec)
    return match.group("version") if match else None


def run_logged(argv: list[str], *, cwd: pathlib.Path, env: dict[str, str], log_file) -> None:
    print(f"[run-tests] $ {' '.join(shlex.quote(item) for item in argv)}", file=log_file)
    subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        check=True,
    )


def ensure_pyenv(env: dict[str, str], log_file) -> pathlib.Path:
    root_path = pyenv_root(env)
    pyenv_bin = root_path / "bin" / "pyenv"
    if pyenv_bin.exists():
        return pyenv_bin

    root_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root_path.parent / ".srobotis-pyenv.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if pyenv_bin.exists():
            return pyenv_bin
        if root_path.exists() and any(root_path.iterdir()):
            raise RuntimeError(f"pyenv root exists but pyenv is incomplete: {root_path}")
        git = shutil.which("git", path=env.get("PATH"))
        if not git:
            raise RuntimeError("git is required to install pyenv")
        url = env.get("SROBOTIS_TEST_PYENV_GIT_URL") or os.environ.get("SROBOTIS_TEST_PYENV_GIT_URL") or PYENV_GIT_URL
        run_logged(
            [git, "clone", "--depth", "1", url, str(root_path)],
            cwd=root_path.parent,
            env=env,
            log_file=log_file,
        )
        if not pyenv_bin.exists():
            raise RuntimeError(f"pyenv install did not create {pyenv_bin}")
        return pyenv_bin


def ensure_pyenv_python(version: str, env: dict[str, str], log_file) -> pathlib.Path:
    root_path = pyenv_root(env)
    pyenv_bin = ensure_pyenv(env, log_file)
    python_path = root_path / "versions" / version / "bin" / "python"
    if python_path.exists():
        return python_path

    lock_path = root_path.parent / f".srobotis-pyenv-{version}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if python_path.exists():
            return python_path
        run_logged(
            [str(pyenv_bin), "install", "-s", version],
            cwd=root_path,
            env=pyenv_env(env, root_path),
            log_file=log_file,
        )
        if not python_path.exists():
            raise RuntimeError(f"pyenv did not create Python {version}: {python_path}")
        return python_path


def resolve_python_interpreter(spec: str, env: dict[str, str], log_file) -> pathlib.Path:
    path_spec = pathlib.Path(spec).expanduser()
    if path_spec.is_absolute() or "/" in spec:
        if path_spec.exists():
            return path_spec
        raise RuntimeError(f"python interpreter not found: {spec}")

    py_path = shutil.which(spec, path=env.get("PATH"))
    if py_path:
        return pathlib.Path(py_path)

    version = normalize_pyenv_version(spec)
    if version is None:
        raise RuntimeError(
            f"python interpreter not found: {spec}; "
            "for pyenv auto-install use a full patch version such as 3.12.3"
        )
    return ensure_pyenv_python(version, env, log_file)


def python_version(python_path: pathlib.Path, env: dict[str, str]) -> str:
    proc = subprocess.run(
        [str(python_path), "--version"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return proc.stdout.strip()


def ready_marker_matches(marker: pathlib.Path, expected: dict[str, Any], venv_python: pathlib.Path) -> bool:
    if not marker.exists() or not venv_python.exists():
        return False
    try:
        actual = json.loads(marker.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return actual == expected


def prepare_python_env(root: pathlib.Path, case: TestCase, env: dict[str, str], log_file) -> dict[str, str]:
    if not case.python_env:
        return env

    py = python_env_spec(case)
    py_path = resolve_python_interpreter(py, env, log_file)
    py_version = python_version(py_path, env)
    python_identity = f"{py}|{py_path}|{py_version}"

    venv_dir = root / "output" / "test" / "venvs" / module_safe_name(case.module) / python_env_hash(root, case, python_identity)
    marker = venv_dir / ".srobotis-ready"
    venv_python = venv_dir / "bin" / "python"
    expected_marker = {
        "python": py,
        "resolved_python": str(py_path),
        "python_version": py_version,
        "python_env": case.python_env or {},
        "install": as_str_list((case.python_env or {}).get("install")),
    }
    if not ready_marker_matches(marker, expected_marker, venv_python):
        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        print(f"[run-tests] create python env: {venv_dir}", file=log_file)
        print(f"[run-tests] python: {py_path} ({py_version})", file=log_file)
        run_logged(
            [str(py_path), "-m", "venv", str(venv_dir)],
            cwd=root / case.module,
            env=env,
            log_file=log_file,
        )

        run_logged(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            cwd=root / case.module,
            env=env,
            log_file=log_file,
        )
        for install in as_str_list(case.python_env.get("install")):
            cmd = [str(venv_python), "-m", "pip", "install", *shlex.split(install)]
            run_logged(
                cmd,
                cwd=root / case.module,
                env=env,
                log_file=log_file,
            )
        marker.write_text(
            json.dumps(expected_marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    new_env = dict(env)
    new_env["VIRTUAL_ENV"] = str(venv_dir)
    new_env["PATH"] = os.pathsep.join([str(venv_dir / "bin"), new_env.get("PATH", "")])
    return new_env


def run_case(root: pathlib.Path, case: TestCase, scope: str, output: pathlib.Path, target: str) -> dict[str, Any]:
    start = time.monotonic()
    artifact_dir = output / "modules" / module_safe_name(case.module)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / f"{case.name}.log"
    env = base_env(root, case, artifact_dir, target)
    workdir = resolve_workdir(root, case)

    result: dict[str, Any] = {
        "module": case.module,
        "name": case.name,
        "description": case.description,
        "category": case.category,
        "scope": scope,
        "status": "PASS",
        "duration_s": 0.0,
        "log": str(log_path.relative_to(output)),
    }

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        print(f"[run-tests] module={case.module} test={case.name} scope={scope}", file=log_file)
        print(f"[run-tests] description={case.description}", file=log_file)
        print(f"[run-tests] workdir={workdir}", file=log_file)
        missing = check_requires(root, case.requires, env)
        if missing:
            result["status"] = "FAIL"
            result["error"] = "; ".join(missing)
            print(f"[run-tests] missing requirements: {result['error']}", file=log_file)
        else:
            try:
                env = prepare_python_env(root, case, env, log_file)
                print("[run-tests] command:", file=log_file)
                print(case.command, file=log_file)
                proc = subprocess.run(
                    ["bash", "-c", case.command],
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    timeout=case.timeout_s,
                    check=False,
                )
                if proc.returncode != 0:
                    result["status"] = "FAIL"
                    result["returncode"] = proc.returncode
            except subprocess.TimeoutExpired:
                result["status"] = "FAIL"
                result["error"] = f"timeout after {case.timeout_s}s"
                print(f"[run-tests] timeout after {case.timeout_s}s", file=log_file)
            except Exception as exc:  # noqa: BLE001 - test runner should report the error.
                result["status"] = "FAIL"
                result["error"] = str(exc)
                print(f"[run-tests] error: {exc}", file=log_file)

        if result["status"] == "FAIL" and case.allow_failure:
            result["status"] = "SKIP"
            result["allow_failure"] = True

    result["duration_s"] = round(time.monotonic() - start, 3)
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(results),
        "passed": sum(1 for item in results if item["status"] == "PASS"),
        "failed": sum(1 for item in results if item["status"] == "FAIL"),
        "skipped": sum(1 for item in results if item["status"] == "SKIP"),
    }


def write_results(output: pathlib.Path, results: list[dict[str, Any]]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    payload = {"summary": summarize(results), "tests": results}
    (output / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def filter_cases(cases: list[TestCase], scope: str, category: str | None) -> list[TestCase]:
    selected = [case for case in cases if scope in case.scopes]
    if category:
        selected = [case for case in selected if case.category == category]
    return selected


def cmd_list(args: argparse.Namespace) -> int:
    root = repo_root()
    cases = load_test_cases(root, args.module)
    if not cases:
        print(f"no tests declared for module: {args.module}")
        return 0
    for case in cases:
        print(f"{case.name}\t{case.category}\t{','.join(case.scopes)}\t{case.description}")
    return 0


def run_module(
    root: pathlib.Path,
    module: str,
    scope: str,
    category: str | None,
    output: pathlib.Path,
    target: str,
) -> list[dict[str, Any]]:
    cases = filter_cases(load_test_cases(root, module), scope, category)
    results: list[dict[str, Any]] = []
    if not cases:
        return results
    for case in cases:
        print(f"[run-tests] RUN {case.module}:{case.name}")
        result = run_case(root, case, scope, output, target)
        print(f"[run-tests] {result['status']} {case.module}:{case.name} ({result['duration_s']}s)")
        results.append(result)
    return results


def cmd_run(args: argparse.Namespace) -> int:
    root = repo_root()
    output = pathlib.Path(args.output) if args.output else root / "output" / "test" / args.scope
    if not output.is_absolute():
        output = root / output
    results = run_module(root, args.module, args.scope, args.category, output, args.target or "")
    write_results(output, results)
    summary = summarize(results)
    print(f"[run-tests] summary: {summary}")
    return 1 if summary["failed"] else 0


def target_packages(root: pathlib.Path, target: str) -> list[str]:
    target_name = target[:-5] if target.endswith(".json") else target
    path = root / "target" / f"{target_name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = data.get("enabled_packages") or []
    if not isinstance(packages, list):
        raise ValueError(f"{path}: enabled_packages must be a list")
    return [str(pkg) for pkg in packages]


def cmd_run_target(args: argparse.Namespace) -> int:
    root = repo_root()
    output = pathlib.Path(args.output) if args.output else root / "output" / "test" / args.target
    if not output.is_absolute():
        output = root / output
    results: list[dict[str, Any]] = []
    for module in target_packages(root, args.target):
        results.extend(run_module(root, module, args.scope, args.category, output, args.target))
    write_results(output, results)
    summary = summarize(results)
    print(f"[run-tests] summary: {summary}")
    return 1 if summary["failed"] else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run_tests.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_parser = sub.add_parser("list", help="List tests declared by a module")
    list_parser.add_argument("--module", required=True)
    list_parser.set_defaults(func=cmd_list)

    run_parser = sub.add_parser("run", help="Run tests declared by a module")
    run_parser.add_argument("--module", required=True)
    run_parser.add_argument("--scope", default="pr", choices=sorted(VALID_SCOPES))
    run_parser.add_argument("--category", choices=sorted(VALID_CATEGORIES))
    run_parser.add_argument("--target", default="")
    run_parser.add_argument("--output", default="")
    run_parser.set_defaults(func=cmd_run)

    target_parser = sub.add_parser("run-target", help="Run tests for packages enabled by a target")
    target_parser.add_argument("--target", required=True)
    target_parser.add_argument("--scope", default="scheduled", choices=sorted(VALID_SCOPES))
    target_parser.add_argument("--category", choices=sorted(VALID_CATEGORIES))
    target_parser.add_argument("--output", default="")
    target_parser.set_defaults(func=cmd_run_target)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
