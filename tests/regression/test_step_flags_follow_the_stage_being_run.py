import getpass
import json

import pytest

from hammer.shell import sledgehammer_cli


@pytest.fixture
def trigger(tmp_path, monkeypatch):
    """Run `sledgehammer run ...` against a registered DAG; returns the trigger conf it would send."""
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / f"sledgehammer_Top_{getpass.getuser()}.py").write_text("'forceall': Param(default=False)")
    calls = []
    monkeypatch.setattr(sledgehammer_cli, "_airflow", lambda *a, capture=True: calls.append(a) or (0, "", ""))
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))

    def run(*argv: str) -> dict:
        calls.clear()
        assert sledgehammer_cli._cmd_run(list(argv) + ["--obj_dir", str(tmp_path / "Top"), "--no-wait"]) == 0
        sent = next(c for c in calls if c[:2] == ("dags", "trigger"))
        return json.loads(sent[sent.index("-c") + 1])
    return run


@pytest.mark.parametrize("stage", ["syn", "par"])
def test_step_flags_go_to_the_stage_named(trigger, stage) -> None:
    """`sledgehammer run par --from_step route_opt_design` (the CLI_REFERENCE example) used to step syn."""
    conf = trigger(stage, "--from_step", "route_opt_design")
    assert conf["steps_stage"] == stage and conf["from_step"] == "route_opt_design"


def test_an_explicit_steps_stage_wins(trigger) -> None:
    assert trigger("par", "--to_step", "syn_map", "--steps-stage", "syn")["steps_stage"] == "syn"


def test_no_step_flags_send_no_steps_stage(trigger) -> None:
    assert "steps_stage" not in trigger("par")


@pytest.mark.parametrize("stages", [("syn", "par"), ("drc",), ("lvs", "drc")])
def test_step_flags_without_one_syn_or_par_ask_which(trigger, capsys, stages) -> None:
    with pytest.raises(SystemExit) as cm:
        trigger(*stages, "--only_step", "place_opt_design")
    assert "--steps-stage" in str(cm.value.code)
