import json
import shutil
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, rtl_check


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(rtl_check, "digest_units", _fixed_digest)
    monkeypatch.setattr(rtl_check, "digest_files", _fixed_digest)
    for name in ("HAMMER_PD_CACHE", "HAMMER_AIRFLOW_DESIGN", "HAMMER_SUBSTEP_RESUME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")


class Flow:
    """drc and lvs with the mock tools on a fixed layout, run as make runs them."""

    def __init__(self, tmp_path: Path, capsys) -> None:
        self.tmp = tmp_path
        self.obj = tmp_path / "obj"
        self.capsys = capsys
        rtl = tmp_path / "top.v"
        rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
        gds = tmp_path / "dummy.gds"
        gds.write_text("gds\n")
        (tmp_path / "config.json").write_text(json.dumps({
            "vlsi.core.technology": "hammer.technology.nop",
            "vlsi.core.drc_tool": "hammer.drc.mockdrc",
            "vlsi.core.lvs_tool": "hammer.lvs.mocklvs",
            "vlsi.inputs.hierarchical.config_source": "none",
            "synthesis.inputs.top_module": "dummy",
            "synthesis.inputs.input_files": [str(rtl)],
            "drc.inputs.top_module": "dummy",
            "drc.inputs.layout_file": str(gds),
            "lvs.inputs.top_module": "dummy",
            "lvs.inputs.layout_file": str(gds),
            "lvs.inputs.schematic_files": [str(rtl)],
        }, cls=HammerJSONEncoder, indent=4))

    def run(self, action: str) -> str:
        self.capsys.readouterr()
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=[action, "-p", str(self.tmp / "config.json"), "-o", str(self.out(action)),
                                   "--obj_dir", str(self.obj), "--log", str(self.tmp / "log.txt")])
        out = self.capsys.readouterr().out
        assert cm.value.code == 0, out
        return out

    def out(self, action: str) -> Path:
        return self.tmp / f"{action}-out.json"


@pytest.mark.parametrize("stage", ["drc", "lvs"])
def test_an_unchanged_stage_returns_the_output_of_its_last_run(tmp_path, capsys, stage) -> None:
    f = Flow(tmp_path, capsys)
    f.run(stage)
    first = json.loads(f.out(stage).read_text())
    assert f"can skip {stage}" in f.run(stage)
    second = json.loads(f.out(stage).read_text())
    assert isinstance(second, dict)
    assert second == first


@pytest.mark.parametrize("stage", ["drc", "lvs"])
def test_an_unchanged_stage_whose_rundir_is_gone_runs_again(tmp_path, capsys, stage) -> None:
    f = Flow(tmp_path, capsys)
    f.run(stage)
    shutil.rmtree(f.obj / f"{stage}-rundir")
    out = f.run(stage)
    assert f"{stage} is unchanged, but {stage}-output.json is missing; running {stage}." in out
    assert (f.obj / f"{stage}-rundir" / f"{stage}-output.json").is_file()
    assert isinstance(json.loads(f.out(stage).read_text()), dict)
    assert f"can skip {stage}" in f.run(stage)
