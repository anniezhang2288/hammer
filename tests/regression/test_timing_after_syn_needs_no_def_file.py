import json

from hammer.vlsi import HammerDriver, HammerDriverOptions


def test_timing_after_syn_loads_without_a_def_file(tmp_path) -> None:
    """syn-to-timing never sets timing.inputs.def_file, so it needs a default."""
    netlist = tmp_path / "dummy.mapped.v"
    netlist.write_text("module dummy(); endmodule\n")
    syn_out = {
        "synthesis.outputs.output_files": [str(netlist)],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.outputs.sdc": None,
        "synthesis.outputs.sdf_file": None,
    }
    timing_in = HammerDriver.synthesis_output_to_timing_input(syn_out)
    assert timing_in is not None and "timing.inputs.def_file" not in timing_in
    project = dict(timing_in, **{
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.timing_tool": "hammer.timing.tempus",
        "timing.tempus.tempus_bin": "/usr/bin/true",
        "vlsi.inputs.hierarchical.config_source": "none",
    })
    (tmp_path / "timing-in.json").write_text(json.dumps(project))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(tmp_path / "timing-in.json")],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    assert driver.load_timing_tool()
    assert driver.timing_tool.def_file is None
