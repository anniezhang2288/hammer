import json
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in ("HAMMER_PD_CACHE", "HAMMER_AIRFLOW_DESIGN", "HAMMER_SUBSTEP_RESUME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")


class Flow:
    def __init__(self, tmp_path: Path, capsys) -> None:
        self.tmp = tmp_path
        self.obj = tmp_path / "obj"
        self.capsys = capsys
        self.rtl = tmp_path / "top.v"
        self.rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
        (tmp_path / "config.json").write_text(json.dumps({
            "vlsi.core.technology": "hammer.technology.nop",
            "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
            "vlsi.core.par_tool": "hammer.par.nop",
            "vlsi.inputs.hierarchical.config_source": "none",
            "vlsi.technology.extra_macro_sizes": [],
            "synthesis.inputs.top_module": "dummy",
            "synthesis.inputs.input_files": [str(self.rtl)],
        }, cls=HammerJSONEncoder, indent=4))

    def run(self, action: str, config: str) -> tuple:
        self.capsys.readouterr()
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=[action, "-p", config, "-o", str(self.tmp / f"{action}-out.json"),
                                   "--obj_dir", str(self.obj), "--log", str(self.tmp / "log.txt")])
        captured = self.capsys.readouterr()
        return cm.value.code, captured.out + captured.err


def test_par_runs_after_the_rtl_is_gone(tmp_path, capsys) -> None:
    """Only synthesis reads the RTL; upstream Hammer ran par from syn's outputs with the RTL cleaned away."""
    f = Flow(tmp_path, capsys)
    assert f.run("syn", str(tmp_path / "config.json"))[0] == 0
    assert f.run("syn-to-par", str(f.obj / "syn-rundir" / "syn-output-full.json"))[0] == 0
    assert f.run("par", str(tmp_path / "syn-to-par-out.json"))[0] == 0
    f.rtl.unlink()
    code, out = f.run("par", str(tmp_path / "syn-to-par-out.json"))
    assert code == 0, out
    assert "RTL file not found" in out and "without the RTL fingerprint" in out
    assert "can skip par" not in out  # the fingerprint it last ran with is gone, so par reruns


def test_syn_still_fails_without_its_rtl(tmp_path, capsys) -> None:
    f = Flow(tmp_path, capsys)
    f.rtl.unlink()
    code, out = f.run("syn", str(tmp_path / "config.json"))
    assert code == 1
    assert "RTL file not found" in out
