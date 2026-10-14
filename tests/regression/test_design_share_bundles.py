import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from hammer.vlsi import design_share, pd_store
from hammer.vlsi import fingerprints as fp


def _tokens(roots):
    return {token for _, token in roots}


def test_the_working_directory_is_a_root(tmp_path) -> None:
    roots = fp.make_roots(None, str(tmp_path / "vlsi" / "build" / "Top"), hammer_root="",
                          work_dir=str(tmp_path / "vlsi"))
    assert _tokens(roots) == {"<OBJ_DIR>", "<WORK_DIR>"}
    assert fp.tokenize(str(tmp_path / "vlsi" / "rtl" / "top.v"), roots) == "<WORK_DIR>/rtl/top.v"
    assert fp.tokenize(str(tmp_path / "vlsi" / "build" / "Top" / "x"), roots) == "<OBJ_DIR>/x"


@pytest.mark.parametrize("work", ["/", "~", "obj/inner"])
def test_a_working_directory_too_broad_or_inside_another_root_is_not_one(tmp_path, work) -> None:
    obj = tmp_path / "obj"
    work_dir = os.path.expanduser(work) if work != "obj/inner" else str(obj / "inner")
    assert "<WORK_DIR>" not in _tokens(fp.make_roots(None, str(obj), hammer_root="", work_dir=work_dir))


def _tar(members) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, body in members:
            tar.addfile(info, io.BytesIO(body) if body is not None else None)
    return buf.getvalue()


def _file(name: str, body: bytes = b"x\n") -> tuple:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    return info, body


def _link(name: str, target: str) -> tuple:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


@pytest.mark.parametrize("member", [_file("work/../escape"), _file("/etc/passwd"), _file("other/x"),
                                    _link("work/link", "/etc/passwd")])
def test_a_bundle_with_anything_but_plain_files_inside_work_or_obj_is_refused(tmp_path, member) -> None:
    with pytest.raises(design_share.ShareError, match="refusing"):
        design_share.unpack(_tar([member]), {}, str(tmp_path / "w"), str(tmp_path / "o"))
    assert not (tmp_path / "escape").exists()


def test_unpack_points_configs_at_the_new_directories_and_leaves_other_files_alone(tmp_path) -> None:
    source = {"work_dir": "/home/alice/vlsi", "obj_dir": "/scratch/alice/Top"}
    config = b'synthesis.inputs.input_files: ["/home/alice/vlsi/rtl/top.v"]\nx: /scratch/alice/Top/in.yml\n' \
             b'y: /home/alice/vlsi2/other.v\n'
    sdc = b"source /home/alice/vlsi/tcl/fix.tcl\n"
    data = _tar([_file("work/design.yml", config), _file("work/io.sdc", sdc)])
    work, obj = tmp_path / "bob", tmp_path / "bob-obj"
    design_share.unpack(data, {"source": source, "obj_dir_rel": None, "rewrite": ["work/design.yml"]},
                        str(work), str(obj))
    text = (work / "design.yml").read_text()
    assert f'["{work}/rtl/top.v"]' in text and f"x: {obj}/in.yml" in text
    assert "/home/alice/vlsi2/other.v" in text   # a sibling directory is not the working directory
    assert (work / "io.sdc").read_bytes() == sdc  # content-hashed collateral is never rewritten


def _manifest_bundle(files: dict, design="Top") -> dict:
    data = _tar([_file(name, body) for name, body in files.items()])
    manifest = {"version": design_share.BUNDLE_VERSION, "design": design, "source": {}, "obj_dir_rel": "build/Top",
                "rewrite": [], "files": {}, "external": [], "hammer": {}, "env_configs": [],
                "proj_confs_by_tools": {"default": []}, "default_tools": "default", "driver": None}
    return {"manifest": manifest, "data": data, "bundle_sha": json.dumps(sorted(files)), "updated_at": None}


def test_pull_refuses_a_directory_it_did_not_make(tmp_path) -> None:
    into = tmp_path / "mine"
    into.mkdir()
    (into / "my_work.v").write_text("mine\n")
    rec = _manifest_bundle({"work/top.v": b"theirs\n"})
    with pytest.raises(design_share.ShareError, match="not empty"):
        design_share.pull("alice", "Top", str(into), fetch=lambda o, d: rec, build=False)
    assert (into / "my_work.v").read_text() == "mine\n" and not (into / "top.v").exists()


def test_pulling_again_follows_the_owners_latest_and_drops_removed_files(tmp_path) -> None:
    into = tmp_path / "pulled"
    first = _manifest_bundle({"work/a.v": b"1\n", "work/old.v": b"gone soon\n"})
    design_share.pull("alice", "Top", str(into), fetch=lambda o, d: first, build=False)
    assert (into / "old.v").is_file()
    second = _manifest_bundle({"work/a.v": b"2\n"})
    design_share.pull("alice", "Top", str(into), fetch=lambda o, d: second, build=False)
    assert (into / "a.v").read_text() == "2\n" and not (into / "old.v").exists()
    with pytest.raises(design_share.ShareError, match="holds alice's Top"):
        design_share.pull("carol", "Top", str(into), fetch=lambda o, d: second, build=False)


def test_the_database_enforces_who_can_read_a_bundle() -> None:
    ddl = pd_store._DDL
    for table in (pd_store.FQ_DESIGN_BUNDLE, pd_store.FQ_DESIGN_BUNDLE_CHUNK, pd_store.FQ_DESIGN_SHARE):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in ddl
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in ddl
    shared = ddl[ddl.index("CREATE POLICY design_bundles_shared"):]
    assert "s.grantee = current_user" in shared[:shared.index("END IF;")]
    # the tables are created before the grants, so the group role covers them
    assert ddl.index(pd_store.FQ_DESIGN_SHARE) < ddl.index("REVOKE ALL ON SCHEMA")


def test_build_system_never_splits_a_cache_key() -> None:
    from hammer.config.config_src import RUN_CONTROL_KEYS
    assert "vlsi.core.build_system" in RUN_CONTROL_KEYS
    db = {"synthesis.inputs.top_module": "Top", "vlsi.core.build_system": "make"}
    other = dict(db, **{"vlsi.core.build_system": "sledgehammer", "vlsi.core.airflow_dag_id": "x"})
    assert pd_store.compute_stage_key(db, "synthesis") == pd_store.compute_stage_key(other, "synthesis")


def test_republish_does_nothing_without_the_cache_or_a_share(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace
    from hammer.vlsi import pd_cache
    driver = SimpleNamespace(obj_dir=str(tmp_path / "Top"))
    monkeypatch.setattr(pd_cache, "is_cache_enabled", lambda d: False)
    assert design_share.republish_if_shared(driver) is None
    monkeypatch.setattr(pd_cache, "is_cache_enabled", lambda d: True)
    monkeypatch.setattr(pd_store, "design_has_shares", lambda design: False)
    assert design_share.republish_if_shared(driver) is None
    monkeypatch.setattr(pd_store, "design_has_shares", lambda design: (_ for _ in ()).throw(OSError("db down")))
    assert design_share.republish_if_shared(driver) is None   # can't tell it is shared: stay quiet
    monkeypatch.setattr(pd_store, "design_has_shares", lambda design: True)
    monkeypatch.setattr(design_share, "bundle_from_driver", lambda d: (_ for _ in ()).throw(OSError("disk")))
    assert "could not refresh" in design_share.republish_if_shared(driver)
