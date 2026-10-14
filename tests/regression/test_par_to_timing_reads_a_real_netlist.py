import json

import pytest

from hammer.vlsi import CLIDriver, HammerDriver, rtl_check

PAR_OUTPUT = {
    "par.outputs.output_netlist": "/run/dummy.lvs.v",
    "par.inputs.top_module": "dummy",
    "par.inputs.post_synth_sdc": None,
    "par.outputs.spefs": [],
}


@pytest.mark.parametrize("physical", [None, "", "None"])
def test_par_to_timing_falls_back_to_the_netlist_without_a_physical_one(physical) -> None:
    """A missing physical netlist, or the string "None" older par outputs recorded for one, is not a file."""
    out = dict(PAR_OUTPUT)
    if physical is not None:
        out["par.outputs.output_physical_netlist"] = physical
    timing = HammerDriver.par_output_to_timing_input(out)
    assert timing is not None
    assert timing["timing.inputs.input_files"] == ["/run/dummy.lvs.v"]


def test_par_to_timing_prefers_the_physical_netlist() -> None:
    out = dict(PAR_OUTPUT, **{"par.outputs.output_physical_netlist": "/run/dummy.physical.v"})
    assert HammerDriver.par_output_to_timing_input(out)["timing.inputs.input_files"] == ["/run/dummy.physical.v"]


def test_a_par_tool_without_a_physical_netlist_does_not_export_one(tmp_path, monkeypatch) -> None:
    """mockpar, like every par tool but innovus, never sets output_physical_netlist."""
    monkeypatch.setattr(rtl_check, "digest_units", lambda *a, **k: ("0" * 64, []))
    monkeypatch.setattr(rtl_check, "digest_files", lambda *a, **k: ("0" * 64, []))
    monkeypatch.delenv("HAMMER_PD_CACHE", raising=False)
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.mockpar",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
    }))
    obj = tmp_path / "obj"

    def run(action: str, config: str) -> None:
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=[action, "-p", config, "-o", str(tmp_path / f"{action}-out.json"),
                                   "--obj_dir", str(obj), "--log", str(tmp_path / "log.txt")])
        assert cm.value.code == 0, action

    run("syn", str(tmp_path / "config.json"))
    run("syn-to-par", str(obj / "syn-rundir" / "syn-output-full.json"))
    run("par", str(tmp_path / "syn-to-par-out.json"))
    for name in ("par-output.json", "par-output-full.json"):
        assert "par.outputs.output_physical_netlist" not in json.loads((obj / "par-rundir" / name).read_text())
