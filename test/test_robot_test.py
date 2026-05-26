from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def write_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_sdk(tmp_path: Path) -> tuple[Path, Path]:
    sdk = tmp_path / "sdk"
    script_path = sdk / "scripts" / "test" / "robot-test"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCRIPT_ROOT / "test" / "robot-test", script_path)
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    log_path = tmp_path / "runner-args.json"
    write_file(
        sdk / "scripts" / "test" / "run_tests.py",
        """
        #!/usr/bin/env python3
        from __future__ import annotations

        import json
        import os
        import sys
        from pathlib import Path


        Path(os.environ["ROBOT_TEST_ARG_LOG"]).write_text(
            json.dumps(sys.argv[1:]) + "\\n",
            encoding="utf-8",
        )
        """,
        executable=True,
    )
    return sdk, log_path


def run_robot_test(sdk: Path, log_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ROBOT_TEST_ARG_LOG"] = str(log_path)
    return subprocess.run(
        [str(sdk / "scripts" / "test" / "robot-test"), *args],
        cwd=sdk,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def read_args(log_path: Path) -> list[str]:
    return json.loads(log_path.read_text(encoding="utf-8"))


def load_run_tests_module():
    module_name = "srobotis_run_tests_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        SCRIPT_ROOT / "test" / "run_tests.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_robot_test_list_expands_to_runner_list(tmp_path: Path) -> None:
    sdk, log_path = make_sdk(tmp_path)

    result = run_robot_test(sdk, log_path, "list", "build")

    assert result.returncode == 0, result.stdout
    assert read_args(log_path) == ["list", "--module", "build"]


def test_robot_test_run_defaults_to_pr_output(tmp_path: Path) -> None:
    sdk, log_path = make_sdk(tmp_path)

    result = run_robot_test(sdk, log_path, "run", "components/foo")

    assert result.returncode == 0, result.stdout
    assert read_args(log_path) == [
        "run",
        "--module",
        "components/foo",
        "--scope",
        "pr",
        "--output",
        "output/test/pr/components__foo",
    ]


def test_robot_test_run_forwards_target_category_and_output(tmp_path: Path) -> None:
    sdk, log_path = make_sdk(tmp_path)

    result = run_robot_test(
        sdk,
        log_path,
        "run",
        "build",
        "--scope",
        "scheduled",
        "--target",
        "k3",
        "--category",
        "functional",
        "--output",
        "out",
    )

    assert result.returncode == 0, result.stdout
    assert read_args(log_path) == [
        "run",
        "--module",
        "build",
        "--scope",
        "scheduled",
        "--output",
        "out",
        "--target",
        "k3",
        "--category",
        "functional",
    ]


def test_robot_test_target_defaults_to_scheduled_output(tmp_path: Path) -> None:
    sdk, log_path = make_sdk(tmp_path)

    result = run_robot_test(sdk, log_path, "target", "k3-com260-minimal")

    assert result.returncode == 0, result.stdout
    assert read_args(log_path) == [
        "run-target",
        "--target",
        "k3-com260-minimal",
        "--scope",
        "scheduled",
        "--output",
        "output/test/k3-com260-minimal",
    ]


def test_run_tests_installs_missing_toolchain_dependencies(tmp_path: Path, monkeypatch) -> None:
    run_tests = load_run_tests_module()
    package_xml = tmp_path / "package.xml"
    installed = tmp_path / "installed.txt"
    tool_log = tmp_path / "tool.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed.write_text("present-pkg\n", encoding="utf-8")
    package_xml.write_text(
        """
        <?xml version="1.0"?>
        <package format="3">
          <name>srobotis_test</name>
          <version>0.1.0</version>
          <description>test</description>
          <maintainer email="dev@example.com">dev</maintainer>
          <license>Apache-2.0</license>
          <system_depend>present-pkg</system_depend>
          <system_depend>missing-one</system_depend>
          <system_depend>missing-two</system_depend>
        </package>
        """.strip()
        + "\n",
        encoding="utf-8",
    )
    write_file(
        bin_dir / "dpkg",
        f"""
        #!/bin/sh
        echo "dpkg $@" >> {shlex.quote(str(tool_log))}
        if [ "$1" = "-s" ] && grep -qx "$2" {shlex.quote(str(installed))}; then
          exit 0
        fi
        exit 1
        """,
        executable=True,
    )
    write_file(
        bin_dir / "apt-get",
        f"""
        #!/bin/sh
        echo "apt-get $@" >> {shlex.quote(str(tool_log))}
        if [ "$1" = "install" ]; then
          shift
          for arg in "$@"; do
            [ "$arg" = "-y" ] && continue
            grep -qx "$arg" {shlex.quote(str(installed))} || \\
              echo "$arg" >> {shlex.quote(str(installed))}
          done
        fi
        exit 0
        """,
        executable=True,
    )

    monkeypatch.setattr(run_tests, "test_toolchain_package_xml", lambda: package_xml)
    monkeypatch.setattr(run_tests.os, "geteuid", lambda: 0)
    env = {
        "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
        "SROBOTIS_TEST_PYENV_ROOT": str(tmp_path / "pyenv"),
    }

    with (tmp_path / "run.log").open("w", encoding="utf-8") as log_file:
        identity = run_tests.ensure_test_toolchain_dependencies(env, log_file)

    assert identity
    assert installed.read_text(encoding="utf-8").splitlines() == [
        "present-pkg",
        "missing-one",
        "missing-two",
    ]
    log = tool_log.read_text(encoding="utf-8")
    assert "apt-get update" in log
    assert "apt-get install -y missing-one missing-two" in log
