from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
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
