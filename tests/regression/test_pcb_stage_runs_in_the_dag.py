import getpass
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hammer.shell import sledgehammer_cli
from hammer.vlsi import HammerDriver, HammerDriverOptions
from hammer.vlsi.hammer_build_systems import build_airflow_dag


class _FailException(Exception):
    pass


class _SkipException(Exception):
    pass


def _airflow_stubs(monkeypatch, context: dict) -> None:
    def module(name: str, **attrs) -> None:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    module("pendulum", datetime=lambda *a, **k: None)
    module("airflow")
    module("airflow.decorators", task=lambda fn=None, **kw: fn if fn is not None else (lambda f: f),
           dag=lambda *a, **k: (lambda f: (lambda: None)))
    module("airflow.models", Param=lambda *a, **k: None)
    module("airflow.utils")
    module("airflow.utils.task_group", TaskGroup=object)
    module("airflow.utils.trigger_rule",
           TriggerRule=types.SimpleNamespace(ALL_DONE="all_done", NONE_FAILED="none_failed"))
    module("airflow.exceptions", AirflowFailException=_FailException, AirflowSkipException=_SkipException)
    module("airflow.sdk", get_current_context=lambda: context)


BUMPS = {"x": 2, "y": 2, "pitch": 100.0, "cell": "bump",
         "assignments": [{"name": "VDD", "x": 1, "y": 1}, {"name": "VSS", "x": 2, "y": 1},
                         {"name": "clock", "x": 1, "y": 2}, {"name": "reset", "x": 2, "y": 2}]}


@pytest.fixture
def dag(tmp_path, monkeypatch):
    """A generated DAG with a pcb-ready config, exec'd with Airflow stubbed; returns (text, call(conf))."""
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    monkeypatch.setattr(sys, "argv", [os.path.join(os.path.dirname(sys.executable), "hammer-vlsi")])
    config = tmp_path / "pcb.json"
    config.write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.pcb_tool": "hammer.pcb.generic",
        "vlsi.inputs.hierarchical.config_source": "none",
        "synthesis.inputs.top_module": "dummy",
        "pcb.inputs.top_module": "dummy",
        "vlsi.inputs.bumps_mode": "manual",
        "vlsi.inputs.bumps": BUMPS,
        "technology.pcb.bump_pad_opening_diameter": 60,
        "technology.pcb.bump_pad_metal_diameter": 75,
    }))
    obj = tmp_path / "obj"
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(config)],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(obj)))
    build_airflow_dag(driver, lambda e: None)
    text = (obj / "hammer_dag.py").read_text()

    import hammer.shell.hammer_vlsi as hammer_vlsi
    monkeypatch.setattr(hammer_vlsi, "_resolve_workspace_obj_dir", lambda *a, **k: None)
    cmds = []
    context: dict = {}
    _airflow_stubs(monkeypatch, context)
    ns: dict = {}
    exec(compile(text, "hammer_dag.py", "exec"), ns)
    ns["subprocess"] = types.SimpleNamespace(
        run=lambda cmd, **kw: cmds.append(cmd) or types.SimpleNamespace(returncode=0), DEVNULL=-3)

    def call(conf: dict):
        context.clear()
        context.update(dag_run=types.SimpleNamespace(conf=conf, dag_id="d", run_id="r"),
                       ti=types.SimpleNamespace(task_id="pcb"),
                       task=types.SimpleNamespace(task_id="pcb", downstream_list=[]), params={})
        cmds.clear()
        ns["pcb"](**context)
        return cmds[0] if cmds else None

    call.obj = obj
    return text, call


def test_the_dag_has_a_pcb_stage_beside_the_first_stages(dag) -> None:
    text, _ = dag
    assert "'pcb': Param(default=False, type='boolean', title='PCB Deliverables')" in text
    assert "start_node >> pcb_node >> [summary_node, exit_node]" in text


def test_the_pcb_task_builds_the_board_deliverables(dag, tmp_path) -> None:
    """The command the task runs is make pcb's: the project configs, the obj_dir, and the pcb action."""
    _, call = dag
    cmd = call({"pcb": True})
    assert cmd[-3:] == ["--obj_dir", str(call.obj), "pcb"]
    assert cmd[cmd.index("-p") + 1] == str(tmp_path / "pcb.json")
    # run it against this checkout's hammer, whatever checkout the venv's editable install points at
    repo = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
               PYTHONPATH=repo + os.pathsep + os.environ.get("PYTHONPATH", ""))
    res = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    for name in ("dummy-pads.d", "dummy-pads.csv", "dummy-symbol.csv", "dummy-pindata.txt"):
        assert (call.obj / "pcb-rundir" / name).is_file(), name


def test_the_pcb_task_skips_unless_asked_and_forces_on_redo(dag) -> None:
    _, call = dag
    with pytest.raises(_SkipException):
        call({"syn": True})
    assert "--force" in call({"pcb": True, "redo": True})


@pytest.fixture
def trigger(tmp_path, monkeypatch):
    dags = tmp_path / "dags_cli"
    dags.mkdir()
    dag_file = dags / f"sledgehammer_Top_{getpass.getuser()}.py"
    calls = []
    monkeypatch.setattr(sledgehammer_cli, "_airflow", lambda *a, capture=True: calls.append(a) or (0, "", ""))
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))

    def run(dag_text: str, *argv: str) -> dict:
        dag_file.write_text(dag_text)
        calls.clear()
        assert sledgehammer_cli._cmd_run(list(argv) + ["--obj_dir", str(tmp_path / "Top"), "--no-wait"]) == 0
        sent = next(c for c in calls if c[:2] == ("dags", "trigger"))
        return json.loads(sent[sent.index("-c") + 1])
    return run


def test_sledgehammer_runs_pcb_like_make_pcb(trigger) -> None:
    assert sledgehammer_cli._parse_target("pcb") == ("pcb", None, False)
    assert sledgehammer_cli._parse_target("redo-pcb") == ("pcb", None, True)
    assert trigger("'forceall': Param(...)\n'pcb': Param(...)", "pcb") == {"pcb": True}
    assert sledgehammer_cli._shown("pcb") and not sledgehammer_cli._shown("start")


def test_a_dag_from_before_the_pcb_stage_says_to_regenerate(trigger) -> None:
    with pytest.raises(SystemExit) as cm:
        trigger("'forceall': Param(...)", "pcb")
    assert "predates the pcb stage" in str(cm.value.code)
