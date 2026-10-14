"""
studio: CLI for the Postgres-backed PD store.

Roughly grouped by what each subcommand is for:

Schema and JSON-artifact operations (from the original POC):
    init              Create the hammer_poc schema and tables. Idempotent.
    list [-n N]       Show the most recent JSON artifacts.
    get  <sha256>     Print one artifact to stdout.
    put  <path>       Store a JSON file. Prints its sha256.

Master-database and per-stage cache blobs:
    master-push <design> [--master <path>]
    master-pull <design> [--out <path>]
    master-list [-n N]               (list master_database rows with provenance)
    stage-key   <stage_tag> [--master <path>]
    stage-push  <stage_tag> --rundir <path> [--master <path>]
    stage-pull  <stage_tag> --rundir <path> [--master <path>]
    blob-list   [--stage <tag>] [-n N]

Per-user workspaces (so that one user's "clean" task can't wipe another
user's build dir):
    workspace-list                  List every registered workspace.
    workspace-show  <username>      Print one user's workspace root.
    workspace-set   <username> <p>  Set or update a user's workspace.
    workspace-unset <username>      Drop a user's registration.

Cache wipes (destructive; default behavior prompts for confirmation):
    wipe-blobs     [--stage <tag>]   Delete stage tarballs. Forces cold run.
    wipe-master    [--design <name>] Delete dep-check baseline.
    wipe-artifacts [--kind <kind>]   Delete JSON artifacts (par-input, etc.).
    wipe-all                         Delete all cache. user_workspaces is kept.

Design registration (turn RTL into Hammer configs without hand-writing YAML):
    design-register --name <n> --top-module <m> --rtl <paths...> --clock-ns <c>
    augment --design <n>            (fill in SRAM stuff for an existing design)
    make-dag --design <n>           (generate the Airflow DAG and link it in)

Design sharing (build a partner's design yourself, starting from their cache):
    share   <design> <partner...>   Publish your design's files and let these partners pull them.
    unshare <design> <partner>      Stop a partner pulling it.
    publish <design>                Upload your design's current files again (syn does this itself).
    shares                          Designs you shared, and designs shared with you.
    pull    <owner> <design>        Fetch a shared design and generate your own DAG for it.

`<path>` defaults to ./master_database.json when omitted.

Connection settings come from HAMMER_PG_* env vars, or fall back to
sql_alchemy_conn in airflow.cfg. See hammer/vlsi/pd_store.py for the
precise resolution order.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from hammer.config import HammerJSONEncoder
from hammer.vlsi import pd_store


def _cmd_init(_args: argparse.Namespace) -> int:
    pd_store.ensure_schema()
    print(f"Initialized schema '{pd_store.SCHEMA_NAME}' and table '{pd_store.TABLE_NAME}'.")
    return 0


def _short_str(s: Optional[str], n: int) -> str:
    if s is None:
        return "-"
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def _cmd_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_artifacts(limit=args.limit)
    if not rows:
        print("(no artifacts)")
        return 0
    header = (f"{'sha':<10} {'kind':<11} {'top_module':<18} {'owner':<14} "
              f"{'trig_user':<14} {'design':<28} {'dag_id':<22}  created_at")
    print(header)
    print("-" * len(header))
    for r in rows:
        sha, kind, top_module, owner, trig, dag, design, workspace, created = r
        print(f"{_short_str(sha,10):<10} {_short_str(kind,11):<11} "
              f"{_short_str(top_module,18):<18} {_short_str(owner,14):<14} "
              f"{_short_str(trig,14):<14} {_short_str(design,28):<28} "
              f"{_short_str(dag,22):<22}  {created}")
    return 0


def _cmd_master_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_master_databases(limit=args.limit)
    if not rows:
        print("(no master_databases)")
        return 0
    header = (f"{'design':<32} {'owner':<14} {'trig_user':<14} "
              f"{'dag_id':<28} {'workspace':<48}  updated_at")
    print(header)
    print("-" * len(header))
    for r in rows:
        design, owner, trig, dag, workspace, updated = r
        print(f"{_short_str(design,32):<32} {_short_str(owner,14):<14} "
              f"{_short_str(trig,14):<14} {_short_str(dag,28):<28} "
              f"{_short_str(workspace,48):<48}  {updated}")
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    data = pd_store.load_artifact(args.sha256)
    if data is None:
        print(f"No artifact found with sha256={args.sha256}", file=sys.stderr)
        return 1
    print(json.dumps(data, cls=HammerJSONEncoder, indent=4))
    return 0


def _cmd_put(args: argparse.Namespace) -> int:
    with open(args.path, "r") as f:
        data = json.load(f)
    sha = pd_store.store_artifact(data, kind=args.kind)
    print(sha)
    return 0


def _read_master(path: Optional[str]) -> dict:
    p = Path(path) if path else Path("master_database.json")
    if not p.is_file():
        print(f"master_database not found at {p}", file=sys.stderr)
        sys.exit(2)
    with p.open("r") as f:
        return json.load(f)


def _cmd_master_push(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    pd_store.store_master_database(args.design, master_db)
    print(f"Pushed master_database for design '{args.design}'.")
    return 0


def _cmd_master_pull(args: argparse.Namespace) -> int:
    db = pd_store.load_master_database(args.design)
    if db is None:
        print(f"No master_database found for design '{args.design}'.", file=sys.stderr)
        return 1
    payload = json.dumps(db, cls=HammerJSONEncoder, indent=4, sort_keys=True)
    if args.out:
        Path(args.out).write_text(payload)
        print(f"Wrote {args.out}")
    else:
        print(payload)
    return 0


def _cmd_stage_key(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    print(pd_store.compute_stage_key(master_db, args.stage))
    return 0


def _cmd_stage_push(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    rundir = Path(args.rundir)
    if not rundir.is_dir():
        print(f"rundir not found: {rundir}", file=sys.stderr)
        return 2
    sha = pd_store.compute_stage_key(master_db, args.stage)
    data = pd_store.tar_directory(rundir)
    pd_store.store_stage_blob(args.stage, sha, data)
    print(f"sha256={sha} stage={args.stage} bytes={len(data)} from={rundir}")
    return 0


def _cmd_stage_pull(args: argparse.Namespace) -> int:
    master_db = _read_master(args.master)
    sha = pd_store.compute_stage_key(master_db, args.stage)
    blob = pd_store.load_stage_blob(sha)
    if blob is None:
        print(f"No blob for sha256={sha} stage={args.stage}", file=sys.stderr)
        return 1
    stored_stage, data, _duration, _cpu = blob
    rundir = Path(args.rundir)
    if rundir.exists():
        if not args.overwrite:
            print(
                f"{rundir} already exists. Pass --overwrite to replace it.",
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(rundir)
    rundir.parent.mkdir(parents=True, exist_ok=True)
    pd_store.untar_to_directory(data, rundir.parent, as_name=rundir.name)
    print(f"sha256={sha} stage={stored_stage} extracted to {rundir}")
    return 0


def _cmd_grant(args: argparse.Namespace) -> int:
    try:
        pd_store.grant_access(args.role)
        print(f"Added '{args.role}' to {pd_store.SLEDGEHAMMER_GROUP}.")
    except Exception as e:
        if "does not exist" not in str(e):
            raise
        # no group role on this cluster (needs CREATEROLE): grant directly
        pd_store.grant_schema_access(args.role)
        print(f"Group role {pd_store.SLEDGEHAMMER_GROUP} doesn't exist on this "
              f"cluster; granted '{args.role}' direct schema access instead.")
    return 0


def _cmd_onboard(args: argparse.Namespace) -> int:
    """One command for a new teammate: metadata DB + schema access."""
    for line in pd_store.onboard_user(args.role):
        print(f"  {line}")
    print(f"'{args.role}' is set up. They can now run: sledgehammer")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    pd_store.revoke_access(args.role)
    print(f"Removed '{args.role}' from {pd_store.SLEDGEHAMMER_GROUP}.")
    return 0


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Prompt the user to confirm a destructive op. Returns True to proceed."""
    if assume_yes:
        return True
    try:
        reply = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def _cmd_wipe_blobs(args: argparse.Namespace) -> int:
    target = (
        f"all rows in {pd_store.FQ_BLOB}"
        if not args.stage
        else f"rows in {pd_store.FQ_BLOB} where stage = '{args.stage}'"
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    n = pd_store.delete_stage_blobs(stage_tag=args.stage)
    print(f"Deleted {n} row(s) from {pd_store.FQ_BLOB}.")
    return 0


def _cmd_wipe_master(args: argparse.Namespace) -> int:
    target = (
        f"all rows in {pd_store.FQ_MASTER}"
        if not args.design
        else f"the row in {pd_store.FQ_MASTER} for design = '{args.design}'"
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    n = pd_store.delete_master_databases(design=args.design)
    print(f"Deleted {n} row(s) from {pd_store.FQ_MASTER}.")
    return 0


def _cmd_wipe_artifacts(args: argparse.Namespace) -> int:
    target = (
        f"all rows in {pd_store.FQ_TABLE}"
        if not args.kind
        else f"rows in {pd_store.FQ_TABLE} where kind = '{args.kind}'"
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    n = pd_store.delete_artifacts(kind=args.kind)
    print(f"Deleted {n} row(s) from {pd_store.FQ_TABLE}.")
    return 0


def _cmd_wipe_all(args: argparse.Namespace) -> int:
    # Note: user_workspaces is intentionally left alone. That's config (where
    # each user's builds live), not cache state, and clobbering it would force
    # every user to re-register on next login.
    target = (
        f"ALL cache data: pd_blobs + master_databases + pd_artifacts in "
        f"schema {pd_store.SCHEMA_NAME}. user_workspaces is preserved."
    )
    if not _confirm(f"This will permanently delete {target}. Continue?", args.yes):
        print("Aborted.", file=sys.stderr)
        return 1
    blobs = pd_store.delete_stage_blobs()
    master = pd_store.delete_master_databases()
    artifacts = pd_store.delete_artifacts()
    print(f"Deleted {blobs} row(s) from {pd_store.FQ_BLOB}.")
    print(f"Deleted {master} row(s) from {pd_store.FQ_MASTER}.")
    print(f"Deleted {artifacts} row(s) from {pd_store.FQ_TABLE}.")
    return 0


def _cmd_design_register(args: argparse.Namespace) -> int:
    """
    Walk a directory of RTL and write out the Hammer configs for the design.

    See hammer/shell/design_register.py for the actual work. This wrapper
    just validates arg paths, hands them off, then prints the warning list
    and the next steps the user still has to do by hand.
    """
    from hammer.shell import design_register
    rtl_paths = [Path(p) for p in args.rtl]
    for p in rtl_paths:
        if not p.exists():
            print(f"RTL path not found: {p}", file=sys.stderr)
            return 2
    out_dir, warnings = design_register.register_design(
        name=args.name,
        top_module=args.top_module,
        clock_ns=args.clock_ns,
        rtl_paths=rtl_paths,
        pdk=args.pdk,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        exclude_patterns=args.exclude,
        use_default_excludes=not args.no_default_excludes,
    )
    if warnings:
        print("", file=sys.stderr)
        print("WARNINGS:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    print(f"\nDesign registered. Configs at {out_dir}.", file=sys.stderr)
    print(f"Next steps:", file=sys.stderr)
    print(f"  1. Edit {out_dir}/par.yml to add macro placement constraints.", file=sys.stderr)
    print(f"  2. Write a DAG file for this design (no factory yet, so still a copy-paste).", file=sys.stderr)
    print(f"  3. Trigger via the Airflow UI or hammer-vlsi syn_par.", file=sys.stderr)
    return 0


def _cmd_augment(args: argparse.Namespace) -> int:
    """
    Look at a design's existing common.yml, find the SRAM macros, and write
    the blackbox stubs + sky130-extras.yml. Doesn't touch the user's other
    configs.
    """
    from hammer.shell import design_register

    design_dir = Path(args.design_dir) if args.design_dir else None
    if design_dir is None:
        cwd = Path.cwd()
        if (cwd / "e2e").is_dir():
            design_dir = cwd / "e2e" / "configs-design" / args.design
        else:
            design_dir = Path("e2e/configs-design") / args.design

    if not design_dir.is_dir():
        print(f"Design directory not found: {design_dir}", file=sys.stderr)
        print(f"Create it first (with common.yml etc.) or pass --design-dir.",
              file=sys.stderr)
        return 2

    try:
        srams, warnings = design_register.augment_existing_design(design_dir)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"\nAugmented {design_dir}.", file=sys.stderr)
    print(f"  SRAM macros bound: {len(srams)}", file=sys.stderr)
    if warnings:
        print("\nNotes:", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)
    return 0


def _cmd_make_dag(args: argparse.Namespace) -> int:
    """
    Build the Airflow DAG for an already-configured design and symlink it
    into the dags/ folder so Airflow's scheduler picks it up.
    """
    import os

    design = args.design
    design_dir = Path(args.design_dir) if args.design_dir else None
    if design_dir is None:
        cwd = Path.cwd()
        if (cwd / "e2e").is_dir():
            design_dir = cwd / "e2e" / "configs-design" / design
        else:
            design_dir = Path("e2e/configs-design") / design

    if not design_dir.is_dir():
        print(f"Design directory not found: {design_dir}", file=sys.stderr)
        return 2

    if not args.skip_augment:
        from hammer.shell import design_register
        try:
            srams, warnings = design_register.augment_existing_design(design_dir)
            print(f"  Augment: bound {len(srams)} SRAM macros", file=sys.stderr)
            for w in warnings:
                print(f"  WARNING: {w}", file=sys.stderr)
        except (FileNotFoundError, ValueError) as e:
            print(f"Augment step failed: {e}", file=sys.stderr)
            print("Pass --skip-augment to bypass.", file=sys.stderr)
            return 2

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    e2e = repo_root / "e2e"
    obj_dir = Path(args.obj_dir) if args.obj_dir else (
        e2e / f"build-{args.pdk}-{args.tools}" / design
    )
    obj_dir.mkdir(parents=True, exist_ok=True)

    dags_folder = Path(args.dags_folder) if args.dags_folder else (repo_root / "dags")

    env_conf = e2e / "configs-env" / f"{args.env}-env.yml"
    pdk_conf = e2e / "configs-pdk" / f"{args.pdk}.yml"
    tools_dir = e2e / "configs-tool"
    tools_conf = tools_dir / f"{args.tools}.yml"

    # Discover every tool config so the DAG can offer them as a runtime
    # dropdown. The default stays whatever --tools picked.
    design_confs = [str(yml) for yml in sorted(design_dir.glob("*.yml"))]
    proj_confs_by_tools: Dict[str, List[str]] = {}
    for tool_yml in sorted(tools_dir.glob("*.yml")):
        proj_confs_by_tools[tool_yml.stem] = [str(pdk_conf), str(tool_yml)] + design_confs

    # The driver itself needs ONE project_configs list to compute the dep
    # graph (hier vs flat detection, top_module resolution, etc.). Use the
    # caller's chosen --tools.
    proj_confs = proj_confs_by_tools.get(
        args.tools, [str(pdk_conf), str(tools_conf)] + design_confs
    )

    from hammer.vlsi import HammerDriver, HammerDriverOptions
    import hammer.config as hcfg

    # build_system hammer is legacy Hammer run by make, and every task of its DAG would refuse to
    # run. extra_dict below forces airflow, so ask the design's own configs first.
    from hammer.vlsi import legacy_hammer
    if legacy_hammer.is_legacy_hammer_project(proj_confs):
        print(f"vlsi.core.build_system is hammer for '{design}', which runs plain legacy Hammer driven "
              f"by make, so no DAG was generated. Run the flow with make, or set "
              f"vlsi.core.build_system: sledgehammer to build it with a DAG.", file=sys.stderr)
        return 2

    opts = HammerDriverOptions(
        environment_configs=[str(env_conf)],
        project_configs=proj_confs,
        log_file=str(obj_dir / "hammer.log"),
        obj_dir=str(obj_dir),
    )
    extra_dict = {
        "vlsi.core.build_system": "airflow",
        "vlsi.core.airflow_dags_folder": str(dags_folder),
    }
    if args.dag_id:
        extra_dict["vlsi.core.airflow_dag_id"] = args.dag_id
    import json
    extra = hcfg.load_config_from_string(json.dumps(extra_dict), is_yaml=False)
    driver = HammerDriver(opts, extra)

    from hammer.vlsi.hammer_build_systems import build_airflow_dag
    errs: List[str] = []
    dep_graph = build_airflow_dag(
        driver, errs.append,
        proj_confs_by_tools=proj_confs_by_tools,
        default_tools=args.tools,
    )
    for e in errs:
        print(f"WARNING: {e}", file=sys.stderr)

    dag_label = args.dag_id or f"Hammer_{design}"
    if dep_graph:
        leaves = sorted(m for m, edges in dep_graph.items() if not edges[1])
        non_leaves = sorted(m for m, edges in dep_graph.items() if edges[1])
        print(f"\nHierarchical flow detected: {len(dep_graph)} modules.",
              file=sys.stderr)
        print(f"  non-leaf: {', '.join(non_leaves) or '(none)'}", file=sys.stderr)
        print(f"  leaves:   {', '.join(leaves) or '(none)'}", file=sys.stderr)
    else:
        print(f"\nFlat flow.", file=sys.stderr)

    tool_names = sorted(proj_confs_by_tools.keys())
    print(f"\nTool dropdown options (default {args.tools!r}): {tool_names}",
          file=sys.stderr)
    print(f"DAG for '{design}' generated. Airflow's dag-processor should",
          file=sys.stderr)
    print(f"register it within a few minutes as {dag_label}.", file=sys.stderr)
    return 0


def _cmd_workspace_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_user_workspaces(getattr(args, "username", None))
    if not rows:
        print("(no workspaces registered)")
        return 0
    print(f"{'username':<20} {'workspace':<16} {'workspace_root':<56} updated_at")
    print("-" * 120)
    for username, name, root, updated_at in rows:
        print(f"{username:<20} {name:<16} {root:<56} {updated_at}")
    return 0


def _cmd_workspace_show(args: argparse.Namespace) -> int:
    root = pd_store.get_user_workspace(args.username, args.name, auto_register=False)
    if root is None:
        print(f"No workspace '{args.name}' registered for '{args.username}'. "
              f"Set one: studio workspace-set {args.username} /path", file=sys.stderr)
        return 1
    print(root)
    return 0


def _cmd_workspace_set(args: argparse.Namespace) -> int:
    pd_store.set_user_workspace(args.username, args.workspace_root, args.name)
    print(f"Set workspace '{args.name}' for '{args.username}' -> {args.workspace_root}")
    return 0


def _cmd_workspace_unset(args: argparse.Namespace) -> int:
    name = getattr(args, "name", None)
    if pd_store.delete_user_workspace(args.username, name):
        if name:
            print(f"Removed workspace '{name}' for '{args.username}'.")
        else:
            print(f"Removed ALL workspace registrations for '{args.username}'.")
        return 0
    target = f"workspace '{name}'" if name else "any workspace"
    print(f"No {target} was registered for '{args.username}'.", file=sys.stderr)
    return 1


def _human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _human_dur(s: Optional[float]) -> str:
    if s is None:
        return "-"
    s = float(s)
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{int(s/60)}m{int(s%60):02d}s"
    return f"{int(s/3600)}h{int((s%3600)/60):02d}m"


def _print_blob_rows(rows) -> None:
    """Print pd_blobs rows (the shape returned by list_stage_blobs / find_blobs)."""
    header = (f"{'sha':<10} {'stage':<10} {'size':>8} {'wall':>10} {'cpu':>10} "
              f"{'owner':<14} {'trig_user':<14} {'design':<28} {'dag_id':<28}  created_at")
    print(header)
    print("-" * len(header))
    for r in rows:
        (sha, stage, size, dur_s, cpu_s, owner, trig, dag,
         design, workspace, created) = r
        print(f"{_short_str(sha,10):<10} {_short_str(stage,10):<10} {_human_bytes(size):>8} "
              f"{_human_dur(dur_s):>10} {_human_dur(cpu_s):>10} "
              f"{_short_str(owner,14):<14} "
              f"{_short_str(trig,14):<14} {_short_str(design,28):<28} "
              f"{_short_str(dag,28):<28}  {created}")


def _cmd_blob_list(args: argparse.Namespace) -> int:
    rows = pd_store.list_stage_blobs(stage_tag=args.stage, limit=args.limit)
    if not rows:
        print("(no blobs)")
        return 0
    _print_blob_rows(rows)
    return 0


# ---- Filter-based management: blob-find / blob-delete / blob-reassign ----

def _hex_prefix(value: str) -> str:
    """argparse type for --sha: an empty or wildcard prefix would match every blob."""
    if not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise argparse.ArgumentTypeError(f"expected hex digits, got {value!r}")
    return value


def _blob_filter_kwargs(args: argparse.Namespace) -> dict:
    """Pull the shared filter flags off ``args`` into pd_store filter kwargs."""
    return {
        "user": getattr(args, "user", None),
        "owner": getattr(args, "owner", None),
        "triggering_user": getattr(args, "triggering_user", None),
        "design": getattr(args, "design", None),
        "stage": getattr(args, "stage", None),
        "dag_id": getattr(args, "dag_id", None),
        "workspace": getattr(args, "workspace", None),
        "before": getattr(args, "before", None),
        "after": getattr(args, "after", None),
        "sha": getattr(args, "sha", None),
    }


def _cmd_blob_find(args: argparse.Namespace) -> int:
    kw = _blob_filter_kwargs(args)
    rows = pd_store.find_blobs(limit=args.limit, **kw)
    if not rows:
        print("(no matching blobs)")
        return 0
    _print_blob_rows(rows)
    n, total = pd_store.count_blobs(**kw)
    extra = "" if n <= len(rows) else f" (showing newest {len(rows)})"
    print(f"\n{n} blob(s) match, {_human_bytes(total)} total{extra}")
    return 0


def _cmd_blob_delete(args: argparse.Namespace) -> int:
    kw = _blob_filter_kwargs(args)
    if all(v is None for v in kw.values()):
        print("Refusing to delete with no filter. Pass --user/--design/--stage/"
              "--before/--after/--sha (or use wipe-blobs to clear everything).",
              file=sys.stderr)
        return 2
    n, total = pd_store.count_blobs(**kw)
    if n == 0:
        print("(no matching blobs; nothing to delete)")
        return 0
    print(f"Matched {n} blob(s), {_human_bytes(total)}:")
    _print_blob_rows(pd_store.find_blobs(limit=10, **kw))
    if n > 10:
        print(f"  ... and {n - 10} more")
    if args.dry_run:
        print(f"\n[dry-run] would delete {n} blob(s). Re-run without --dry-run to apply.")
        return 0
    if not _confirm(f"Permanently delete {n} blob(s) ({_human_bytes(total)})?", args.yes):
        print("Aborted.")
        return 1
    deleted = pd_store.delete_blobs(**kw)
    print(f"Deleted {deleted} blob(s).")
    return 0


def _cmd_blob_reassign(args: argparse.Namespace) -> int:
    kw = _blob_filter_kwargs(args)
    sets = {
        "set_owner": args.set_owner,
        "set_triggering_user": args.set_user,
        "set_design": args.set_design,
        "set_workspace": args.set_workspace,
    }
    if all(v is None for v in kw.values()):
        print("Refusing to update with no filter. Narrow it with --user/--design/etc.",
              file=sys.stderr)
        return 2
    if all(v is None for v in sets.values()):
        print("Nothing to set. Pass at least one of --set-owner/--set-user/"
              "--set-design/--set-workspace.", file=sys.stderr)
        return 2
    n, total = pd_store.count_blobs(**kw)
    if n == 0:
        print("(no matching blobs; nothing to update)")
        return 0
    changes = ", ".join(f"{k.replace('set_', '')}={v}"
                        for k, v in sets.items() if v is not None)
    print(f"Matched {n} blob(s). Will set: {changes}")
    _print_blob_rows(pd_store.find_blobs(limit=10, **kw))
    if n > 10:
        print(f"  ... and {n - 10} more")
    if args.dry_run:
        print(f"\n[dry-run] would update {n} blob(s). Re-run without --dry-run to apply.")
        return 0
    if not _confirm(f"Update {n} blob(s) ({changes})?", args.yes):
        print("Aborted.")
        return 1
    updated = pd_store.reassign_blobs(**sets, **kw)
    print(f"Updated {updated} blob(s).")
    return 0


# EDA tools a PD run launches; matched by process name for `studio reap`.
_EDA_TOOLS_DEFAULT = (
    "genus", "innovus", "tempus", "joules", "openroad",
    "vcs", "simv", "dc_shell", "pt_shell", "calibre",
    "magic", "netgen", "klayout",
)


def _fmt_dur(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _cmd_reap(args: argparse.Namespace) -> int:
    """List, and with --kill terminate, EDA-tool processes left over from dead or
    idle PD runs. Dry run by default. A process is flagged stale when it's been
    orphaned (reparented to init after its task died), or -- with --include-idle
    -- when it has sat near-idle past the threshold."""
    import os
    import time
    try:
        import psutil
    except ImportError:
        print("reap needs psutil.", file=sys.stderr)
        return 1

    tools = tuple(
        t.strip().lower()
        for t in (args.tools.split(",") if args.tools else _EDA_TOOLS_DEFAULT)
        if t.strip()
    )
    me = args.user or psutil.Process().username()
    idle_secs = max(0, args.idle_mins) * 60

    matched = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "username", "create_time", "cmdline"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            cmd = info.get("cmdline") or []
            base = os.path.basename(cmd[0]).lower() if cmd else ""
            if not any(name == t or base == t for t in tools):
                continue
            if me != "ALL" and (info.get("username") or "") != me:
                continue
            matched.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # prime then read CPU over one short window for the matched processes
    for p in matched:
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    if matched:
        time.sleep(0.4)

    rows = []
    for p in matched:
        try:
            info = p.info
            ppid = info.get("ppid")
            runtime = time.time() - (info.get("create_time") or time.time())
            try:
                cpu = p.cpu_percent(None)
            except Exception:
                cpu = 0.0
            try:
                cwd = p.cwd()
            except Exception:
                cwd = "?"
            orphaned = ppid == 1
            idle = args.include_idle and cpu < args.cpu and runtime > idle_secs
            reason = "orphaned" if orphaned else ("idle" if idle else "")
            rows.append({
                "pid": info.get("pid"), "ppid": ppid,
                "user": info.get("username") or "?",
                "runtime": runtime, "cpu": cpu, "reason": reason,
                "tool": (info.get("name") or "?"), "cwd": cwd,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not rows:
        print("No matching EDA-tool processes found.")
        return 0

    rows.sort(key=lambda r: (r["reason"] == "", -r["runtime"]))
    print(f"{'PID':>7} {'PPID':>7} {'USER':<12} {'RUNTIME':>8} {'CPU%':>6}  "
          f"{'STALE':<9} {'TOOL':<10} CWD")
    for r in rows:
        print(f"{r['pid']:>7} {r['ppid']:>7} {r['user']:<12} "
              f"{_fmt_dur(r['runtime']):>8} {r['cpu']:>6.1f}  "
              f"{(r['reason'] or '-'):<9} {r['tool'][:10]:<10} {r['cwd']}")

    targets = rows if args.all else [r for r in rows if r["reason"]]
    if not targets:
        print("\nNothing flagged stale. Use --include-idle to also flag idle "
              "processes, or --all to target everything listed.")
        return 0

    if not args.kill:
        print(f"\n(dry run) {len(targets)} process(es) would be killed. "
              f"Re-run with --kill to terminate them.")
        return 0

    if not args.yes:
        if input(f"\nKill {len(targets)} process(es)? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    procs = []
    for r in targets:
        try:
            procs.append(psutil.Process(r["pid"]))
        except psutil.NoSuchProcess:
            pass
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    print(f"Killed {len(procs)} process(es) ({len(alive)} needed SIGKILL).")
    return 0


def _cmd_time_saved(args: argparse.Namespace) -> int:
    """Total the PD cache's wall-clock / CPU time savings across runs.

    Same report as scripts/report_time_saved.py: sums every cache HIT's saved
    duration across runs (a whole tapeout / RTL bring-up), from the durable
    Postgres ledger and/or the on-disk JSONL event files.
    """
    import time as _time
    from datetime import datetime as _datetime
    from hammer.vlsi import time_tracking

    def _when(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _time.mktime(_datetime.strptime(s, fmt).timetuple())
            except ValueError:
                continue
        raise SystemExit(f"could not parse date/time {s!r}; use epoch, "
                         f"YYYY-MM-DD, or 'YYYY-MM-DD HH:MM'")

    events, source = time_tracking.collect_savings_events(
        source=args.source,
        since=_when(args.since), until=_when(args.until),
        dag=args.dag, design=args.design, stage=args.stage, user=args.user,
        project=args.project, module=args.module, limit=args.limit,
        events_dir=args.events_dir,
    )
    db_down = "DB unavailable" in source
    if db_down:
        print(f"WARNING: reporting from {source}", file=sys.stderr)
    if args.cache_only:
        events = time_tracking.exclude_depcheck_skips(events)
        source = f"{source}, cache-only"
    if args.csv:
        csv_text = time_tracking.savings_csv(events, group_by=args.group_by)
        if args.csv == "-":
            print(csv_text, end="")
        else:
            with open(args.csv, "w") as f:
                f.write(csv_text)
            print(f"Wrote {args.csv} ({len(csv_text.splitlines()) - 1} data row(s)).")
        return 1 if db_down and not events else 0
    print(time_tracking.format_savings_report(events, group_by=args.group_by, source=source))
    return 1 if db_down and not events else 0


def _cmd_project_set(args: argparse.Namespace) -> int:
    """Categorize ledger rows under a project (relabel the project column)."""
    import time as _time
    from datetime import datetime as _datetime
    from hammer.vlsi import pd_store

    def _when(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _time.mktime(_datetime.strptime(s, fmt).timetuple())
            except ValueError:
                continue
        raise SystemExit(f"could not parse date/time {s!r}")

    filters: Dict[str, object] = {}
    if args.dag:
        filters["dag"] = args.dag
    if args.design:
        filters["design"] = args.design
    if args.stage:
        filters["stage"] = args.stage
    if args.user:
        filters["user"] = args.user
    if args.after:
        filters["since"] = _when(args.after)
    if args.before:
        filters["until"] = _when(args.before)

    if not filters and not args.all:
        print("Refusing to relabel the whole ledger without --all (or pass a "
              "filter like --dag/--design/--stage/--after/--before).")
        return 1
    try:
        n = pd_store.count_cache_events(**filters)
    except Exception as e:
        print(f"error: ledger unreachable ({e})")
        return 1
    if n == 0:
        print("No matching ledger rows to relabel.")
        return 0
    scope = "ALL ledger rows" if not filters else f"{n} matching ledger row(s)"
    if not args.yes:
        resp = input(f"Set project='{args.project}' on {scope}? [y/N] ")
        if resp.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    updated = pd_store.set_cache_event_project(
        args.project, all_rows=not filters, **filters)
    print(f"Set project='{args.project}' on {updated} ledger row(s).")
    return 0


def _cmd_checkpoints(args: argparse.Namespace) -> int:
    """List stored sub-step checkpoints (broken/paused stages only)."""
    from hammer.vlsi import pd_store
    rows = pd_store.find_checkpoints(stage_key=args.key, design=args.design,
                                     stage=args.stage, limit=args.limit)
    if not rows:
        print("No checkpoints stored (they exist only while a stage is broken or paused).")
        return 0
    print(f"{'id':>5}  {'stage':<10} {'step':<24} {'size':>9}  {'design':<18} "
          f"{'module':<14} {'owner':<12} created")
    for r in rows:
        size = f"{(r['size_bytes'] or 0) / 1e6:.1f}M"
        print(f"{r['id']:>5}  {r['stage'] or '':<10} {r['step'] or '':<24} {size:>9}  "
              f"{(r['design'] or '-'):<18} {(r['module'] or '-'):<14} "
              f"{(r['owner'] or '-'):<12} {r['created_at']:%Y-%m-%d %H:%M}")
        if args.keys:
            print(f"       key={r['stage_key']}")
    return 0


def _cmd_checkpoints_push(args: argparse.Namespace) -> int:
    """Upload a local checkpoint from a rundir to the database by hand.

    The automatic push fires on stage failure; this covers the rest: banking
    partial progress before wiping a machine, or handing a paused run to a
    teammate. The stage key comes from the rundir's resume marker, so the
    rundir must have been run by resume-enabled hammer (or pass --key).
    """
    import os
    from pathlib import Path
    from hammer.vlsi import pd_store, substep_resume

    stage_tag, log_name = {"syn": ("synthesis", "genus.log"),
                           "par": ("par", "innovus.log")}[args.stage]
    key = args.key
    if key is None:
        marker = substep_resume.read_marker(args.rundir)
        key = marker.get("stage_key") if marker else None
    if not key:
        print("No resume marker in that rundir; pass --key explicitly.")
        return 1
    confirmed = substep_resume.confirmed_checkpoints(args.rundir, log_name)
    ceiling = substep_resume._RESUME_CEILING.get(log_name)
    if ceiling and ceiling in confirmed:
        confirmed = confirmed[: confirmed.index(ceiling) + 1]
    if not confirmed:
        print("No tool-confirmed checkpoints in that rundir; nothing trustworthy to push.")
        return 1
    step = args.step or confirmed[-1]
    if step not in confirmed:
        print(f"Step '{step}' is not a tool-confirmed checkpoint here. "
              f"Confirmed: {', '.join(confirmed)}.")
        return 1
    path = Path(args.rundir) / f"pre_{step}"
    size = pd_store.store_checkpoint(
        key, stage_tag, step, path,
        design=args.design or os.environ.get("HAMMER_AIRFLOW_DESIGN"),
        module=args.module, project=args.project,
        triggering_user=os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER"),
        workspace=os.environ.get("HAMMER_AIRFLOW_WORKSPACE"))
    print(f"Pushed pre_{step} ({size / 1e6:.1f} MB compressed) for stage "
          f"{stage_tag}. A rerun with matching inputs resumes from it; "
          f"see it with: studio checkpoints")
    return 0


def _cmd_checkpoints_fetch(args: argparse.Namespace) -> int:
    """Download one checkpoint by id into a rundir as pre_<step>.

    No stage-key check: fetching by id is an explicit choice, like naming a
    step with --from_step. Useful for continuing a teammate's failed run
    after a config change, where the automatic key-matched fetch won't fire.
    """
    from pathlib import Path
    from hammer.vlsi import pd_store
    rec = pd_store.fetch_checkpoint(ckpt_id=args.id)
    if rec is None:
        print(f"No checkpoint with id {args.id}.")
        return 1
    dest = pd_store.materialize_checkpoint(rec, Path(args.dest))
    print(f"Wrote {dest} ({rec['size_bytes'] / 1e6:.1f} MB compressed, "
          f"stage {rec['stage']}, step {rec['step']}).")
    print(f"Continue with: --from_step {rec['step']} (or the DAG From-step field).")
    return 0


def _cmd_checkpoints_clear(args: argparse.Namespace) -> int:
    """Delete stored checkpoints by id, key, design, or age."""
    from hammer.vlsi import pd_store
    if not any([args.id, args.key, args.design, args.older_than_days]):
        print("Pass --id/--key/--design/--older-than-days (no blanket wipe).")
        return 1
    n = pd_store.delete_checkpoints(
        stage_key=args.key, design=args.design,
        ids=[int(i) for i in args.id] if args.id else None,
        older_than_days=args.older_than_days)
    print(f"Deleted {n} checkpoint(s).")
    return 0


def _cmd_cache_status(args: argparse.Namespace) -> int:
    """Show whether the cache + time-saved ledger are on, and the ledger size."""
    import os
    from hammer.vlsi import pd_cache, pd_store, time_tracking

    cache_on = pd_cache.is_cache_enabled()
    ledger_on = time_tracking.is_ledger_enabled()
    print(f"PD cache         : {'ON' if cache_on else 'off'}   "
          f"(HAMMER_PD_CACHE={os.environ.get('HAMMER_PD_CACHE', '(unset -> off)')})")
    print(f"Time-saved ledger: {'ON' if ledger_on else 'off'}   "
          f"(HAMMER_PD_CACHE_LEDGER={os.environ.get('HAMMER_PD_CACHE_LEDGER', '(unset -> on)')})")
    print("  turn off: export HAMMER_PD_CACHE_LEDGER=0   turn on: export HAMMER_PD_CACHE_LEDGER=1")
    try:
        n = pd_store.count_cache_events()
        print(f"Durable ledger   : {n} event row(s) in {pd_store.FQ_CACHE_EVENT}")
    except Exception as e:
        print(f"Durable ledger   : unreachable ({e})")
        return 1
    return 0


def _cmd_cache_events_clear(args: argparse.Namespace) -> int:
    """Reset the durable time-saved ledger (delete rows; filters optional)."""
    import time as _time
    from datetime import datetime as _datetime
    from hammer.vlsi import pd_store

    def _when(s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return _time.mktime(_datetime.strptime(s, fmt).timetuple())
            except ValueError:
                continue
        raise SystemExit(f"could not parse date/time {s!r}")

    filters: Dict[str, object] = {}
    if args.dag:
        filters["dag"] = args.dag
    if args.design:
        filters["design"] = args.design
    if args.stage:
        filters["stage"] = args.stage
    if args.user:
        filters["user"] = args.user
    if args.after:
        filters["since"] = _when(args.after)
    if args.before:
        filters["until"] = _when(args.before)

    if not filters and not args.all:
        print("Refusing to wipe the whole ledger without --all (or pass a filter "
              "like --dag/--design/--stage/--after/--before).")
        return 1
    try:
        n = pd_store.count_cache_events(**filters)
    except Exception as e:
        print(f"error: ledger unreachable ({e})")
        return 1
    if n == 0:
        print("No matching ledger rows.")
        return 0
    scope = "ALL ledger rows" if not filters else f"{n} matching ledger row(s)"
    if not args.yes:
        resp = input(f"Delete {scope}? This cannot be undone. [y/N] ")
        if resp.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    deleted = pd_store.clear_cache_events(all_rows=not filters, **filters)
    print(f"Deleted {deleted} ledger row(s).")
    return 0


def _shared_dag_file(design: str, obj_dir: Optional[str]) -> str:
    """The DAG that builds the caller's design: <obj_dir>/hammer_dag.py, else the registered one."""
    from hammer.vlsi import design_share
    if obj_dir:
        path = os.path.join(os.path.abspath(obj_dir), "hammer_dag.py")
        if not os.path.isfile(path):
            raise design_share.ShareError(f"{obj_dir} has no hammer_dag.py; run make buildfile there first.")
        return path
    import getpass
    from hammer.shell.sledgehammer_cli import _dags_folder
    folder, src = _dags_folder()
    path = os.path.join(folder, f"sledgehammer_{design}_{getpass.getuser()}.py")
    if not os.path.isfile(path):
        raise design_share.ShareError(f"no DAG registered for {design} in {folder} (from {src}). Pass "
                                      f"--obj-dir <build dir>, or run make buildfile first.")
    return path


def _publish_design(design: str, obj_dir: Optional[str]):
    """Bundle the caller's design from its DAG and upload it. Returns (owner, bundle, uploaded)."""
    from hammer.vlsi import design_share
    bundle = design_share.bundle_from_dag(_shared_dag_file(design, obj_dir), owner=pd_store.current_db_user())
    if bundle.manifest["design"] != design:
        raise design_share.ShareError(f"that DAG builds {bundle.manifest['design']!r}, not {design!r}.")
    uploaded = pd_store.publish_design_bundle(design, bundle.manifest, bundle.data, bundle.bundle_sha)
    return bundle.manifest["owner"], bundle, uploaded


def _print_bundle(design: str, bundle, uploaded: bool) -> None:
    files, size = len(bundle.manifest["files"]), len(bundle.data)
    print(f"{'Published' if uploaded else 'Already up to date:'} {design}: {files} file(s), "
          f"{size / 1e6:.1f} MB compressed.")
    external = bundle.manifest["external"]
    if external:
        print(f"  {len(external)} path(s) outside your checkout (PDK, tools) are not copied; partners need them "
              f"at the same place.")


def _cmd_share(args: argparse.Namespace) -> int:
    from hammer.vlsi import design_share
    try:
        owner, bundle, uploaded = _publish_design(args.design, args.obj_dir)
        for partner in args.partners:
            pd_store.share_design(args.design, partner)
    except (design_share.ShareError, ValueError) as e:
        print(f"studio share: {e}", file=sys.stderr)
        return 2
    _print_bundle(args.design, bundle, uploaded)
    print(f"Shared {args.design} with {', '.join(args.partners)}. They run:  studio pull {owner} {args.design}")
    print("Your syn runs publish it again whenever its files change.")
    return 0


def _cmd_unshare(args: argparse.Namespace) -> int:
    n = pd_store.unshare_design(args.design, args.partner)
    if not n:
        print(f"{args.design} was not shared with {args.partner}.", file=sys.stderr)
        return 1
    print(f"{args.partner} can no longer pull {args.design}.")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    from hammer.vlsi import design_share
    try:
        _, bundle, uploaded = _publish_design(args.design, args.obj_dir)
    except design_share.ShareError as e:
        print(f"studio publish: {e}", file=sys.stderr)
        return 2
    _print_bundle(args.design, bundle, uploaded)
    return 0


def _cmd_shares(args: argparse.Namespace) -> int:
    me = pd_store.current_db_user()
    rows = pd_store.list_design_shares()
    mine = [r for r in rows if r["owner"] == me]
    theirs = [r for r in rows if r["owner"] != me]

    def updated(r) -> str:
        t = r["bundle_updated_at"]
        return t.strftime("%Y-%m-%d %H:%M") if hasattr(t, "strftime") else "not published"

    print("Shared by you:" if mine else "You have not shared any design.")
    for r in mine:
        print(f"  {r['design']:32s} with {r['grantee']:16s} bundle {updated(r)}")
    print("Shared with you:" if theirs else "Nobody has shared a design with you.")
    for r in theirs:
        print(f"  {r['design']:32s} from {r['owner']:16s} bundle {updated(r)}   studio pull {r['owner']} {r['design']}")
    return 0


def _cmd_pull(args: argparse.Namespace) -> int:
    from hammer.vlsi import design_share
    into = args.into or f"{args.design}-from-{args.owner}"
    try:
        summary = design_share.pull(args.owner, args.design, into, env_configs=args.env or None,
                                    force=args.force, build=not args.no_dag)
    except design_share.ShareError as e:
        print(f"studio pull: {e}", file=sys.stderr)
        return 2
    print(f"Pulled {args.owner}'s {args.design} into {summary['work_dir']} ({summary['files']} file(s)).")
    if summary["hammer_mismatch"]:
        print(f"WARNING: {summary['hammer_mismatch']}")
    missing = summary["missing_external"]
    if missing:
        print(f"WARNING: {len(missing)} path(s) the design uses outside {args.owner}'s checkout do not exist here, "
              f"so stages that read them will fail:")
        for p in missing[:10]:
            print(f"  {p}")
    if summary["dag_file"]:
        print(f"Generated your DAG {summary['dag_id']}. Once Airflow picks it up (about 30 s), run it from there:")
        print(f"  cd {summary['work_dir']}")
        print("  sledgehammer run syn par")
        print(f"syn and par restore {args.owner}'s cached results instead of running the tools when nothing differs.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="studio",
        description="SledgeHammer Studio: the admin CLI for the shared PD "
                    "database -- stage cache and blobs, checkpoints, the "
                    "time-saved ledger, users and access, workspaces, and "
                    "flow-completion notifications.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create schema + table if not present.")
    p_init.set_defaults(func=_cmd_init)

    p_list = sub.add_parser("list", help="List recent artifacts.")
    p_list.add_argument("-n", "--limit", type=int, default=20,
                        help="Number of rows to show (default: 20).")
    p_list.set_defaults(func=_cmd_list)

    p_get = sub.add_parser("get", help="Fetch an artifact by SHA256 and print its JSON.")
    p_get.add_argument("sha256", help="SHA256 hex digest of the artifact.")
    p_get.set_defaults(func=_cmd_get)

    p_put = sub.add_parser("put", help="Store a JSON file as an artifact.")
    p_put.add_argument("path", help="Path to the JSON file to store.")
    p_put.add_argument("--kind", default="par-input",
                       help="Artifact kind label (default: par-input).")
    p_put.set_defaults(func=_cmd_put)

    p_mpush = sub.add_parser("master-push", help="Upsert a master_database for a design.")
    p_mpush.add_argument("design", help="Design name to use as the row key.")
    p_mpush.add_argument("--master", default=None,
                         help="Path to master_database.json (default: ./master_database.json).")
    p_mpush.set_defaults(func=_cmd_master_push)

    p_mpull = sub.add_parser("master-pull", help="Fetch a master_database by design.")
    p_mpull.add_argument("design")
    p_mpull.add_argument("--out", default=None,
                         help="Write JSON to this path. Default: stdout.")
    p_mpull.set_defaults(func=_cmd_master_pull)

    p_mlist = sub.add_parser(
        "master-list",
        help="List master_database rows with provenance (design, owner, "
             "triggering_user, dag_id, workspace, updated_at). One row per "
             "design.",
    )
    p_mlist.add_argument("-n", "--limit", type=int, default=50)
    p_mlist.set_defaults(func=_cmd_master_list)

    p_skey = sub.add_parser("stage-key", help="Compute a stage's cache key from a master_database.")
    p_skey.add_argument("stage", choices=pd_store.KNOWN_STAGE_TAGS,
                        help="Stage tag (e.g. 'synthesis', 'par').")
    p_skey.add_argument("--master", default=None,
                        help="Path to master_database.json (default: ./master_database.json).")
    p_skey.set_defaults(func=_cmd_stage_key)

    p_spush = sub.add_parser("stage-push", help="Tar a stage rundir and store it under its cache key.")
    p_spush.add_argument("stage", choices=pd_store.KNOWN_STAGE_TAGS)
    p_spush.add_argument("--rundir", required=True,
                         help="Path to the stage's run directory (e.g. obj_dir/syn-rundir).")
    p_spush.add_argument("--master", default=None,
                         help="Path to master_database.json (default: ./master_database.json).")
    p_spush.set_defaults(func=_cmd_stage_push)

    p_spull = sub.add_parser("stage-pull", help="Fetch a stage tarball by cache key and untar it.")
    p_spull.add_argument("stage", choices=pd_store.KNOWN_STAGE_TAGS)
    p_spull.add_argument("--rundir", required=True,
                         help="Where to extract (e.g. obj_dir/syn-rundir).")
    p_spull.add_argument("--master", default=None,
                         help="Path to master_database.json (default: ./master_database.json).")
    p_spull.add_argument("--overwrite", action="store_true",
                         help="Replace --rundir if it already exists.")
    p_spull.set_defaults(func=_cmd_stage_pull)

    p_blist = sub.add_parser("blob-list", help="List stored stage tarballs.")
    p_blist.add_argument("--stage", default=None, choices=pd_store.KNOWN_STAGE_TAGS)
    p_blist.add_argument("-n", "--limit", type=int, default=20)
    p_blist.set_defaults(func=_cmd_blob_list)

    def _add_blob_filters(p: argparse.ArgumentParser) -> None:
        """Shared filter flags for blob-find / blob-delete / blob-reassign."""
        g = p.add_argument_group("filters (ANDed together)")
        g.add_argument("--user", help="match owner OR triggering_user (everything from this person)")
        g.add_argument("--owner", help="exact Postgres role that stored the blob")
        g.add_argument("--triggering-user", dest="triggering_user",
                       help="exact Airflow user who triggered the run")
        g.add_argument("--design", help="design name")
        g.add_argument("--stage", help="stage tag (synthesis, par, drc, lvs, ...)")
        g.add_argument("--dag-id", dest="dag_id", help="Airflow dag_id")
        g.add_argument("--workspace", help="workspace root")
        g.add_argument("--before", metavar="DATE",
                       help="created_at < DATE (e.g. 2026-06-01 or 2026-06-01T12:00)")
        g.add_argument("--after", metavar="DATE", help="created_at >= DATE")
        g.add_argument("--sha", type=_hex_prefix, help="sha256 prefix (hex digits)")

    p_bfind = sub.add_parser(
        "blob-find",
        help="List stage blobs filtered by user / design / stage / date / etc.",
    )
    _add_blob_filters(p_bfind)
    p_bfind.add_argument("-n", "--limit", type=int, default=50,
                         help="Max rows to print (default 50).")
    p_bfind.set_defaults(func=_cmd_blob_find)

    p_bdel = sub.add_parser(
        "blob-delete",
        help="Delete stage blobs matching filters (user/design/stage/date/...). "
             "Refuses to run with no filter; prompts unless --yes.",
    )
    _add_blob_filters(p_bdel)
    p_bdel.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted, don't delete.")
    p_bdel.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    p_bdel.set_defaults(func=_cmd_blob_delete)

    p_brea = sub.add_parser(
        "blob-reassign",
        help="Move/retag blobs: set owner/user/design/workspace on matching rows.",
    )
    _add_blob_filters(p_brea)
    s = p_brea.add_argument_group("updates (at least one required)")
    s.add_argument("--set-owner", dest="set_owner", help="new owner (Postgres role)")
    s.add_argument("--set-user", dest="set_user", help="new triggering_user")
    s.add_argument("--set-design", dest="set_design", help="new design name")
    s.add_argument("--set-workspace", dest="set_workspace", help="new workspace root")
    p_brea.add_argument("--dry-run", action="store_true",
                        help="Show what would change, don't update.")
    p_brea.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    p_brea.set_defaults(func=_cmd_blob_reassign)

    p_onboard = sub.add_parser("onboard",
        help="Set up a new teammate: create their airflow_<user> metadata DB "
             "and grant hammer_poc access.")
    p_onboard.add_argument("role", help="Their Postgres role / EECS uid (e.g. 'desvaun').")
    p_onboard.set_defaults(func=_cmd_onboard)

    p_grant = sub.add_parser("grant",
                             help=f"Add a role to the {pd_store.SLEDGEHAMMER_GROUP} group.")
    p_grant.add_argument("role", help="Postgres role name (e.g. 'colin').")
    p_grant.set_defaults(func=_cmd_grant)

    p_revoke = sub.add_parser("revoke",
                              help=f"Remove a role from the {pd_store.SLEDGEHAMMER_GROUP} group.")
    p_revoke.add_argument("role")
    p_revoke.set_defaults(func=_cmd_revoke)

    p_ws_list = sub.add_parser(
        "workspace-list",
        help="List registered workspaces (all users, or just one user's).",
    )
    p_ws_list.add_argument(
        "username", nargs="?", default=None,
        help="Optional: list only this user's workspaces.",
    )
    p_ws_list.set_defaults(func=_cmd_workspace_list)

    p_ws_show = sub.add_parser(
        "workspace-show",
        help="Print a user's workspace root (read-only).",
    )
    p_ws_show.add_argument("username", help="Airflow username.")
    p_ws_show.add_argument(
        "--name", default="default",
        help="Workspace name to resolve (default: 'default').",
    )
    p_ws_show.set_defaults(func=_cmd_workspace_show)


    p_ne = sub.add_parser(
        "notify-email",
        help="Show or set the flow-completion email for a user (no args: list all).",
    )
    p_ne.add_argument("uid", nargs="?", help="Username (default: list every registration).")
    p_ne.add_argument("address", nargs="?", help="Email address to register for uid.")
    p_ne.set_defaults(func=_cmd_notify_email)

    p_ss = sub.add_parser(
        "smtp-setup",
        help="Configure the SMTP sender for completion emails (writes ~/.sledgehammer/smtp.env).",
    )
    p_ss.add_argument("--user", default=os.environ.get("SLEDGEHAMMER_SMTP_USER", ""),
                      help="SMTP account / sender address "
                           "(default: $SLEDGEHAMMER_SMTP_USER).")
    p_ss.add_argument("--host", default="smtp.gmail.com", help="SMTP host (default: smtp.gmail.com).")
    p_ss.add_argument("--port", default="587", help="SMTP port (default: 587).")
    p_ss.add_argument("--sender", help="From address if different from --user.")
    p_ss.add_argument("--password-file",
                      help="Existing password file to reference instead of prompting.")
    p_ss.add_argument("--password-stdin", action="store_true",
                      help="Read the password from stdin instead of prompting.")
    p_ss.set_defaults(func=_cmd_smtp_setup)

    p_nt = sub.add_parser(
        "notify-test",
        help="Send a test email through the real notification path.",
    )
    p_nt.add_argument("--to", help="Recipient (default: your registered notify email).")
    p_nt.set_defaults(func=_cmd_notify_test)

    p_ws_set = sub.add_parser(
        "workspace-set",
        help="Set or update a named workspace root for a user.",
    )
    p_ws_set.add_argument("username", help="Airflow username.")
    p_ws_set.add_argument(
        "workspace_root",
        help="Absolute path to the workspace root. The Airflow daemon "
             "user must have write permission here.",
    )
    p_ws_set.add_argument(
        "--name", default="default",
        help="Workspace name (default: 'default'). Use distinct names to "
             "register multiple workspaces a user can run in concurrently.",
    )
    p_ws_set.set_defaults(func=_cmd_workspace_set)

    p_ws_unset = sub.add_parser(
        "workspace-unset",
        help="Remove a user's workspace registration(s). With --name removes "
             "just that one; without it removes ALL of the user's workspaces. "
             "The user stays unregistered until a new `studio workspace-set`.",
    )
    p_ws_unset.add_argument("username")
    p_ws_unset.add_argument(
        "--name", default=None,
        help="Workspace name to remove. Omit to remove ALL of the user's.",
    )
    p_ws_unset.set_defaults(func=_cmd_workspace_unset)

    # ---- destructive cache-wiping subcommands ----
    p_wb = sub.add_parser(
        "wipe-blobs",
        help="Delete stage tarballs from pd_blobs. Forces a fresh cold run "
             "the next time a stage is invoked. Requires --yes or interactive "
             "confirmation.",
    )
    p_wb.add_argument(
        "--stage",
        choices=pd_store.KNOWN_STAGE_TAGS,
        help="Only delete blobs for this stage (default: ALL stages).",
    )
    p_wb.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")
    p_wb.set_defaults(func=_cmd_wipe_blobs)

    p_wm = sub.add_parser(
        "wipe-master",
        help="Delete master_databases rows. Forces stage_change_check to say "
             "'run' the next time (no baseline to compare against).",
    )
    p_wm.add_argument(
        "--design",
        help="Only delete the row for this design (default: ALL designs).",
    )
    p_wm.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")
    p_wm.set_defaults(func=_cmd_wipe_master)

    p_wa = sub.add_parser(
        "wipe-artifacts",
        help="Delete JSON artifacts from pd_artifacts (par-input, etc.).",
    )
    p_wa.add_argument(
        "--kind",
        help="Only delete artifacts of this kind (default: ALL kinds).",
    )
    p_wa.add_argument("--yes", action="store_true",
                      help="Skip the confirmation prompt.")
    p_wa.set_defaults(func=_cmd_wipe_artifacts)

    p_wall = sub.add_parser(
        "wipe-all",
        help="Nuclear: delete pd_blobs + master_databases + pd_artifacts. "
             "Preserves user_workspaces (that's config, not cache).",
    )
    p_wall.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    p_wall.set_defaults(func=_cmd_wipe_all)

    p_dreg = sub.add_parser(
        "design-register",
        help="Walk a directory of RTL and write Hammer configs for the "
             "design: common.yml, syn.yml, sky130.yml, par.yml, plus a "
             "blackbox stub for every sram22 macro the RTL instantiates. "
             "The sram22 LEF/lib/GDS paths get wired into sky130.yml so "
             "Innovus can find them at place_inst time.",
    )
    p_dreg.add_argument("--name", required=True,
                        help="Design name. The configs end up at "
                             "e2e/configs-design/<name>/.")
    p_dreg.add_argument("--top-module", required=True,
                        help="Top Verilog module. Make sure this matches the "
                             "module name in the RTL, not the file name.")
    p_dreg.add_argument("--clock-ns", required=True, type=float,
                        help="Target clock period in nanoseconds (e.g. 11.8 "
                             "for ~85 MHz).")
    p_dreg.add_argument("--rtl", nargs="+", required=True,
                        help="One or more RTL files or directories. Directories "
                             "get walked recursively for .v and .sv files.")
    p_dreg.add_argument("--pdk", default="sky130", choices=["sky130"],
                        help="PDK to target. sky130 is the only one wired up "
                             "today; asap7 and techname are on the to-do list.")
    p_dreg.add_argument("--out-dir", default=None,
                        help="Where to write the configs. Defaults to "
                             "./e2e/configs-design/<name>.")
    p_dreg.add_argument("--exclude", action="append", default=[],
                        help="Glob pattern to skip when walking the RTL "
                             "directory. Repeatable. Stacks on top of the "
                             "built-in pattern list (testbenches, copies, "
                             "etc.); use --no-default-excludes to turn that "
                             "list off.")
    p_dreg.add_argument("--no-default-excludes", action="store_true",
                        help="Turn off the built-in exclude list. Use this if "
                             "your design genuinely needs files matching "
                             "*Testbench.v, *_tb.v, etc. (uncommon).")
    p_dreg.set_defaults(func=_cmd_design_register)

    p_aug = sub.add_parser(
        "augment",
        help="Look at an already-configured design (configs-design/<name>/ "
             "with common.yml etc. in place) and fill in just the SRAM "
             "pieces: blackbox stubs + sky130-extras.yml with extra_libraries. "
             "Doesn't touch the user's other configs. Use this when you've "
             "written your own syn/par/sky130 yml by hand and just want the "
             "macro plumbing automated.",
    )
    p_aug.add_argument("--design", required=True,
                       help="Design name (looks under e2e/configs-design/<name>/).")
    p_aug.add_argument("--design-dir", default=None,
                       help="Override the design directory path.")
    p_aug.set_defaults(func=_cmd_augment)

    p_share = sub.add_parser(
        "share", help="Publish your design's files and let these partners pull them.")
    p_share.add_argument("design", help="Design name: your build dir's basename, as in sledgehammer_<design>_<you>.")
    p_share.add_argument("partners", nargs="+", help="Each partner's Postgres role (their username).")
    p_share.add_argument("--obj-dir", default=None,
                         help="Build dir whose hammer_dag.py to share (default: your registered DAG).")
    p_share.set_defaults(func=_cmd_share)

    p_unshare = sub.add_parser("unshare", help="Stop a partner pulling one of your designs.")
    p_unshare.add_argument("design")
    p_unshare.add_argument("partner")
    p_unshare.set_defaults(func=_cmd_unshare)

    p_publish = sub.add_parser(
        "publish", help="Upload your shared design's current files again (each syn run does this itself).")
    p_publish.add_argument("design")
    p_publish.add_argument("--obj-dir", default=None,
                           help="Build dir whose hammer_dag.py to publish (default: your registered DAG).")
    p_publish.set_defaults(func=_cmd_publish)

    p_shares = sub.add_parser("shares", help="List designs you shared and designs shared with you.")
    p_shares.set_defaults(func=_cmd_shares)

    p_pull = sub.add_parser(
        "pull", help="Fetch a design a partner shared with you and generate your own DAG for it.")
    p_pull.add_argument("owner", help="The partner who shared it (their Postgres role).")
    p_pull.add_argument("design")
    p_pull.add_argument("--into", default=None,
                        help="Directory to build it in (default: ./<design>-from-<owner>). Pulling again "
                             "updates it to the partner's latest.")
    p_pull.add_argument("--env", action="append", default=[],
                        help="Use this env config instead of the partner's (repeatable). Settings that differ "
                             "change the cache keys, so expect misses.")
    p_pull.add_argument("--force", action="store_true",
                        help="Write into a non-empty directory that no earlier pull made.")
    p_pull.add_argument("--no-dag", action="store_true", help="Fetch the files only; do not generate a DAG.")
    p_pull.set_defaults(func=_cmd_pull)

    p_dag = sub.add_parser(
        "make-dag",
        help="Generate an Airflow DAG for an existing design and symlink it "
             "into the dags/ folder. Wraps hammer-vlsi build with "
             "vlsi.core.build_system=airflow. Within a few minutes the DAG "
             "appears as Hammer_<design> in the Airflow UI.",
    )
    p_dag.add_argument("--design", required=True,
                       help="Design name (configs-design/<name>/).")
    p_dag.add_argument("--design-dir", default=None,
                       help="Override the design directory path.")
    p_dag.add_argument("--repo-root", default=None,
                       help="Hammer repo root (defaults to CWD).")
    p_dag.add_argument("--obj-dir", default=None,
                       help="Where to write the generated DAG. "
                            "Defaults to build-<pdk>-<tools>/<design> under e2e/.")
    p_dag.add_argument("--dags-folder", default=None,
                       help="Where to symlink the DAG. Defaults to <repo>/dags.")
    p_dag.add_argument("--pdk", default="sky130")
    p_dag.add_argument("--tools", default="cm")
    p_dag.add_argument("--env", default="bwrc")
    p_dag.add_argument("--dag-id", default=None,
                       help="DAG ID to use in Airflow. Defaults to "
                            "Hammer_<design>. Pick whatever name you want "
                            "shown in the Airflow UI.")
    p_dag.add_argument("--skip-augment", action="store_true",
                       help="Don't run the augment step first. By default "
                            "make-dag runs augment to keep blackbox stubs "
                            "and sky130-extras.yml up to date before "
                            "generating the DAG.")
    p_dag.set_defaults(func=_cmd_make_dag)

    p_reap = sub.add_parser(
        "reap",
        help="List (and with --kill, terminate) EDA-tool processes left by dead "
             "or idle PD runs. Dry run by default.",
    )
    p_reap.add_argument("--kill", action="store_true",
                        help="Terminate the flagged processes (default: just list them).")
    p_reap.add_argument("--all", action="store_true",
                        help="Target every matched EDA process, not just the stale ones.")
    p_reap.add_argument("--include-idle", action="store_true",
                        help="Also flag long-running near-idle processes, not just orphaned ones.")
    p_reap.add_argument("--idle-mins", type=int, default=30,
                        help="Runtime, in minutes, before an idle process counts (default 30).")
    p_reap.add_argument("--cpu", type=float, default=1.0,
                        help="CPU%% below which a long-running process counts as idle (default 1.0).")
    p_reap.add_argument("--user", default=None,
                        help="Only your processes by default; pass a username, or ALL for everyone.")
    p_reap.add_argument("--tools", default=None,
                        help="Comma-separated tool names to match (default: the standard EDA set).")
    p_reap.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt when killing.")
    p_reap.set_defaults(func=_cmd_reap)

    p_saved = sub.add_parser(
        "time-saved",
        help="Total the PD cache's wall-clock / CPU time saved across runs.")
    p_saved.add_argument("--source", choices=["auto", "db", "jsonl", "both"],
                         default="auto",
                         help="Event source (default: auto = DB, fall back to JSONL).")
    p_saved.add_argument("-g", "--group-by", default="stage",
                         choices=["stage", "dag", "design", "project", "module", "run", "none"],
                         help="Break the report down by this dimension (default: stage).")
    p_saved.add_argument("--since", help="Only events at/after this time (epoch or YYYY-MM-DD).")
    p_saved.add_argument("--until", help="Only events at/before this time (epoch or YYYY-MM-DD).")
    p_saved.add_argument("--dag", help="Filter to dag_id containing this substring.")
    p_saved.add_argument("--design", help="Filter to design containing this substring.")
    p_saved.add_argument("--stage", help="Filter to stage (e.g. synthesis, par).")
    p_saved.add_argument("--user", help="Filter to triggering_user / owner substring.")
    p_saved.add_argument("--project", help="Filter to project containing this substring.")
    p_saved.add_argument("--module", help="Filter to module containing this substring (hierarchical flows).")
    p_saved.add_argument("--cache-only", action="store_true",
                         help="Count only cache-delivered savings (exclude dependency-check "
                              "skips, which a legacy make flow may also have skipped).")
    p_saved.add_argument("--events-dir", help="Override JSONL dir (default $AIRFLOW_HOME/cache_events).")
    p_saved.add_argument("--limit", type=int, default=None, help="Max DB rows to read.")
    p_saved.add_argument("--csv", metavar="PATH",
                         help="Write the 8-column TAT breakdown (dep management / caching / "
                              "checkpointing, wall + compute, plus totals) as CSV. '-' for stdout.")
    p_saved.set_defaults(func=_cmd_time_saved)

    p_pset = sub.add_parser(
        "project-set",
        help="Categorize ledger rows under a project (relabel; filters or --all).")
    p_pset.add_argument("project", help="Project name to assign (e.g. ee290_tapeout).")
    p_pset.add_argument("--all", action="store_true",
                        help="Relabel every row (required to set with no filter).")
    p_pset.add_argument("--dag", help="Only rows with dag_id containing this substring.")
    p_pset.add_argument("--design", help="Only rows with design containing this substring.")
    p_pset.add_argument("--stage", help="Only rows for this stage (e.g. synthesis, par).")
    p_pset.add_argument("--user", help="Only rows for this triggering_user / owner.")
    p_pset.add_argument("--after", help="Only rows at/after this time (epoch or YYYY-MM-DD).")
    p_pset.add_argument("--before", help="Only rows at/before this time (epoch or YYYY-MM-DD).")
    p_pset.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    p_pset.set_defaults(func=_cmd_project_set)

    p_ckpt = sub.add_parser(
        "checkpoints",
        help="List sub-step checkpoints stored in the database (broken/paused stages).")
    p_ckpt.add_argument("--key", help="Exact stage key.")
    p_ckpt.add_argument("--design", help="Only this design.")
    p_ckpt.add_argument("--stage", help="Only this stage (synthesis, par).")
    p_ckpt.add_argument("--limit", type=int, default=50)
    p_ckpt.add_argument("--keys", action="store_true", help="Also print stage keys.")
    p_ckpt.set_defaults(func=_cmd_checkpoints)

    p_ckp = sub.add_parser(
        "checkpoints-push",
        help="Upload a local rundir checkpoint to the database by hand.")
    p_ckp.add_argument("--rundir", required=True, help="syn-rundir / par-rundir path.")
    p_ckp.add_argument("--stage", required=True, choices=["syn", "par"])
    p_ckp.add_argument("--step", help="Which pre_<step> (default: newest confirmed).")
    p_ckp.add_argument("--key", help="Stage key override if the rundir has no marker.")
    p_ckp.add_argument("--design", help="Design tag (default: $HAMMER_AIRFLOW_DESIGN).")
    p_ckp.add_argument("--module", help="Module tag for hierarchical flows.")
    p_ckp.add_argument("--project", help="Project tag for the tapeout grouping.")
    p_ckp.set_defaults(func=_cmd_checkpoints_push)

    p_ckf = sub.add_parser(
        "checkpoints-fetch",
        help="Download one checkpoint by id into a rundir (no key check; explicit choice).")
    p_ckf.add_argument("--id", type=int, required=True, help="Checkpoint id (see checkpoints).")
    p_ckf.add_argument("--dest", required=True, help="Rundir to write pre_<step> into.")
    p_ckf.set_defaults(func=_cmd_checkpoints_fetch)

    p_ckclr = sub.add_parser(
        "checkpoints-clear",
        help="Delete stored sub-step checkpoints by --id/--key/--design/--older-than-days.")
    p_ckclr.add_argument("--id", action="append", help="Checkpoint id (repeatable).")
    p_ckclr.add_argument("--key", help="Exact stage key.")
    p_ckclr.add_argument("--design", help="All checkpoints for this design.")
    p_ckclr.add_argument("--older-than-days", type=float,
                         help="Only checkpoints older than this many days.")
    p_ckclr.set_defaults(func=_cmd_checkpoints_clear)

    p_cstat = sub.add_parser(
        "cache-status",
        help="Show whether the PD cache + time-saved ledger are on, and ledger size.")
    p_cstat.set_defaults(func=_cmd_cache_status)

    p_cclr = sub.add_parser(
        "cache-events-clear",
        help="Reset the durable time-saved ledger (delete rows; --all or filters).")
    p_cclr.add_argument("--all", action="store_true",
                        help="Delete every row (required to wipe with no filter).")
    p_cclr.add_argument("--dag", help="Only rows with dag_id containing this substring.")
    p_cclr.add_argument("--design", help="Only rows with design containing this substring.")
    p_cclr.add_argument("--stage", help="Only rows for this stage (e.g. synthesis, par).")
    p_cclr.add_argument("--user", help="Only rows for this triggering_user / owner.")
    p_cclr.add_argument("--after", help="Only rows at/after this time (epoch or YYYY-MM-DD).")
    p_cclr.add_argument("--before", help="Only rows at/before this time (epoch or YYYY-MM-DD).")
    p_cclr.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    p_cclr.set_defaults(func=_cmd_cache_events_clear)

    return parser




def _smtp_env_path() -> str:
    return os.path.expanduser("~/.sledgehammer/smtp.env")


def _load_smtp_env() -> None:
    """Load SLEDGE_SMTP_* from the smtp.env file when the shell hasn't."""
    if os.environ.get("SLEDGE_SMTP_USER"):
        return
    path = _smtp_env_path()
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" in line and line.split("=", 1)[0].startswith("SLEDGE_SMTP_"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip('"'))


def _cmd_notify_email(args: argparse.Namespace) -> int:
    """Show or set the flow-completion email address for a user."""
    if args.address:
        pd_store.set_notify_email(args.uid, args.address)
        print(f"notify email for {args.uid} -> {args.address}")
        return 0
    if args.uid:
        addr = pd_store.get_notify_email(args.uid)
        print(f"{args.uid}: {addr or '(none registered)'}")
        return 0
    with pd_store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT uid, email FROM {pd_store.FQ_NOTIFY_EMAIL} ORDER BY uid")
            rows = cur.fetchall()
    if not rows:
        print("no notify emails registered")
    for uid, email in rows:
        print(f"{uid}: {email}")
    return 0


def _cmd_smtp_setup(args: argparse.Namespace) -> int:
    """Write the SMTP sender config + password file, CLI only, no UI."""
    import getpass
    envd = os.path.dirname(_smtp_env_path())
    os.makedirs(envd, exist_ok=True)
    os.chmod(envd, 0o700)

    pw_file = args.password_file
    if not pw_file:
        if args.password_stdin:
            pw = sys.stdin.readline().strip()
        else:
            print(f"Enter the SMTP password for {args.user} at the prompt below.")
            print("(input is hidden -- type or paste it, then press Enter)")
            pw = getpass.getpass("SMTP password: ")
        if not pw:
            print("ERROR: empty password given; nothing written. Re-run and "
                  "type the app password at the prompt, or pipe it in with "
                  "--password-stdin.", file=sys.stderr)
            return 2
        pw_file = os.path.join(envd, ".smtp_password")
        fd = os.open(pw_file, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(pw + "\n")

    lines = [
        f'export SLEDGE_SMTP_USER="{args.user}"',
        f'export SLEDGE_SMTP_HOST="{args.host}"',
        f'export SLEDGE_SMTP_PORT="{args.port}"',
        f'export SLEDGE_SMTP_FROM="{args.sender or args.user}"',
        f'export SLEDGE_SMTP_PASSWORD_FILE="{pw_file}"',
    ]
    path = _smtp_env_path()
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {path} (0600); password file: {pw_file}")
    print("verify with:  studio notify-test")
    return 0


def _cmd_notify_test(args: argparse.Namespace) -> int:
    """Send a test completion email through the real pd_notify path."""
    import getpass
    _load_smtp_env()
    to = args.to
    if not to:
        uid = getpass.getuser()
        to = pd_store.get_notify_email(uid)
        if not to:
            print(f"no --to given and no notify email registered for {uid}; "
                  f"register one:  studio notify-email {uid} you@example.com",
                  file=sys.stderr)
            return 2
    if not (os.environ.get("SLEDGE_SMTP_USER") and os.environ.get("SLEDGE_SMTP_PASSWORD_FILE")):
        print("no SMTP sender configured; run:  studio smtp-setup --user <sender-address>",
              file=sys.stderr)
        return 2
    from hammer.vlsi.pd_notify import _send_completion_email
    try:
        sent = _send_completion_email(
            to, "[SledgeHammer] test notification",
            "<p>This is a test of the flow-completion email path. "
            "If you are reading it, notifications work.</p>")
    except Exception as e:
        print(f"test email to {to} failed: {e}", file=sys.stderr)
        return 1
    if not sent:
        return 2
    print(f"test email sent to {to}: the SMTP server accepted it, so check the inbox")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # stderr only: scripts parse the stdout of stage-key and workspace-show
    from hammer.shell.sledgehammer_cli import _load_stack_env
    loaded = _load_stack_env()
    if loaded:
        print(f"[studio] stack env from {loaded}", file=sys.stderr)
    db_errors = (pd_store.DatabaseUnavailable,) + (
        (pd_store.psycopg2.OperationalError,) if pd_store.psycopg2 else ())
    try:
        return args.func(args)
    except db_errors as e:
        # a server-side error (timeout, deadlock, disk full) carries a SQLSTATE; keep its traceback
        if getattr(e, "pgcode", None):
            raise
        lines = [line.strip() for line in str(e).splitlines() if line.strip()] or [type(e).__name__]
        print(f"studio: database unavailable: {lines[0]}", file=sys.stderr)
        for line in lines[1:]:
            print(f"  {line}", file=sys.stderr)
        return 1
    except Exception as e:
        if getattr(e, "pgcode", None) != "42P01":
            raise
        # undefined_table: this Hammer knows tables (design sharing, say) the database was never given
        table = re.search(r'"([^"]+)"', str(e))
        print(f"studio: the database has no {table.group(1) if table else 'such'} table yet. A database admin "
              f"runs `studio init` once after upgrading Hammer to create it.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
