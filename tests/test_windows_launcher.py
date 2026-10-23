"""Exercise the real BAT/PowerShell bootstrap, before Python is on PATH.

The child programs below only record launcher behavior. They do not stand in
for geometry or simulation results; real GUI construction has its own smoke
command. These regressions reproduce the Windows Store alias startup failure.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows BAT bootstrap")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher(tmp_path):
    folder = tmp_path / "中文 启动测试"
    (folder / "scripts").mkdir(parents=True)
    (folder / "config").mkdir()
    for name in ("启动MarkSimModeler.bat", "scripts/launch_windows.ps1",
                 "scripts/launch_modeler.py"):
        shutil.copy2(ROOT / name, folder / name)
    # Reproduce a PATH with an inert Windows Store alias, without invoking the
    # user's Store or modifying the real WindowsApps directory.
    alias_dir = tmp_path / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    (alias_dir / "python.exe").write_bytes(b"")
    env = os.environ.copy()
    env.update(PATH=str(alias_dir), MARKSIM_LAUNCHER_NO_PAUSE="1",
               PYTHONUTF8="1")
    return folder, env


def configure(folder, python=sys.executable):
    (folder / "config/local_runtime.json").write_text(
        json.dumps({"python": str(python)}, ensure_ascii=False), encoding="utf-8")


def child(folder, name="check_environment.py", exit_code=0):
    (folder / "scripts" / name).write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "record = {'executable': sys.executable, 'cwd': os.getcwd(), "
        "'script': Path(__file__).name}\n"
        "Path('child_record.json').write_text(json.dumps(record), encoding='utf-8')\n"
        "print('CHILD_STDOUT_MARKER', flush=True)\n"
        "print('CHILD_STDERR_MARKER', file=sys.stderr, flush=True)\n"
        f"raise SystemExit({exit_code})\n", encoding="utf-8")


def launch(launcher, argument="--check-only"):
    folder, env = launcher
    command = env.get("COMSPEC") or str(Path(env["SYSTEMROOT"]) / "System32/cmd.exe")
    # cmd.exe uses CMD quoting, not the Microsoft C argv escaping applied by
    # subprocess.list2cmdline (which turns embedded quotes into literal \" ).
    result = subprocess.run(
        f'"{command}" /d /c call "{folder / "启动MarkSimModeler.bat"}" {argument}',
        executable=command,
        cwd=folder.parent, env=env, capture_output=True, timeout=30)
    # Child and PowerShell deliberately emit UTF-8. CMD's ASCII error footer
    # remains readable without relying on the active Windows console codepage.
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    logs = list((folder / "logs").glob("startup_*.log"))
    assert len(logs) == 1, output
    log = logs[0].read_text(encoding="utf-8-sig")
    return result.returncode, output, log


def test_configured_python_bootstraps_with_store_alias_only_and_chinese_spaces(launcher):
    folder, _ = launcher
    configure(folder)
    # Explicit runtime wins over even an existing, unusable project .venv.
    local = folder / ".venv/Scripts/python.exe"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"")
    child(folder)
    code, output, log = launch(launcher)
    assert code == 0, output
    record = json.loads((folder / "child_record.json").read_text(encoding="utf-8"))
    assert Path(record["executable"]).samefile(sys.executable)
    assert Path(record["cwd"]) == folder
    assert record["script"] == "check_environment.py"
    assert "CHILD_STDOUT_MARKER" in log
    assert "CHILD_STDERR_MARKER" in log
    assert "Launcher exit code: 0" in log
    assert "Press any key" not in output


def test_smoke_test_argument_reaches_selected_interpreter(launcher):
    folder, _ = launcher
    configure(folder)
    child(folder, "smoke_gui.py")
    code, output, _ = launch(launcher, "--smoke-test")
    assert code == 0, output
    record = json.loads((folder / "child_record.json").read_text(encoding="utf-8"))
    assert record["script"] == "smoke_gui.py"


def test_configured_environment_variable_is_consistent_across_both_launchers(launcher):
    folder, env = launcher
    env["MARKSIM_TEST_PYTHON"] = sys.executable
    configure(folder, "%MARKSIM_TEST_PYTHON%")
    child(folder)
    code, output, log = launch(launcher)
    assert code == 0, output
    record = json.loads((folder / "child_record.json").read_text(encoding="utf-8"))
    assert Path(record["executable"]).samefile(sys.executable)
    assert "Launcher exit code: 0" in log


def test_no_config_and_only_store_alias_gives_persistent_actionable_error(launcher):
    folder, _ = launcher
    code, output, log = launch(launcher)
    assert code != 0, output
    assert "No usable Python found" in log
    assert "config/local_runtime.json" in log
    assert "Windows Store alias" in log
    assert not (folder / "child_record.json").exists()
    assert not (folder / ".venv").exists()


@pytest.mark.parametrize("contents", ["{broken json", '{"python":"missing/python.exe"}', '{}'])
def test_invalid_explicit_config_fails_without_silent_fallback(launcher, contents):
    folder, _ = launcher
    (folder / "config/local_runtime.json").write_text(contents, encoding="utf-8")
    child(folder)
    code, output, log = launch(launcher)
    assert code != 0, output
    assert "Startup error:" in log
    assert not (folder / "child_record.json").exists()


def test_child_error_exit_and_stderr_survive_bat_powershell_python_chain(launcher):
    folder, _ = launcher
    configure(folder)
    child(folder, exit_code=17)
    code, output, log = launch(launcher)
    assert code == 17, output
    assert "CHILD_STDERR_MARKER" in log
    assert "Launcher exit code: 17" in log
    assert "Startup failed" in log
