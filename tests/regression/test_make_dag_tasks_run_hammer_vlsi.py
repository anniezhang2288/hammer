import os
import sys
from pathlib import Path

from hammer.shell import pd_store_cli


def _baked(path: Path, key: str) -> str:
    for line in path.read_text().splitlines():
        if line.startswith(key):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise AssertionError(f"{key} not in {path}")


def test_make_dag_points_the_dag_and_hammer_d_at_hammer_vlsi(tmp_path, monkeypatch) -> None:
    """studio is not a Hammer driver: a DAG whose tasks ran `python studio -e ... syn` failed every task."""
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    e2e = tmp_path / "repo" / "e2e"
    for d in ("configs-env", "configs-pdk", "configs-tool"):
        (e2e / d).mkdir(parents=True)
    (e2e / "configs-env" / "t-env.yml").write_text("{}\n")
    (e2e / "configs-pdk" / "nop.yml").write_text("vlsi.core.technology: hammer.technology.nop\n")
    (e2e / "configs-tool" / "mock.yml").write_text("vlsi.core.synthesis_tool: hammer.synthesis.mocksynth\n")
    design = tmp_path / "design"
    design.mkdir()
    (design / "common.yml").write_text("vlsi.inputs.hierarchical.config_source: none\n"
                                       "synthesis.inputs.top_module: dummy\n")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "bin" / "studio"), "make-dag"])
    assert pd_store_cli.main(["make-dag", "--design", "dummy", "--design-dir", str(design),
                              "--repo-root", str(tmp_path / "repo"), "--obj-dir", str(tmp_path / "obj"),
                              "--dags-folder", str(tmp_path / "dags"), "--pdk", "nop", "--tools", "mock",
                              "--env", "t", "--skip-augment"]) == 0
    hammer_vlsi = os.path.realpath(os.path.join(os.path.dirname(sys.executable), "hammer-vlsi"))
    assert _baked(tmp_path / "obj" / "hammer_dag.py", "HAMMER_EXEC") == hammer_vlsi
    assert _baked(tmp_path / "obj" / "hammer.d", "HAMMER_EXEC ?") == hammer_vlsi
