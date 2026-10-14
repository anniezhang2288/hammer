"""Design sharing: package a design so a partner can build it in a directory of their own and hit its cache.

A bundle holds what a build reads from the owner's checkout: the env and project configs the owner's
DAG runs with, the driver script, and every file or directory those configs name under the working
directory (where make and the DAG run) or directly in the build dir. Paths are stored relative to those
two roots. Anything outside them -- the PDK, tool installs, site-wide configs -- is left out and must
exist at the same absolute path on the partner's machine (BWRC shares one filesystem); the manifest
lists those paths so a pull can say which are missing.

The cache keys name files under the working directory by a portable token (fingerprints.make_roots), so
a partner who pulls the bundle into a directory of their own and builds from there computes the owner's
keys: syn and par restore the owner's results from the PD cache instead of running the tools.

The database decides who may pull what (pd_store's row-level security on the bundle and share tables);
this module only packs, unpacks and rebuilds.
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import getpass
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from hammer.vlsi import fingerprints

BUNDLE_VERSION = 1
STATE_FILE = ".sledgehammer-pull.json"
BUILD_CONFIG = ".sledgehammer-pull-build.yml"
MAX_BUNDLE_FILES = 20000
MAX_BUNDLE_BYTES = 4 * 1024 ** 3
_TOKEN_SPLIT_RE = re.compile(fingerprints.PATH_SEPARATORS)
_DAG_CONSTANTS = ("HAMMER_EXEC", "WORK_DIR", "OBJ_DIR", "ENV_CONFIGS", "PROJ_CONFIGS", "DESIGN_NAME",
                  "PROJ_CONFS_BY_TOOLS", "DEFAULT_TOOLS")


class ShareError(RuntimeError):
    """A design cannot be bundled, pulled or rebuilt; the message says why and what to do."""


# --- paths ---------------------------------------------------------------------------------------------

def _forms(path: str) -> Tuple[str, ...]:
    return tuple(dict.fromkeys((os.path.normpath(os.path.abspath(path)), os.path.realpath(path))))


def _relative(path: str, root: str) -> Optional[str]:
    """path relative to root ('' for root itself) if it is inside it in either normpath or realpath form."""
    for p in _forms(path):
        for r in _forms(root):
            if p == r:
                return ""
            if p.startswith(r.rstrip(os.sep) + os.sep):
                return p[len(r.rstrip(os.sep)) + 1:].replace(os.sep, "/")
    return None


def _excluded_roots() -> List[str]:
    """Trees a bundle never copies: the hammer package and the Python environment running it."""
    return [fingerprints.hammer_dir(), sys.prefix]


# --- reading what the owner builds ---------------------------------------------------------------------

def read_dag(dag_file: str) -> Dict[str, Any]:
    """The settings a generated DAG bakes in (HAMMER_EXEC, WORK_DIR, OBJ_DIR, the configs, ...), read
    without importing it."""
    try:
        tree = ast.parse(Path(dag_file).read_text(), filename=dag_file)
    except (OSError, SyntaxError) as e:
        raise ShareError(f"cannot read the DAG {dag_file}: {e}") from e
    found: Dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in _DAG_CONSTANTS:
                try:
                    found[name] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    missing = [n for n in ("HAMMER_EXEC", "WORK_DIR", "OBJ_DIR", "ENV_CONFIGS", "PROJ_CONFIGS") if n not in found]
    if missing:
        raise ShareError(f"{dag_file} is not a SledgeHammer DAG (no {', '.join(missing)}); "
                         f"regenerate it with make buildfile.")
    if not isinstance(found.get("PROJ_CONFS_BY_TOOLS"), dict):
        found["PROJ_CONFS_BY_TOOLS"] = {"default": list(found["PROJ_CONFIGS"])}
        found["DEFAULT_TOOLS"] = "default"
    found.setdefault("DESIGN_NAME", os.path.basename(str(found["OBJ_DIR"]).rstrip("/")))
    return found


def _candidate_paths(value: Any, work_dir: str, suffixes: Tuple[str, ...]) -> Iterator[str]:
    """Absolute paths a config value names: absolute tokens, and relative ones (resolved against the
    working directory, as the tools resolve them) that look like a path or a collateral file."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _candidate_paths(v, work_dir, suffixes)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _candidate_paths(v, work_dir, suffixes)
    elif isinstance(value, str):
        for token in _TOKEN_SPLIT_RE.split(value):
            if not token:
                continue
            if os.path.isabs(token):
                yield os.path.normpath(token)
            elif "/" in token or token.lower().endswith(suffixes):
                yield os.path.normpath(os.path.join(work_dir, token))


def _walk(directory: str, prune: Callable[[str], bool]) -> Iterator[str]:
    seen = set()
    for top, dirs, files in os.walk(directory, followlinks=True):
        real = os.path.realpath(top)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not prune(os.path.join(top, d)))
        for name in sorted(files):
            if not fingerprints.skip_name(name):
                yield os.path.join(top, name)


# --- building a bundle ---------------------------------------------------------------------------------

class Bundle:
    def __init__(self, manifest: Dict[str, Any], data: bytes, bundle_sha: str) -> None:
        self.manifest = manifest
        self.data = data
        self.bundle_sha = bundle_sha


def _sha256(path: str) -> str:
    return fingerprints.sha256_file(path)


def build_bundle(design: str, work_dir: str, obj_dir: str, env_configs: Iterable[str],
                 proj_confs_by_tools: Dict[str, List[str]], default_tools: str, hammer_exec: Optional[str],
                 config: Dict[str, Any], owner: Optional[str] = None) -> Bundle:
    """Pack the files a partner needs to build ``design`` as the owner does; ``config`` is the owner's merged
    config (HammerDriver.database), whose values name the RTL and collateral."""
    work_dir = os.path.normpath(os.path.abspath(work_dir))
    obj_dir = os.path.normpath(os.path.abspath(obj_dir))
    obj_rel = _relative(obj_dir, work_dir)
    excluded = _excluded_roots()
    files: Dict[str, str] = {}  # arcname -> absolute path
    external: Dict[str, None] = {}

    def arcname(path: str) -> Optional[str]:
        """Where a file lives in the bundle, or None when it is not the bundle's to carry."""
        if any(_relative(path, root) is not None for root in excluded):
            return None
        in_obj = _relative(path, obj_dir)
        if in_obj is not None:
            # The build dir holds stage outputs, which the PD cache or a rerun recreates. Only files at
            # its top level (generated inputs such as sram_generator-output.json) belong to the design.
            return f"obj/{in_obj}" if in_obj and "/" not in in_obj else None
        in_work = _relative(path, work_dir)
        if in_work:
            return f"work/{in_work}"
        return None

    def add_file(path: str) -> Optional[str]:
        name = arcname(path)
        if name is not None:
            files[name] = path
        elif not any(_relative(path, root) is not None for root in excluded + [obj_dir, work_dir]):
            external[os.path.normpath(path)] = None
        return name

    def prune(path: str) -> bool:
        return (_relative(path, obj_dir) is not None
                or any(_relative(path, root) is not None for root in excluded))

    def add_path(path: str) -> None:
        if _relative(work_dir, path) is not None:
            return  # the working directory itself or a directory above it: never the whole tree
        if os.path.isfile(path):
            add_file(path)
        elif os.path.isdir(path) and not prune(path):
            if _relative(path, work_dir) is None:
                external[os.path.normpath(path)] = None
                return
            for f in _walk(path, prune):
                add_file(f)
                if len(files) > MAX_BUNDLE_FILES:
                    raise ShareError(f"{path} holds more than {MAX_BUNDLE_FILES} files; a bundle that big "
                                     f"is not a design. Point the config at a narrower directory.")

    def config_arcnames(paths: Iterable[str]) -> List[str]:
        out = []
        for p in paths:
            name = add_file(os.path.normpath(os.path.abspath(p)))
            out.append(name if name is not None else os.path.normpath(os.path.abspath(p)))
        return out

    env_arcs = config_arcnames(env_configs)
    tools_arcs = {tool: config_arcnames(confs) for tool, confs in proj_confs_by_tools.items()}
    driver_arc = None
    rewrite = set(a for a in env_arcs + [a for arcs in tools_arcs.values() for a in arcs] if "/" in a
                  and not os.path.isabs(a))
    if hammer_exec:
        driver_arc = add_file(os.path.normpath(os.path.abspath(hammer_exec)))
        if driver_arc is not None:
            rewrite.add(driver_arc)
            # hooks split out of the driver into modules beside it
            here = os.path.dirname(os.path.abspath(hammer_exec))
            for name in sorted(os.listdir(here)):
                if name.endswith(".py") and os.path.isfile(os.path.join(here, name)):
                    arc = add_file(os.path.join(here, name))
                    if arc is not None:
                        rewrite.add(arc)
    suffixes = fingerprints.collateral_suffixes() + fingerprints.RTL_INCLUDE_SUFFIXES
    for path in dict.fromkeys(_candidate_paths(config, work_dir, suffixes)):
        if os.path.exists(path):
            add_path(path)

    total = sum(os.path.getsize(p) for p in files.values())
    if total > MAX_BUNDLE_BYTES:
        raise ShareError(f"the design's files add up to {total / 1024 ** 3:.1f} GiB, over the "
                         f"{MAX_BUNDLE_BYTES / 1024 ** 3:.0f} GiB a bundle may hold.")
    hashes = {name: _sha256(path) for name, path in sorted(files.items())}
    manifest: Dict[str, Any] = {
        "version": BUNDLE_VERSION,
        "design": design,
        "owner": owner,
        "source": {"work_dir": work_dir, "obj_dir": obj_dir},
        "obj_dir_rel": obj_rel,
        "env_configs": env_arcs,
        "proj_confs_by_tools": tools_arcs,
        "default_tools": default_tools if default_tools in tools_arcs else sorted(tools_arcs)[0],
        "driver": driver_arc,
        "rewrite": sorted(rewrite),
        "files": hashes,
        "external": sorted(external),
        "hammer": hammer_identity(),
    }
    bundle_sha = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    manifest["created_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    return Bundle(manifest, _pack(files), bundle_sha)


def _pack(files: Dict[str, str]) -> bytes:
    """gzip tar of the files under their arcnames, mtimes kept (fingerprints key files too big to hash by
    whole-second mtime) and owners scrubbed."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(files):
            info = tar.gettarinfo(files[name], arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode &= 0o755
            with open(files[name], "rb") as f:
                tar.addfile(info, f)
    return buf.getvalue()


def hammer_identity() -> Dict[str, Optional[str]]:
    """The Hammer a bundle was made with. Its framework fingerprint is part of every cache key, so a
    partner on a different Hammer cannot hit the owner's cache."""
    from hammer.vlsi import code_fingerprints
    commit = None
    try:
        out = subprocess.run(["git", "-C", fingerprints.hammer_dir(), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        commit = out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        commit = None
    try:
        framework = code_fingerprints.framework_fingerprint()
    except Exception:
        framework = None
    return {"commit": commit, "framework_fingerprint": framework}


@contextlib.contextmanager
def _cwd(path: str) -> Iterator[None]:
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def bundle_from_dag(dag_file: str, owner: Optional[str] = None) -> Bundle:
    """Bundle the design a generated DAG builds, reading its merged config the way its tasks do."""
    from hammer.vlsi import HammerDriver, HammerDriverOptions
    dag = read_dag(dag_file)
    tools = dag["PROJ_CONFS_BY_TOOLS"]
    default_tools = dag.get("DEFAULT_TOOLS") if dag.get("DEFAULT_TOOLS") in tools else sorted(tools)[0]
    with _cwd(dag["WORK_DIR"]):
        driver = HammerDriver(HammerDriverOptions(
            environment_configs=list(dag["ENV_CONFIGS"]), project_configs=list(tools[default_tools]),
            log_file=os.path.join(dag["OBJ_DIR"], "studio-share.log"), obj_dir=dag["OBJ_DIR"]))
        config = json.loads(driver.database.get_database_json())
        return build_bundle(dag["DESIGN_NAME"], dag["WORK_DIR"], dag["OBJ_DIR"], dag["ENV_CONFIGS"], tools,
                            default_tools, dag["HAMMER_EXEC"], config, owner)


def bundle_from_driver(driver: Any, owner: Optional[str] = None) -> Bundle:
    """Bundle the design a running action builds: the DAG in its build dir if it has one (all its tools
    choices), else the configs this action was given."""
    dag_file = os.path.join(driver.obj_dir, "hammer_dag.py")
    config = json.loads(driver.database.get_database_json())
    design = os.environ.get("HAMMER_AIRFLOW_DESIGN") or os.path.basename(str(driver.obj_dir).rstrip("/"))
    if os.path.isfile(dag_file):
        dag = read_dag(dag_file)
        tools = dag["PROJ_CONFS_BY_TOOLS"]
        default_tools = dag.get("DEFAULT_TOOLS") if dag.get("DEFAULT_TOOLS") in tools else sorted(tools)[0]
        return build_bundle(design, dag["WORK_DIR"], driver.obj_dir, dag["ENV_CONFIGS"], tools, default_tools,
                            dag["HAMMER_EXEC"], config, owner)
    return build_bundle(design, os.getcwd(), driver.obj_dir, driver.options.environment_configs,
                        {"default": list(driver.options.project_configs)}, "default", sys.argv[0], config, owner)


def republish_if_shared(driver: Any) -> Optional[str]:
    """After a successful syn: if this design is shared, publish its bundle again when its files changed, so
    partners who pull get what this run built. Returns a one-line note, or None when nothing was done.
    Never raises; sharing must not fail a stage that succeeded."""
    from hammer.vlsi import pd_cache, pd_store
    try:
        if not pd_cache.is_cache_enabled(driver):
            return None
        design = os.environ.get("HAMMER_AIRFLOW_DESIGN") or os.path.basename(str(driver.obj_dir).rstrip("/"))
        if not pd_store.design_has_shares(design):
            return None
    except Exception:
        return None  # no database, or no sharing tables yet: nothing is shared that could need refreshing
    try:
        bundle = bundle_from_driver(driver)
        if pd_store.publish_design_bundle(design, bundle.manifest, bundle.data, bundle.bundle_sha):
            return f"published the updated {design} bundle for its partners ({len(bundle.data)} bytes)"
        return None
    except Exception as e:
        return f"could not refresh the shared {driver.obj_dir} bundle ({type(e).__name__}: {e})"


# --- pulling --------------------------------------------------------------------------------------------

def _check_member(member: tarfile.TarInfo) -> str:
    """The member's relative path, or ShareError for anything but a plain file or directory inside
    work/ or obj/."""
    name = member.name
    parts = name.split("/")
    if (os.path.isabs(name) or ".." in parts or not parts or parts[0] not in ("work", "obj")
            or not (member.isfile() or member.isdir())):
        raise ShareError(f"the bundle holds {name!r}, which a design bundle never contains; refusing it.")
    return "/".join(parts[1:])


def _rewrite(text: str, pairs: List[Tuple[str, str]]) -> str:
    for old, new in pairs:
        text = re.sub(re.escape(old) + r"(?=$|[/\s\"'{}\[\]();,=<>|:])", new.replace("\\", "\\\\"), text)
    return text


def unpack(data: bytes, manifest: Dict[str, Any], work_dir: str, obj_dir: str) -> List[str]:
    """Write a bundle into work_dir (and obj_dir for build-dir files), with the owner's absolute paths in
    the configs and driver pointed at these directories. Returns the files written, relative to work_dir
    or as absolute paths for build-dir files."""
    source = manifest.get("source") or {}
    pairs: List[Tuple[str, str]] = []
    if manifest.get("obj_dir_rel") is None and source.get("obj_dir"):
        pairs += [(form, obj_dir) for form in _forms(source["obj_dir"])]
    if source.get("work_dir"):
        pairs += [(form, work_dir) for form in _forms(source["work_dir"])]
    pairs.sort(key=lambda p: -len(p[0]))
    rewrite = set(manifest.get("rewrite") or [])
    written = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            rel = _check_member(member)
            root = work_dir if member.name.startswith("work/") else obj_dir
            dest = os.path.join(root, *rel.split("/")) if rel else root
            if member.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            src = tar.extractfile(member)
            body = src.read() if src is not None else b""
            if member.name in rewrite:
                body = _rewrite(body.decode("utf-8", "surrogateescape"), pairs).encode("utf-8", "surrogateescape")
            tmp = f"{dest}.{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.chmod(tmp, (member.mode & 0o755) | 0o600)
            os.replace(tmp, dest)
            if member.name not in rewrite:
                os.utime(dest, (member.mtime, member.mtime))
            written.append(rel if member.name.startswith("work/") else dest)
    return written


def _local_path(arc: str, work_dir: str, obj_dir: str) -> str:
    if arc.startswith("work/"):
        return os.path.join(work_dir, *arc[len("work/"):].split("/"))
    if arc.startswith("obj/"):
        return os.path.join(obj_dir, *arc[len("obj/"):].split("/"))
    return arc  # an absolute path outside the owner's checkout, used as is


def pull(owner: str, design: str, into: str, env_configs: Optional[List[str]] = None, force: bool = False,
         fetch: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
         build: bool = True) -> Dict[str, Any]:
    """Fetch ``owner``'s bundle of ``design`` into ``into`` and generate the caller's own DAG for it.

    ``into`` becomes the build's working directory: the DAG runs from it, so the cache keys match the
    owner's. Pulling again updates it to the owner's latest bundle and removes files the owner dropped.
    """
    if fetch is None:
        from hammer.vlsi import pd_store
        fetch = pd_store.fetch_design_bundle
    rec = fetch(owner, design)
    if rec is None:
        raise ShareError(f"no bundle of {design!r} from {owner!r} is visible to you. Either it was never "
                         f"published, or {owner} has not shared it with you (they run: studio share {design} "
                         f"{getpass.getuser()}).")
    manifest = rec["manifest"]
    if manifest.get("version") != BUNDLE_VERSION:
        raise ShareError(f"the bundle is format {manifest.get('version')}, this Hammer reads {BUNDLE_VERSION}; "
                         f"update Hammer.")
    work_dir = os.path.normpath(os.path.abspath(into))
    state_path = os.path.join(work_dir, STATE_FILE)
    previous: Dict[str, Any] = {}
    if os.path.isfile(state_path):
        previous = json.loads(Path(state_path).read_text())
        if (previous.get("owner"), previous.get("design")) != (owner, design):
            raise ShareError(f"{work_dir} holds {previous.get('owner')}'s {previous.get('design')}; pull "
                             f"{owner}'s {design} into another directory.")
    elif os.path.isdir(work_dir) and os.listdir(work_dir) and not force:
        raise ShareError(f"{work_dir} is not empty and was not made by a pull; pull into a new directory, or "
                         f"pass --force to write into it anyway.")
    obj_rel = manifest.get("obj_dir_rel")
    obj_dir = os.path.join(work_dir, *(obj_rel or f"build/{design}").split("/"))
    os.makedirs(obj_dir, exist_ok=True)
    written = unpack(rec["data"], manifest, work_dir, obj_dir)
    kept = set(written)
    for old in previous.get("files", []):
        if old not in kept:
            path = old if os.path.isabs(old) else os.path.join(work_dir, old)
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
    missing = [p for p in manifest.get("external", []) if not os.path.exists(p)]
    state = {"owner": owner, "design": design, "bundle_sha": rec["bundle_sha"],
             "bundle_updated_at": rec.get("updated_at"), "files": sorted(written), "obj_dir": obj_dir}
    Path(state_path).write_text(json.dumps(state, indent=2) + "\n")
    summary: Dict[str, Any] = {"work_dir": work_dir, "obj_dir": obj_dir, "files": len(written),
                               "missing_external": missing, "hammer_mismatch": _hammer_mismatch(manifest),
                               "dag_id": dag_id_for(design, owner), "dag_file": None}
    if build:
        summary["dag_file"] = generate_dag(manifest, work_dir, obj_dir, owner, env_configs)
    return summary


def _hammer_mismatch(manifest: Dict[str, Any]) -> Optional[str]:
    theirs = (manifest.get("hammer") or {}).get("framework_fingerprint")
    mine = hammer_identity()
    if theirs and mine["framework_fingerprint"] and theirs != mine["framework_fingerprint"]:
        commit = (manifest.get("hammer") or {}).get("commit")
        return (f"your Hammer ({mine['commit'] or 'unknown commit'}) differs from the one the bundle was made "
                f"with ({commit or 'unknown commit'}). Every cache key includes Hammer's own code, so the "
                f"owner's results will not be reused until you run the same Hammer.")
    return None


def dag_id_for(design: str, owner: str, user: Optional[str] = None) -> str:
    """The pulled design's DAG id. It names the owner, so it never replaces the caller's own DAG of a design
    with the same name, and `sledgehammer run` finds it from the pull directory like any other."""
    user = user or getpass.getuser()
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"sledgehammer_{design}_from_{owner}_{user}")


def generate_dag(manifest: Dict[str, Any], work_dir: str, obj_dir: str, owner: str,
                 env_configs: Optional[List[str]] = None) -> str:
    """Build the caller's own DAG for a pulled design, the way make buildfile does: the owner's driver (or
    this Hammer's hammer-vlsi) runs `build` from the pull directory, so the DAG runs from there too."""
    design = manifest["design"]
    tools = manifest["proj_confs_by_tools"]
    proj = [_local_path(a, work_dir, obj_dir) for a in tools[manifest["default_tools"]]]
    envs = env_configs or [_local_path(a, work_dir, obj_dir) for a in manifest["env_configs"]]
    driver = manifest.get("driver")
    exe = _local_path(driver, work_dir, obj_dir) if driver else os.path.join(os.path.dirname(sys.executable),
                                                                             "hammer-vlsi")
    build_config = os.path.join(work_dir, BUILD_CONFIG)
    Path(build_config).write_text(
        "# Written by studio pull: build this design as a SledgeHammer DAG of your own.\n"
        "vlsi.core.build_system: sledgehammer\n"
        f"vlsi.core.airflow_dag_id: {dag_id_for(design, owner)}\n")
    cmd = [sys.executable, exe]
    for e in envs:
        cmd += ["-e", e]
    for p in proj + [build_config]:
        cmd += ["-p", p]
    cmd += ["--obj_dir", obj_dir, "-o", os.path.join(obj_dir, "studio-pull-build.json"),
            "--log", os.path.join(obj_dir, "studio-pull-build.log"), "build"]
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    res = subprocess.run(cmd, cwd=work_dir, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if res.returncode != 0:
        raise ShareError(f"the files are in {work_dir}, but generating your DAG failed:\n"
                         f"  {' '.join(cmd)}\n{(res.stderr or res.stdout).strip()[-2000:]}")
    return os.path.join(obj_dir, "hammer_dag.py")
