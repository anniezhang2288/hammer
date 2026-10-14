import json
import os
import sys
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerDriver, design_share, pd_cache, pd_store


class FakeDB:
    """The shared database in memory, showing each user what the row-level security lets them see."""

    def __init__(self) -> None:
        self.user = "alice"
        self.blobs: dict = {}
        self.bundles: dict = {}
        self.shares: set = set()
        self.loads: list = []

    def store_stage_blob(self, stage, key, data, duration_seconds=None, cpu_seconds=None, **_):
        self.blobs[key] = (stage, data, duration_seconds, cpu_seconds)

    def load_stage_blob(self, key):
        self.loads.append(key)
        return self.blobs.get(key)

    def publish(self, design, manifest, data, bundle_sha):
        current = self.bundles.get((self.user, design))
        if current is not None and current["bundle_sha"] == bundle_sha:
            return False
        self.bundles[(self.user, design)] = {"manifest": json.loads(json.dumps(manifest)), "data": data,
                                             "bundle_sha": bundle_sha, "updated_at": "2026-10-14T00:00:00"}
        return True

    def fetch(self, owner, design):
        if owner != self.user and (owner, design, self.user) not in self.shares:
            return None
        return self.bundles.get((owner, design))

    def share(self, design, grantee):
        self.shares.add((self.user, design, grantee))


@pytest.fixture
def db(monkeypatch, tmp_path):
    fake = FakeDB()
    monkeypatch.setattr(pd_store, "store_stage_blob", fake.store_stage_blob)
    monkeypatch.setattr(pd_store, "load_stage_blob", fake.load_stage_blob)
    monkeypatch.setattr(pd_store, "publish_design_bundle", fake.publish)
    monkeypatch.setattr(pd_store, "fetch_design_bundle", fake.fetch)
    monkeypatch.setattr(pd_store, "share_design", fake.share)
    monkeypatch.setattr(pd_store, "design_has_shares",
                        lambda design: any(o == fake.user and d == design for o, d, _ in fake.shares))
    monkeypatch.setattr(pd_store, "current_db_user", lambda: fake.user)
    monkeypatch.setattr(pd_cache, "_record_cache_event", lambda *a, **k: None)
    monkeypatch.setattr(pd_cache, "_stamp_project_from_config", lambda *a, **k: None)
    monkeypatch.setenv("HAMMER_PD_CACHE", "1")
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    for name in ("HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID", "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    # the DAG bakes the driver that ran `build`; make that this checkout's hammer-vlsi, as make buildfile does
    monkeypatch.setattr(sys, "argv", [os.path.join(os.path.dirname(sys.executable), "hammer-vlsi")])
    return fake


@pytest.fixture
def tool_runs(monkeypatch):
    """(stage, working directory) of every real tool run."""
    runs = []
    for name in ("run_synthesis", "run_par"):
        real = getattr(HammerDriver, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            runs.append((_name, os.getcwd()))
            return _real(self, *args, **kwargs)
        monkeypatch.setattr(HammerDriver, name, spy)
    return runs


def _alice_design(tmp_path: Path) -> Path:
    """Alice's vlsi directory: RTL with an include dir, an SDC, and configs naming them by absolute path."""
    work = tmp_path / "alice" / "chipyard" / "vlsi"
    (work / "rtl" / "inc").mkdir(parents=True)
    (work / "rtl" / "inc" / "defs.vh").write_text("`define WIDTH 1\n")
    (work / "rtl" / "top.v").write_text('`include "defs.vh"\n'
                                       'module dummy(input a, output b); assign b = a; endmodule\n')
    (work / "constraints").mkdir()
    (work / "constraints" / "io.sdc").write_text("set_load 1 [all_outputs]\n")
    (work / "env.yml").write_text("{}\n")
    (work / "design.yml").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": "sledgehammer",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "vlsi.inputs.custom_sdc_files": [str(work / "constraints" / "io.sdc")],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(work / "rtl" / "top.v")],
        "synthesis.inputs.include_dirs": [str(work / "rtl" / "inc")],
    }, cls=HammerJSONEncoder, indent=4))
    return work


def _hammer(action: str, envs, projs, obj_dir: str, out: Path) -> None:
    args = [action]
    for e in envs:
        args += ["-e", e]
    for p in projs:
        args += ["-p", p]
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=args + ["--obj_dir", obj_dir, "-o", str(out / f"{action}-out.json"),
                                      "--log", str(out / "log.txt")])
    assert cm.value.code == 0, action


def _run_dag(dag_file: str, out: Path, through_par: bool = False) -> None:
    """syn (and syn-to-par, par) exactly as the DAG's tasks run them: from WORK_DIR, with its configs."""
    dag = design_share.read_dag(dag_file)
    obj = dag["OBJ_DIR"]
    old = os.getcwd()
    os.chdir(dag["WORK_DIR"])
    try:
        _hammer("syn", dag["ENV_CONFIGS"], dag["PROJ_CONFIGS"], obj, out)
        if through_par:
            par_in = os.path.join(obj, "par-input.json")
            with pytest.raises(SystemExit) as cm:
                CLIDriver().main(args=["syn-to-par"] + [a for e in dag["ENV_CONFIGS"] for a in ("-e", e)]
                                 + ["-p", os.path.join(obj, "syn-rundir", "syn-output-full.json"), "-o", par_in,
                                    "--obj_dir", obj, "--log", str(out / "log.txt")])
            assert cm.value.code == 0
            _hammer("par", dag["ENV_CONFIGS"], [par_in], obj, out)
    finally:
        os.chdir(old)


def test_a_partner_who_pulls_the_design_restores_the_owners_syn_and_par_from_the_cache(tmp_path, db,
                                                                                      tool_runs, monkeypatch) -> None:
    alice = _alice_design(tmp_path)
    obj_a = alice / "build" / "dummy"
    monkeypatch.chdir(alice)
    _hammer("build", [str(alice / "env.yml")], [str(alice / "design.yml")], str(obj_a), tmp_path)
    _run_dag(str(obj_a / "hammer_dag.py"), tmp_path, through_par=True)
    assert tool_runs == [("run_synthesis", str(alice)), ("run_par", str(alice))] and len(db.blobs) == 2
    alices_keys = list(db.blobs)

    bundle = design_share.bundle_from_dag(str(obj_a / "hammer_dag.py"), owner="alice")
    assert db.publish("dummy", bundle.manifest, bundle.data, bundle.bundle_sha)
    db.share("dummy", "bob")

    db.user = "bob"
    into = tmp_path / "bob" / "pulled"
    summary = design_share.pull("alice", "dummy", str(into), fetch=db.fetch)
    assert summary["missing_external"] == [] and summary["hammer_mismatch"] is None
    pulled = into / "rtl" / "top.v"
    assert pulled.read_text() == (alice / "rtl" / "top.v").read_text()
    assert (into / "rtl" / "inc" / "defs.vh").is_file() and (into / "constraints" / "io.sdc").is_file()
    assert str(alice) not in (into / "design.yml").read_text()

    db.loads.clear()
    _run_dag(summary["dag_file"], tmp_path, through_par=True)
    assert db.loads == alices_keys            # Bob's syn and par computed Alice's keys...
    assert len(tool_runs) == 2                # ...and restored her results instead of running the tools
    for rundir, output in (("syn-rundir", "syn-output.json"), ("par-rundir", "par-output.json")):
        restored = (into / "build" / "dummy" / rundir / output).read_text()
        assert str(alice) not in restored     # and point at Bob's own copy of the design


def test_a_partner_nobody_shared_with_gets_nothing(tmp_path, db, monkeypatch) -> None:
    alice = _alice_design(tmp_path)
    obj_a = alice / "build" / "dummy"
    monkeypatch.chdir(alice)
    _hammer("build", [str(alice / "env.yml")], [str(alice / "design.yml")], str(obj_a), tmp_path)
    bundle = design_share.bundle_from_dag(str(obj_a / "hammer_dag.py"), owner="alice")
    db.publish("dummy", bundle.manifest, bundle.data, bundle.bundle_sha)
    db.user = "mallory"
    with pytest.raises(design_share.ShareError, match="has not shared it with you"):
        design_share.pull("alice", "dummy", str(tmp_path / "m"), fetch=db.fetch)
    assert not (tmp_path / "m").exists()


def test_the_owners_next_syn_republishes_the_changed_design_and_a_repull_follows_it(tmp_path, db, tool_runs,
                                                                                     monkeypatch) -> None:
    alice = _alice_design(tmp_path)
    obj_a = alice / "build" / "dummy"
    monkeypatch.chdir(alice)
    _hammer("build", [str(alice / "env.yml")], [str(alice / "design.yml")], str(obj_a), tmp_path)
    bundle = design_share.bundle_from_dag(str(obj_a / "hammer_dag.py"), owner="alice")
    db.publish("dummy", bundle.manifest, bundle.data, bundle.bundle_sha)
    db.share("dummy", "bob")
    first = db.bundles[("alice", "dummy")]["bundle_sha"]

    (alice / "rtl" / "top.v").write_text('`include "defs.vh"\n'
                                        'module dummy(input a, output b); assign b = ~a; endmodule\n')
    _run_dag(str(obj_a / "hammer_dag.py"), tmp_path)
    assert db.bundles[("alice", "dummy")]["bundle_sha"] != first
    alices_key = next(iter(db.blobs))

    db.user = "bob"
    into = tmp_path / "bob" / "pulled"
    summary = design_share.pull("alice", "dummy", str(into), fetch=db.fetch)
    assert "~a" in (into / "rtl" / "top.v").read_text()
    db.loads.clear()
    _run_dag(summary["dag_file"], tmp_path)
    assert db.loads == [alices_key] and len(tool_runs) == 1


def test_studio_share_and_pull_tell_each_side_what_to_run(tmp_path, db, capsys, monkeypatch) -> None:
    from hammer.shell import pd_store_cli
    alice = _alice_design(tmp_path)
    obj_a = alice / "build" / "dummy"
    monkeypatch.chdir(alice)
    _hammer("build", [str(alice / "env.yml")], [str(alice / "design.yml")], str(obj_a), tmp_path)
    capsys.readouterr()
    assert pd_store_cli.main(["share", "dummy", "bob", "--obj-dir", str(obj_a)]) == 0
    out = capsys.readouterr().out
    assert "Published dummy" in out and "studio pull alice dummy" in out
    assert pd_store_cli.main(["publish", "dummy", "--obj-dir", str(obj_a)]) == 0
    assert "Already up to date" in capsys.readouterr().out

    db.user = "bob"
    monkeypatch.chdir(tmp_path)
    assert pd_store_cli.main(["pull", "alice", "dummy"]) == 0
    out = capsys.readouterr().out
    assert f"cd {tmp_path / 'dummy-from-alice'}" in out and "sledgehammer run syn par" in out
    dag = design_share.read_dag(str(tmp_path / "dummy-from-alice" / "build" / "dummy" / "hammer_dag.py"))
    assert dag["WORK_DIR"] == str(tmp_path / "dummy-from-alice")
    assert (tmp_path / "dags" / f"{design_share.dag_id_for('dummy', 'alice')}.py").is_symlink()

    assert pd_store_cli.main(["pull", "alice", "other"]) == 2
    assert "has not shared it with you" in capsys.readouterr().err
