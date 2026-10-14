"""Fingerprints of the framework, tool, technology and hook code that shapes tool input, by version-neutral AST."""
from __future__ import annotations

import ast
import copy
import fnmatch
import functools
import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import json
import marshal
import os
import re
import stat
import sys
import sysconfig
import tempfile
import textwrap
import threading
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple, Union, cast

from hammer.vlsi.fingerprints import (_MISSING_ERRNOS, Roots, _norm, debug_record, digest_lines, file_line,
                                      path_roots, sha256_file, skip_name, tokenize_value)
from hammer.vlsi.pd_store import KNOWN_STAGE_TAGS

SERIALIZER = "neutral-ast-1"
NORMALIZE_LIMIT = 64 * 1024 * 1024

_HAMMER = str(Path(__file__).resolve().parent.parent)

_FRAMEWORK_FILES: Tuple[str, ...] = (
    "vlsi/hammer_tool.py",
    "vlsi/hammer_vlsi_impl.py",
    "vlsi/driver.py",
    "vlsi/constraints.py",
    "vlsi/units.py",
    "vlsi/hooks.py",
    "tech/__init__.py",
    "tech/specialcells.py",
    "tech/stackup.py",
    "utils/__init__.py",
    "utils/lef_utils.py",
    "utils/lib_utils.py",
    "utils/verilog_utils.py",
)

_INFRA_FILES: Tuple[str, ...] = (
    "vlsi/__init__.py",
    "vlsi/cli_driver.py",
    "vlsi/pd_cache.py",
    "vlsi/pd_store.py",
    "vlsi/pd_notify.py",
    "vlsi/substep_resume.py",
    "vlsi/time_tracking.py",
    "vlsi/error_scan.py",
    "vlsi/rtl_check.py",
    "vlsi/fingerprints.py",
    "vlsi/code_fingerprints.py",
    "vlsi/submit_command.py",
    "vlsi/hammer_build_systems.py",
    "vlsi/sledge_settings.py",
    "vlsi/legacy_hammer.py",
    "vlsi/design_share.py",
)

_TOOL_SIDE_PACKAGES: Tuple[str, ...] = ("vlsi/vendor",)

_INFRA_AREAS = frozenset({"config", "logging", "shell", "flowgraph"})

_SKIP_FIELDS = frozenset({"type_params", "type_comment", "kind"})
_DOC_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_PARSE_ERRORS = (SyntaxError, ValueError, RecursionError, MemoryError)
_DATA_SUFFIXES = (".yml", ".yaml", ".json")

_DEEP_RECURSION_LIMIT = 200_000
_DEEP_STACK_BYTES = 256 * 1024 * 1024
_DEEP_LOCK = threading.Lock()

_MEMO_ENV = "HAMMER_CODE_FP_CACHE"
_MEMO_OFF = frozenset({"0", "false", "no", "off"})
_MEMO_ENTRY_RE = re.compile(r"[0-9a-f]{64}")
_MEMO_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
    | getattr(os, "O_CLOEXEC", 0)
_MEMO_VERSION: Optional[str] = None

_SNAPSHOT: Dict[str, bytes] = {}
_SNAPSHOT_DIGEST: Dict[str, str] = {}
_FIRST: Dict[str, str] = {}
_DIGEST_MEMO: Dict[str, str] = {}
_IMPORTS: Dict[Tuple[str, str, str], FrozenSet[str]] = {}
_WARNED: Set[str] = set()

Unit = Tuple[str, Tuple[str, ...]]


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, _DOC_OWNERS):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:]
    return tree


def _neutral_dump(node: Any, out: List[str]) -> None:
    todo: List[Tuple[bool, Any]] = [(False, node)]
    while todo:
        literal, item = todo.pop()
        if literal:
            out.append(item)
        elif isinstance(item, ast.AST):
            out.append("(" + type(item).__name__)
            parts: List[Tuple[bool, Any]] = []
            for field in item._fields:
                if field in _SKIP_FIELDS:
                    continue
                value = getattr(item, field, None)
                if value is None or (isinstance(value, list) and not value):
                    continue
                parts.append((True, " " + field + "="))
                parts.append((False, value))
            parts.append((True, ")"))
            todo.extend(reversed(parts))
        elif isinstance(item, list):
            out.append("[")
            parts = []
            for i, value in enumerate(item):
                if i:
                    parts.append((True, ","))
                parts.append((False, value))
            parts.append((True, "]"))
            todo.extend(reversed(parts))
        else:
            out.append(repr(item))


def _dump_digest(node: Any) -> str:
    out: List[str] = [SERIALIZER, "\n"]
    _neutral_dump(node, out)
    return hashlib.sha256("".join(out).encode("utf-8", "surrogatepass")).hexdigest()


def _deep(fn: Callable[..., Any], *args: Any) -> Any:
    """Run fn, rerunning it on a deep-stack thread if it runs out of stack, so results never depend on the
    caller's depth."""
    try:
        return fn(*args)
    except (RecursionError, MemoryError):
        pass
    result: Dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn(*args)
        except BaseException as e:
            result["error"] = e

    with _DEEP_LOCK:
        old_limit = sys.getrecursionlimit()
        old_stack = threading.stack_size()
        try:
            sys.setrecursionlimit(max(old_limit, _DEEP_RECURSION_LIMIT))
            threading.stack_size(_DEEP_STACK_BYTES)
            worker = threading.Thread(target=target, name="code_fingerprints")
            worker.start()
            worker.join()
        finally:
            threading.stack_size(old_stack)
            sys.setrecursionlimit(old_limit)
    if "error" in result:
        raise result["error"]
    return result["value"]


def _parse(data: Union[bytes, str]) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(data)


def _ast_digest(data: bytes) -> str:
    try:
        return _deep(lambda: _dump_digest(_strip_docstrings(_parse(data))))
    except _PARSE_ERRORS:
        return "raw:" + hashlib.sha256(data).hexdigest()


def _source_version(path: str) -> Optional[str]:
    try:
        src = _read(path)
    except (OSError, ValueError):
        return None
    tag = f"{sys.implementation.name}-{sys.version_info[0]}.{sys.version_info[1]}\n".encode("ascii")
    return hashlib.sha256(tag + src).hexdigest()


def _memo_dir() -> Optional[str]:
    """${XDG_CACHE_HOME:-~/.cache}/sledgehammer/ast, or None when the memo is off or ownership cannot be checked."""
    if not hasattr(os, "getuid") or not _MEMO_VERSION:
        return None
    if os.environ.get(_MEMO_ENV, "").strip().lower() in _MEMO_OFF:
        return None
    base = os.environ.get("XDG_CACHE_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "sledgehammer", "ast") if os.path.isabs(base) else None


def _private(st: os.stat_result) -> bool:
    return st.st_uid == os.getuid() and not st.st_mode & 0o077


def _make_dir_private(directory: str) -> bool:
    """Tighten a memo dir we own that others can only read to 0700; refuse one others own or can write."""
    st = os.stat(directory)
    if st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    if st.st_mode & 0o077:
        os.chmod(directory, 0o700)
        st = os.stat(directory)
    return _private(st)


def _memo_entry(directory: str, sha: str) -> str:
    return os.path.join(directory, f"{sha}-{_MEMO_VERSION}")


def _memo_load(directory: str, sha: str) -> Optional[str]:
    try:
        if not _private(os.stat(directory)):
            return None
        fd = os.open(_memo_entry(directory, sha), _MEMO_OPEN_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not _private(st):
            return None
        data = os.read(fd, 65)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = data.decode("ascii", "replace")
    return text if _MEMO_ENTRY_RE.fullmatch(text) else None


def _make_private_dirs(path: str) -> None:
    missing = []
    while not os.path.isdir(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for p in reversed(missing):
        try:
            os.mkdir(p, 0o700)
        except FileExistsError:
            pass


def _memo_store(directory: str, sha: str, digest: str) -> None:
    try:
        _make_private_dirs(directory)
        if not _make_dir_private(directory):
            return
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(digest)
            os.replace(tmp, _memo_entry(directory, sha))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass


def digest_source(data: bytes) -> str:
    """The code digest of Python source bytes, memoized in process and in the private per-user memo."""
    sha = hashlib.sha256(data).hexdigest()
    hit = _DIGEST_MEMO.get(sha)
    if hit is None:
        directory = _memo_dir()
        hit = _memo_load(directory, sha) if directory else None
        if hit is None:
            hit = _ast_digest(data)
            if directory and not hit.startswith("raw:"):
                _memo_store(directory, sha, hit)
        _DIGEST_MEMO[sha] = hit
    return hit


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _take_snapshot(root: str, files: Iterable[str]) -> None:
    for rel in files:
        path = os.path.realpath(os.path.join(root, rel))
        try:
            _SNAPSHOT[path] = _read(path)
        except (OSError, ValueError):
            continue


def _snapshot_digest(path: str) -> str:
    snap = _SNAPSHOT[path]
    loaded = _SNAPSHOT_DIGEST.get(path)
    if loaded is None:
        loaded = _SNAPSHOT_DIGEST[path] = digest_source(snap)
    try:
        disk: Optional[bytes] = _read(path)
    except PermissionError:
        disk = None
    except OSError as e:
        if e.errno not in _MISSING_ERRNOS:
            raise
        disk = None
    if disk == snap or (disk is not None and digest_source(disk) == loaded):
        return loaded
    if path not in _WARNED:
        _WARNED.add(path)
        print(f"code_fingerprints: {path} changed on disk after this process loaded it; "
              f"restart long-lived workers to pick up the new code", file=sys.stderr)
    return "inmem:" + loaded


def code_digest(path: str) -> str:
    """Digest of a code file as this process loaded it; the caller decides that path holds code."""
    key = os.path.realpath(path)
    if key in _SNAPSHOT:
        return _snapshot_digest(key)
    hit = _FIRST.get(key)
    if hit is None:
        hit = _FIRST[key] = digest_source(_read(key))
    return hit


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def _content_digest(path: str) -> str:
    if os.stat(path).st_size > NORMALIZE_LIMIT:
        return sha256_file(path)
    data = _read(path)
    if _is_text(data):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _str_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _str_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_str_keys(v) for v in obj]
    return obj


def _data_digest(path: str) -> str:
    if os.stat(path).st_size > NORMALIZE_LIMIT:
        return sha256_file(path)
    data = _read(path)
    try:
        if path.lower().endswith(".json"):
            obj = json.loads(data.decode("utf-8"))
        else:
            import yaml
            obj = yaml.safe_load(data)
        canon = json.dumps(_str_keys(obj), sort_keys=True, default=str)
        return "data:" + hashlib.sha256(canon.encode("utf-8", "surrogatepass")).hexdigest()
    except Exception:
        if _is_text(data):
            data = data.replace(b"\r\n", b"\n")
        return hashlib.sha256(data).hexdigest()


def _file_digest(path: str) -> str:
    low = path.lower()
    try:
        if low.endswith(".py"):
            return code_digest(path)
        if low.endswith(_DATA_SUFFIXES):
            return _data_digest(path)
        return _content_digest(path)
    except PermissionError:
        return "UNREADABLE"
    except OSError as e:
        if e.errno in _MISSING_ERRNOS:
            return "MISSING"
        raise
    except ValueError:
        if "\0" in path:
            return "MISSING"
        raise


def framework_lines(root: Optional[str] = None) -> List[str]:
    base = _HAMMER if root is None else root
    lines = []
    for rel in _FRAMEWORK_FILES:
        path = os.path.join(base, rel)
        if os.path.realpath(path) not in _SNAPSHOT and not os.path.isfile(path):
            lines.append(f"{rel}=MISSING")
        else:
            lines.append(f"{rel}={_file_digest(path)}")
    return lines


def framework_fingerprint(root: Optional[str] = None) -> str:
    """vlsi.framework_fingerprint_sha256: the allowlisted hammer framework files."""
    lines = framework_lines(root)
    debug_record("vlsi.framework", lines)
    return digest_lines(lines)


def _hammer_roots() -> List[str]:
    roots = [_HAMMER]
    pkg = sys.modules.get("hammer")
    for entry in list(getattr(pkg, "__path__", None) or []):
        real = os.path.realpath(entry)
        if real not in roots and not _is_foreign(real):
            roots.append(real)
    return roots


def _hammer_parts(path: str) -> Optional[List[str]]:
    real = os.path.realpath(path)
    for root in _hammer_roots():
        prefix = root.rstrip(os.sep) + os.sep
        if real.startswith(prefix):
            return real[len(prefix):].split(os.sep)
    return None


def _foreign_prefixes() -> List[str]:
    out = []
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = paths.get(key)
        if p:
            out.append(os.path.realpath(p).rstrip(os.sep) + os.sep)
    return out


def _is_stdlib(path: str) -> bool:
    real = os.path.realpath(path)
    parts = real.split(os.sep)
    if "site-packages" in parts or "dist-packages" in parts:
        return False
    paths = sysconfig.get_paths()
    prefixes = (paths.get("stdlib"), paths.get("platstdlib"))
    return any(real.startswith(os.path.realpath(p).rstrip(os.sep) + os.sep) for p in prefixes if p)


def _is_foreign(path: str) -> bool:
    real = os.path.realpath(path)
    parts = real.split(os.sep)
    if "site-packages" in parts or "dist-packages" in parts:
        return True
    return any(real.startswith(p) for p in _foreign_prefixes())


def _hammer_package(area: str, pkg: str) -> Optional[Unit]:
    if area in _INFRA_AREAS:
        return None
    dirs = tuple(d for d in (os.path.join(r, area, pkg) for r in _hammer_roots()) if os.path.isdir(d))
    return (f"hammer.{area}.{pkg}", dirs) if dirs else None


def _search_locations(mod: Any) -> List[str]:
    spec = getattr(mod, "__spec__", None)
    locs = getattr(spec, "submodule_search_locations", None) or getattr(mod, "__path__", None) or []
    return [_norm(p) for p in locs]


def _module_unit(mod: Any, own: bool = False) -> Optional[Unit]:
    """The package a module belongs to, hashed whole, or None for framework, infra and foreign code."""
    name = getattr(mod, "__name__", "") or ""
    file = getattr(mod, "__file__", None)
    locs = _search_locations(mod)
    probe = file or (locs[0] if locs else None)
    if not probe:
        return None
    parts = _hammer_parts(probe)
    if parts is not None:
        rel = "/".join(parts)
        if parts[0] in _INFRA_AREAS or rel in _FRAMEWORK_FILES or rel in _INFRA_FILES:
            return None
        if len(parts) >= 3:
            return _hammer_package(parts[0], parts[1])
        if file:
            return name, (_norm(file),)
        return None
    if not own and _is_foreign(probe):
        return None
    if locs:
        return name, tuple(locs)
    package = getattr(mod, "__package__", None) or ""
    if package:
        plocs = _search_locations(sys.modules.get(package))
        if plocs:
            return package, tuple(plocs)
    return (name, (_norm(file),)) if file else None


def _unit_files(unit: Unit) -> List[Tuple[str, str]]:
    """(full path, path relative to the unit) for every regular file of a unit."""
    out = []
    for base in unit[1]:
        if not os.path.isdir(base):
            if os.path.isfile(base):
                out.append((base, os.path.basename(base)))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not skip_name(d))
            for name in sorted(filenames):
                if skip_name(name):
                    continue
                full = os.path.join(dirpath, name)
                if os.path.isfile(full):
                    out.append((full, os.path.relpath(full, base).replace(os.sep, "/")))
    return out


def _module_name(label: str, rel: str, single: bool) -> str:
    if single:
        return label
    stem = rel[:-3] if rel.endswith(".py") else rel
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([label] + parts)


def _read_error_line(path: str, e: BaseException) -> str:
    """MISSING or UNREADABLE for errors that mean the file is absent or private; anything else is re-raised."""
    if isinstance(e, PermissionError):
        return "UNREADABLE"
    if isinstance(e, OSError) and e.errno in _MISSING_ERRNOS:
        return "MISSING"
    if isinstance(e, ValueError) and "\0" in path:
        return "MISSING"
    raise e


def _imported_names(path: str, modname: str, is_pkg: bool) -> FrozenSet[str]:
    try:
        data = _read(path)
    except (OSError, ValueError) as e:
        _read_error_line(path, e)
        return frozenset()
    package = modname if is_pkg else modname.rpartition(".")[0]
    key = (path, hashlib.sha256(data).hexdigest(), package)
    hit = _IMPORTS.get(key)
    if hit is None:
        try:
            hit = frozenset(_tree_imports(_deep(_parse, data), package))
        except _PARSE_ERRORS:
            hit = frozenset()
        _IMPORTS[key] = hit
    return hit


def _tree_imports(tree: ast.AST, package: str) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                names.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".") if package else []
                if node.level - 1:
                    base = base[:-(node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target:
                names.add(target)
                names.update(f"{target}.{a.name}" for a in node.names if a.name != "*")
    return names


def _import_unit(name: str) -> Optional[Unit]:
    parts = name.split(".")
    if len(parts) < 3 or parts[0] != "hammer":
        return None
    return _hammer_package(parts[1], parts[2])


def _tool_class(mod: ModuleType) -> Optional[type]:
    tool = getattr(mod, "tool", None)
    if tool is None:
        return None
    return tool if isinstance(tool, type) else type(tool)


def _tool_units(mod: ModuleType) -> Tuple[Dict[str, Tuple[str, ...]], Dict[str, List[Tuple[str, str]]]]:
    units: Dict[str, Tuple[str, ...]] = {}
    own = _module_unit(mod, own=True)
    if own is not None:
        units[own[0]] = own[1]
    cls = _tool_class(mod)
    for klass in (cls.__mro__ if cls is not None else ()):
        unit = _module_unit(sys.modules.get(klass.__module__))
        if unit is not None and unit[0] not in units:
            units[unit[0]] = unit[1]
    files: Dict[str, List[Tuple[str, str]]] = {}
    pending = list(units)
    while pending:
        label = pending.pop()
        files[label] = _unit_files((label, units[label]))
        single = all(not os.path.isdir(p) for p in units[label])
        for full, rel in files[label]:
            if not rel.endswith(".py"):
                continue
            modname = _module_name(label, rel, single)
            for name in _imported_names(full, modname, rel.endswith("__init__.py")):
                unit = _import_unit(name)
                if unit is not None and unit[0] not in units:
                    units[unit[0]] = unit[1]
                    pending.append(unit[0])
    return units, files


def tool_lines(tool_module: Union[str, ModuleType, None]) -> List[str]:
    if not tool_module:
        return ["<no tool>"]
    mod = importlib.import_module(tool_module) if isinstance(tool_module, str) else tool_module
    _, files = _tool_units(mod)
    return [f"{label}/{rel}={_file_digest(full)}" for label in files for full, rel in files[label]]


def tool_fingerprint(tool_module: Union[str, ModuleType, None]) -> str:
    """<stage>.tool_fingerprint_sha256: the tool's package, its MRO packages and the hammer packages it imports."""
    lines = tool_lines(tool_module)
    debug_record(f"tool:{getattr(tool_module, '__name__', tool_module)}", lines)
    return digest_lines(lines)


def _tech_package_lines(tech: Any) -> List[str]:
    name = getattr(tech, "package", "") or type(tech).__module__
    mod = sys.modules.get(name)
    if mod is None:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            return [f"{name}=<unimportable>"]
    main = getattr(sys.modules.get(type(tech).__module__), "__file__", None)
    main = _norm(main) if main else None
    lines = []
    for base in _search_locations(mod):
        for full, rel in _unit_files((name, (base,))):
            leaf = rel.rsplit("/", 1)[-1]
            if _norm(full) == main or (rel == "__init__.py" and main is None):
                continue
            if "/" not in rel and fnmatch.fnmatch(leaf, "defaults*.yml"):
                continue
            if leaf.endswith(".py") and not leaf[:-3].isidentifier():
                continue
            lines.append(f"{name}/{rel}={_file_digest(full)}")
    return lines


def tech_lines(tech: Any, roots: Roots = ()) -> List[str]:
    if tech is None:
        return ["<no tech>"]
    config = getattr(tech, "config", None)
    if config is None:
        lines = ["config=<none>"]
    else:
        dumped = config.model_dump(mode="json", exclude={"drc_decks", "lvs_decks"})
        blob = json.dumps(tokenize_value(dumped, roots, resolve=False), sort_keys=True, default=str)
        lines = ["config=" + hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()]
    return lines + _tech_package_lines(tech)


def tech_fingerprint(tech: Any, roots: Roots = ()) -> str:
    """vlsi.tech_fingerprint_sha256: the loaded TechConfig without decks, plus the tech package's data files."""
    lines = tech_lines(tech, roots)
    debug_record("vlsi.tech", lines)
    return digest_lines(lines)


_HOOK_STAGES: Tuple[str, ...] = KNOWN_STAGE_TAGS
_STAGE_ALIASES = {"syn": "synthesis"}
_GETTER_RE = re.compile(r"^get_(?:tech|extra)_(?:hierarchical_)?(syn|" + "|".join(_HOOK_STAGES) + r")_hooks$")
_DYNAMIC_NAMES = frozenset({"globals", "vars", "locals", "eval", "exec", "compile", "__import__"})
_SAFE_DECORATORS = frozenset({"staticmethod", "classmethod", "property"})
_MUTATORS = frozenset({"append", "extend", "insert", "update", "add", "setdefault", "pop", "popitem",
                       "remove", "discard", "clear", "sort", "reverse", "__setitem__"})
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ABS_TOKEN_RE = re.compile(r"(/[A-Za-z0-9._+@%=-]+(?:/[A-Za-z0-9._+@%=-]+)+)")
_ADDRESS_RE = re.compile(r" at 0x[0-9a-fA-F]+")
_EVERYTHING = "*"
_FUNC_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SHARED_BASES = frozenset({"self", "cls"})

_SOURCES: Dict[str, bytes] = {}
_LITERALS: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], str] = {}
_BASE_NAMES: Optional[frozenset] = None


def hook_stage(name: str) -> Optional[str]:
    """The stage a hook getter name serves (get_tech_syn_hooks gives synthesis), or None."""
    m = _GETTER_RE.match(name)
    return _STAGE_ALIASES.get(m.group(1), m.group(1)) if m else None


def _base_names() -> frozenset:
    global _BASE_NAMES
    if _BASE_NAMES is None:
        from hammer.tech import HammerTechnology
        from hammer.vlsi.cli_driver import CLIDriver
        names = set(dir(HammerTechnology)) | set(dir(CLIDriver))
        _BASE_NAMES = frozenset(n for n in names if hook_stage(n) is None)
    return _BASE_NAMES


def _source(path: str) -> bytes:
    """Module source as first read in this process."""
    hit = _SOURCES.get(path)
    if hit is None:
        hit = _SOURCES[path] = _read(path)
    return hit


def _refs(nodes: Iterable[ast.AST], import_time: bool = False) -> Set[str]:
    """Names the nodes use; a dynamic lookup counts as every name, but a dynamic import in code that runs at
    import time is only an import."""
    out: Set[str] = set()
    for root in nodes:
        for x in ast.walk(root):
            if isinstance(x, ast.Name):
                out.add(x.id)
                if x.id in _DYNAMIC_NAMES and not (import_time and x.id == "__import__"):
                    out.add(_EVERYTHING)
            elif isinstance(x, ast.Attribute):
                out.add(x.attr)
                if x.attr == "import_module" and not import_time:
                    out.add(_EVERYTHING)
            elif isinstance(x, ast.Constant) and isinstance(x.value, str) and _IDENT_RE.match(x.value):
                out.add(x.value)
    return out


def _local_names(node: ast.AST) -> Tuple[Set[str], Set[str]]:
    declared: Set[str] = set()
    names: Set[str] = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Global):
            declared.update(x.names)
        elif isinstance(x, ast.arg):
            names.add(x.arg)
        elif isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            names.add(x.id)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in x.names)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            names.add(x.name)
    return names - declared, declared


def _chain(node: ast.AST) -> Tuple[Optional[ast.Name], List[str]]:
    attrs: List[str] = []
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            attrs.append(node.attr)
        node = node.value
    return (node if isinstance(node, ast.Name) else None), attrs[::-1]


def _stores(node: ast.AST) -> Tuple[Set[str], Set[str]]:
    """Names a function writes outside its frame, and roots of attribute chains it writes through."""
    if not isinstance(node, _FUNC_DEFS):
        return set(), set()
    local, declared = _local_names(node)
    out: Set[str] = set()
    roots: Set[str] = set()

    def write(base: ast.AST, attr: Optional[str] = None) -> None:
        root, attrs = _chain(base)
        if root is None or (root.id not in _SHARED_BASES and root.id in local):
            return
        attrs = attrs + ([attr] if attr else [])
        out.update({attrs[0], attrs[-1]} if attrs else set())
        if root.id not in _SHARED_BASES:
            (roots if attrs else out).add(root.id)

    for x in ast.walk(node):
        ctx = getattr(x, "ctx", None)
        if isinstance(x, ast.Name) and isinstance(ctx, (ast.Store, ast.Del)) and x.id in declared:
            out.add(x.id)
        elif isinstance(x, (ast.Attribute, ast.Subscript)) and isinstance(ctx, (ast.Store, ast.Del)):
            write(x)
        elif isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr in _MUTATORS:
            write(x.func.value)
        elif isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id in ("setattr", "delattr") \
                and x.args:
            name = x.args[1] if len(x.args) > 1 else None
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                write(x.args[0], name.value)
            else:
                write(x.args[0])
    return out, roots


def _passed_names(node: ast.AST) -> Set[str]:
    """Non-local names a function passes as call arguments, which the callee may mutate."""
    if not isinstance(node, _FUNC_DEFS):
        return set()
    local, _ = _local_names(node)
    out: Set[str] = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Call):
            for arg in list(x.args) + [k.value for k in x.keywords]:
                arg = arg.value if isinstance(arg, ast.Starred) else arg
                if isinstance(arg, ast.Name) and arg.id not in local and arg.id not in _SHARED_BASES:
                    out.add(arg.id)
    return out


def _decorated(node: ast.AST) -> bool:
    names = {getattr(d, "id", getattr(d, "attr", None)) for d in getattr(node, "decorator_list", [])}
    return bool(names - _SAFE_DECORATORS)


def _simple_targets(st: ast.AST) -> Optional[List[str]]:
    targets = st.targets if isinstance(st, ast.Assign) else [getattr(st, "target", None)]
    names = [t.id for t in targets if isinstance(t, ast.Name)]
    return names if len(names) == len(targets) else None


class _HookUnit:
    __slots__ = ("mod", "key", "names", "node", "refs", "stores", "maybe", "seed", "forced")

    def __init__(self, mod: "_HookModule", key: str, names: Set[str], node: ast.AST,
                 seed: Optional[str] = None, forced: bool = False) -> None:
        self.mod = mod
        self.key = key
        self.names = names
        self.node = node
        self.refs = _refs([node], import_time=not isinstance(node, _FUNC_DEFS))
        self.stores, self.maybe = _stores(node)
        if seed is not None:
            self.maybe |= _passed_names(node)
        self.seed = seed
        self.forced = forced


class _HookModule:
    __slots__ = ("path", "name", "label", "live", "tree", "raw", "loose")

    def __init__(self, path: str, live: Optional[ModuleType], name: Optional[str] = None) -> None:
        self.path = path
        self.live = live
        self.name = getattr(live, "__name__", None) or name or ""
        parts = _hammer_parts(path)
        self.label = "hammer/" + "/".join(parts) if parts is not None else os.path.basename(path)
        self.tree: Optional[ast.Module] = None
        self.loose: List[ast.AST] = []
        try:
            data = _source(path)
        except (OSError, ValueError) as e:
            self.raw = _read_error_line(path, e)
            return
        self.raw = "raw:" + hashlib.sha256(data).hexdigest()
        try:
            self.tree = cast(ast.Module, _strip_docstrings(_deep(_parse, data)))
        except _PARSE_ERRORS:
            self.tree = None

    def package(self) -> str:
        pkg = getattr(self.live, "__package__", None)
        if pkg is not None:
            return pkg
        return self.name if os.path.basename(self.path) == "__init__.py" else self.name.rpartition(".")[0]


class _HookAnalysis:
    """Attribution by subtraction: which code units only other stages' hook getters reach."""

    def __init__(self, mods: List[_HookModule], bases: frozenset) -> None:
        self.mods = mods
        self.units: List[_HookUnit] = []
        for mod in mods:
            for st in (mod.tree.body if mod.tree is not None else []):
                if isinstance(st, _FUNC_DEFS):
                    self.units.append(_HookUnit(mod, st.name, {st.name}, st, hook_stage(st.name), _decorated(st)))
                elif isinstance(st, ast.ClassDef):
                    for b in st.body:
                        if isinstance(b, _FUNC_DEFS):
                            seed = hook_stage(b.name)
                            forced = seed is None and (b.name in bases or _decorated(b)
                                                       or (b.name.startswith("__") and b.name.endswith("__")))
                            self.units.append(_HookUnit(mod, f"{st.name}.{b.name}", {b.name}, b, seed, forced))
                        else:
                            mod.loose.append(b)
                    mod.loose.extend(st.bases + st.keywords + st.decorator_list)
                elif isinstance(st, (ast.Assign, ast.AnnAssign)) and _simple_targets(st):
                    names = set(_simple_targets(st) or [])
                    self.units.append(_HookUnit(mod, "=" + ",".join(sorted(names)), names, st))
                else:
                    mod.loose.append(st)
        assigned = {n for u in self.units if not isinstance(u.node, _FUNC_DEFS) for n in u.names}
        self.by_name: Dict[str, List[_HookUnit]] = {}
        self.writers: Dict[str, List[_HookUnit]] = {}
        for u in self.units:
            u.stores |= u.maybe & assigned
            for n in u.names:
                self.by_name.setdefault(n, []).append(u)
            for n in u.stores:
                self.writers.setdefault(n, []).append(u)
        self.aliases: Dict[str, Set[str]] = {}
        for mod in mods:
            for x in (ast.walk(mod.tree) if mod.tree is not None else ()):
                if isinstance(x, (ast.Import, ast.ImportFrom)):
                    for a in x.names:
                        if a.asname and a.asname != a.name:
                            self.aliases.setdefault(a.asname, set()).add(a.name.rsplit(".", 1)[-1])
        global_refs = _refs([n for mod in mods for n in mod.loose], import_time=True)
        self.stage_reach = {s: self.reach([u for u in self.units if u.seed == s]) for s in _HOOK_STAGES}
        any_stage: Set[_HookUnit] = set().union(*self.stage_reach.values())
        start = [u for u in self.units if u.seed is None and (u.forced or u not in any_stage)]
        self.global_reach = self.reach(start, global_refs)

    def reach(self, start: Iterable[_HookUnit], refs: Iterable[str] = ()) -> Set[_HookUnit]:
        todo = list(start)

        def follow(names: Iterable[str]) -> bool:
            for r in names:
                if r == _EVERYTHING:
                    return True
                for n in (r,) + tuple(self.aliases.get(r, ())):
                    todo.extend(self.by_name.get(n, ()))
                    todo.extend(self.writers.get(n, ()))
            return False

        if follow(refs):
            return set(self.units)
        seen: Set[_HookUnit] = set()
        while todo:
            u = todo.pop()
            if u in seen:
                continue
            seen.add(u)
            if follow(u.refs):
                return set(self.units)
        return seen

    def kept(self, stage: str, extra_refs: Iterable[str] = ()) -> Set[_HookUnit]:
        mine = set(self.stage_reach.get(stage, set()))
        extra = set(extra_refs)
        if extra:
            mine |= self.reach([], extra)
        others: Set[_HookUnit] = set()
        for s, r in self.stage_reach.items():
            if s != stage:
                others |= r
        return {u for u in self.units if u in self.global_reach or u in mine or u not in others}

    def module_lines(self, kept: Set[_HookUnit]) -> List[str]:
        drop = {id(u.node) for u in self.units if u not in kept}
        lines = []
        for mod in self.mods:
            if mod.tree is None:
                lines.append(f"code|{mod.label}={mod.raw}")
                continue
            body: List[ast.AST] = []
            for st in mod.tree.body:
                if id(st) in drop:
                    continue
                if isinstance(st, ast.ClassDef):
                    st = copy.copy(st)
                    st.body = [b for b in st.body if id(b) not in drop]
                body.append(st)
            try:
                digest = _dump_digest(body)
            except _PARSE_ERRORS:
                digest = mod.raw
            lines.append(f"code|{mod.label}={digest}")
        return lines


def _isfile(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def _live_str(value: Any) -> Optional[str]:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    return value if isinstance(value, str) else None


def _render_fstring(node: ast.JoinedStr, live: Optional[ModuleType]) -> Optional[str]:
    names = vars(live) if live is not None else {}
    out = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            out.append(v.value)
        elif isinstance(v, ast.FormattedValue) and v.format_spec is None and isinstance(v.value, ast.Name):
            value = names.get(v.value.id)
            text = str(value) if isinstance(value, (int, float)) else _live_str(value)
            if text is None:
                return None
            out.append(text)
        else:
            return None
    return "".join(out)


def _literal_paths(mod: _HookModule, nodes: Iterable[ast.AST], values: Iterable[str]) -> Set[str]:
    """Files named by string literals in kept code: whole strings, absolute tokens, joined fragments, f-strings."""
    moddir = os.path.dirname(mod.path)
    found: Set[str] = set()

    def consider(s: str) -> None:
        if not s or "\0" in s:
            return
        if len(s) <= 4096 and "\n" not in s and " " not in s and ("/" in s or "." in s):
            cand = s if os.path.isabs(s) else os.path.join(moddir, s)
            if _isfile(cand):
                found.add(os.path.normpath(cand))
        if "/" in s:
            for m in _ABS_TOKEN_RE.finditer(s):
                if _isfile(m.group(1)):
                    found.add(os.path.normpath(m.group(1)))

    for value in values:
        consider(value)
    for node in nodes:
        for x in ast.walk(node):
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                consider(x.value)
            elif isinstance(x, (ast.Call, ast.BinOp, ast.JoinedStr)):
                if isinstance(x, ast.JoinedStr):
                    rendered = _render_fstring(x, mod.live)
                    if rendered is not None:
                        consider(rendered)
                frags = [c.value for c in ast.walk(x) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                if 2 <= len(frags) <= 16:
                    joined = "/".join(f.strip("/") for f in frags if f.strip("/"))
                    consider(("/" if frags[0].startswith("/") else "") + joined)
    return found


def _literal_line(path: str, roots: Roots) -> str:
    key = (path, tuple((p, t) for p, t in roots))
    hit = _LITERALS.get(key)
    if hit is None:
        hit = _LITERALS[key] = file_line(path, roots)
    return hit


def _config_files(driver: Any) -> Set[str]:
    """The real paths of the -e and -p files the driver loaded, whose settings the database already holds."""
    options = getattr(driver, "options", None)
    names = list(getattr(options, "environment_configs", None) or []) + \
        list(getattr(options, "project_configs", None) or [])
    return {os.path.realpath(n) for n in names if isinstance(n, str) and n}


def _literal_lines(analysis: _HookAnalysis, kept: Set[_HookUnit], roots: Roots, config_files: Set[str]) -> List[str]:
    """file| lines for the kept code's literal files, leaving out the config files the database covers."""
    paths: Set[str] = set()
    for mod in analysis.mods:
        units = [u for u in kept if u.mod is mod]
        values = []
        if mod.live is not None:
            names = vars(mod.live)
            for u in units:
                if isinstance(u.node, (ast.Assign, ast.AnnAssign)):
                    values.extend(v for v in (_live_str(names.get(n)) for n in sorted(u.names)) if v)
        paths |= _literal_paths(mod, [u.node for u in units] + mod.loose, values)
    return ["file|" + _literal_line(p, roots) for p in sorted(paths) if os.path.realpath(p) not in config_files]


def _canon(value: Any, roots: Roots, depth: int = 0) -> Any:
    """A process-independent stand-in for a value: sets and dicts sorted, paths tokenized."""
    if depth > 32:
        return f"<{type(value).__name__}>"
    if isinstance(value, (set, frozenset)):
        return (type(value).__name__, sorted((_canon(v, roots, depth + 1) for v in value), key=_safe_repr))
    if isinstance(value, dict):
        items = ((_canon(k, roots, depth + 1), _canon(v, roots, depth + 1)) for k, v in value.items())
        return ("dict", sorted(items, key=_safe_repr))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [_canon(v, roots, depth + 1) for v in value])
    if isinstance(value, os.PathLike):
        path = _live_str(value)
        return ("path", tokenize_value(path, roots, resolve=False)) if path is not None else _safe_repr(value)
    if isinstance(value, str):
        return tokenize_value(value, roots, resolve=False)
    return value


def _safe_repr(value: Any) -> str:
    try:
        text = repr(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    return _ADDRESS_RE.sub("", text)


def _unwrap(func: Any, roots: Roots = ()) -> Tuple[Any, List[str]]:
    lines = []
    for _ in range(64):
        if not isinstance(func, functools.partial):
            break
        lines.append("partial:" + _safe_repr(_canon(func.args, roots)) + _safe_repr(_canon(func.keywords, roots)))
        func = func.func
    func = getattr(func, "__func__", func)
    if getattr(func, "__code__", None) is None and not isinstance(func, type):
        call = getattr(type(func), "__call__", None)
        if getattr(call, "__code__", None) is not None:
            func = call
    return func, lines


def _ident(func: Any) -> str:
    try:
        module = getattr(func, "__module__", None) or type(func).__module__
        qualname = getattr(func, "__qualname__", None) or type(func).__qualname__
    except Exception:
        return f"<{type(func).__name__}>"
    return f"{module}:{qualname}"


def _code_path(func: Any) -> Optional[str]:
    code = getattr(func, "__code__", None)
    return os.path.realpath(code.co_filename) if code is not None else None


def _function_digest(func: Any, path: str) -> str:
    try:
        src = textwrap.dedent(inspect.getsource(func))
        return _dump_digest(_strip_docstrings(_deep(_parse, src)))
    except Exception:
        pass
    if _isfile(path):
        try:
            return "file:" + code_digest(path)
        except (OSError, ValueError):
            pass
    try:
        return "code:" + hashlib.sha256(marshal.dumps(func.__code__)).hexdigest()
    except Exception:
        return "nosrc"


def _tool_setting(driver: Any, stage: str) -> str:
    db = getattr(driver, "database", None)
    if db is None:
        return ""
    try:
        return db.get_setting(f"vlsi.core.{stage}_tool", nullvalue="") or ""
    except Exception:
        return ""


class _Covered:
    """Code another key holds: the framework allowlist, the selected tool's packages and the tech package."""

    def __init__(self, driver: Any, stage: str, tool_name: Optional[str]) -> None:
        self.driver = driver
        self.stage = stage
        self.tool_name = tool_name
        self.framework = {os.path.realpath(os.path.join(_HAMMER, rel)) for rel in _FRAMEWORK_FILES}
        self._packages: Optional[List[str]] = None

    def packages(self) -> List[str]:
        if self._packages is None:
            out: List[str] = []
            name = self.tool_name if self.tool_name is not None else _tool_setting(self.driver, self.stage)
            if name:
                try:
                    units, _ = _tool_units(importlib.import_module(name))
                except Exception:
                    units = {}
                for paths in units.values():
                    out.extend(os.path.realpath(p) for p in paths)
            tech = getattr(self.driver, "tech", None)
            if tech is not None:
                pkg = getattr(tech, "package", "") or type(tech).__module__
                out.extend(os.path.realpath(p) for p in _search_locations(sys.modules.get(pkg)))
            self._packages = out
        return self._packages

    def __contains__(self, path: str) -> bool:
        if path in self.framework:
            return True
        return any(path == p or path.startswith(p.rstrip(os.sep) + os.sep) for p in self.packages())


def _step_lines(func: Any, analyzed: Set[str], covered: _Covered, roots: Roots = ()) -> List[str]:
    """A step's identity, plus its source digest unless it is stdlib, analyzed, or held by another key."""
    func, lines = _unwrap(func, roots)
    ident = _ident(func)
    path = _code_path(func)
    if path is None or path in analyzed or (_isfile(path) and (_is_stdlib(path) or path in covered)):
        return lines + [ident]
    return lines + [f"{ident}={_function_digest(func, path)}"]


def _analyzable(path: Optional[str], hammer_ok: bool, own: bool) -> Optional[str]:
    """The real path of a module to analyze; own code skips only the stdlib, followed imports skip site-packages."""
    if not path:
        return None
    real = os.path.realpath(path)
    if not _isfile(real) or (not own and not real.endswith(".py")):
        return None
    parts = _hammer_parts(real)
    if parts is not None:
        rel = "/".join(parts)
        if not hammer_ok or parts[0] in _INFRA_AREAS or rel in _FRAMEWORK_FILES or rel in _INFRA_FILES:
            return None
        return real
    if _is_stdlib(real) or (not own and _is_foreign(real)):
        return None
    return real


def _live_module(name: Optional[str], real: str) -> Optional[ModuleType]:
    mod = sys.modules.get(name or "")
    file = getattr(mod, "__file__", None)
    return mod if file and os.path.realpath(file) == real else None


def _resolve_import(name: str) -> Tuple[Optional[str], Optional[ModuleType]]:
    """Where a module is or would be loaded from, found without importing anything."""
    mod = sys.modules.get(name)
    if mod is not None:
        return getattr(mod, "__file__", None), mod
    parts = name.split(".")
    spec = None
    locs: List[str] = []
    for i in range(len(parts)):
        prefix = ".".join(parts[:i + 1])
        live = sys.modules.get(prefix)
        if live is not None:
            locs = list(getattr(live, "__path__", None) or [])
            continue
        if i and not locs:
            return None, None
        try:
            if i:
                spec = importlib.machinery.PathFinder.find_spec(prefix, locs)
            else:
                spec = importlib.util.find_spec(prefix)
        except Exception:
            return None, None
        if spec is None:
            return None, None
        locs = list(spec.submodule_search_locations or [])
    if spec is None or not spec.has_location or not spec.origin:
        return None, None
    return spec.origin, None


def _analyzed_modules(driver: Any, cli: Any, user_funcs: Iterable[Any]) -> List[_HookModule]:
    """Tech MRO modules outside the framework, non-hammer CLI driver modules, user step modules, and their imports."""
    found: Dict[str, _HookModule] = {}
    order: List[str] = []

    def add(path: Optional[str], name: Optional[str], hammer_ok: bool, own: bool) -> None:
        real = _analyzable(path, hammer_ok, own)
        if real is not None and real not in found:
            found[real] = _HookModule(real, _live_module(name, real), name)
            order.append(real)

    tech = getattr(driver, "tech", None) if driver is not None else None
    for klass in (type(tech).__mro__ if tech is not None else ()):
        add(getattr(sys.modules.get(klass.__module__), "__file__", None), klass.__module__, True, True)
    for klass in (type(cli).__mro__ if cli is not None else ()):
        add(getattr(sys.modules.get(klass.__module__), "__file__", None), klass.__module__, False, True)
    for func in user_funcs:
        func, _ = _unwrap(func)
        add(_code_path(func), getattr(func, "__module__", None), False, True)
    i = 0
    while i < len(order):
        mod = found[order[i]]
        i += 1
        if mod.tree is None:
            continue
        for name in sorted(_tree_imports(mod.tree, mod.package())):
            path, live = _resolve_import(name)
            add(path, getattr(live, "__name__", name), False, False)
    return [found[p] for p in sorted(found)]


def hooks_lines(driver: Any, cli: Any, stage_tag: str, tech_hooks: Optional[Iterable[Any]],
                user_hooks: Optional[Iterable[Any]], tool_name: Optional[str] = None) -> List[str]:
    stage = _STAGE_ALIASES.get(stage_tag, stage_tag)
    sources = (("tech", list(tech_hooks or [])), ("user", list(user_hooks or [])))
    lines = []
    for src, actions in sources:
        for i, action in enumerate(actions):
            step = action.step
            loc = getattr(action.location, "name", str(action.location))
            lines.append(f"id|{src}|{i:04d}|{loc}|{action.target_name}|{step.name if step is not None else ''}")
    mods = _analyzed_modules(driver, cli, [a.step.func for a in sources[1][1] if a.step is not None])
    analyzed = {m.path for m in mods}
    extra: Set[str] = set()
    for _, actions in sources:
        for action in actions:
            if action.step is None:
                continue
            func, _ = _unwrap(action.step.func)
            if _code_path(func) in analyzed:
                head = (getattr(func, "__qualname__", "") or "").split(".<locals>.")[0]
                extra.add(head.rsplit(".", 1)[-1])
    analysis = _HookAnalysis(mods, _base_names())
    kept = analysis.kept(stage, extra)
    roots = path_roots(driver) if driver is not None else []
    lines.extend(analysis.module_lines(kept))
    lines.extend(_literal_lines(analysis, kept, roots, _config_files(driver)))
    covered = _Covered(driver, stage, tool_name)
    for src, actions in sources:
        for i, action in enumerate(actions):
            if action.step is not None:
                step_lines = _step_lines(action.step.func, analyzed, covered, roots)
                lines.extend(f"step|{src}|{i:04d}|{line}" for line in step_lines)
    return lines


def hooks_fingerprint(driver: Any, cli: Any, stage_tag: str, tech_hooks: Optional[Iterable[Any]],
                      user_hooks: Optional[Iterable[Any]], tool_name: Optional[str] = None) -> str:
    """<stage>.hooks_fingerprint_sha256: hook identities, this stage's share of hook code, and literal files."""
    stage = _STAGE_ALIASES.get(stage_tag, stage_tag)
    lines = hooks_lines(driver, cli, stage, tech_hooks, user_hooks, tool_name)
    debug_record(f"{stage}.hooks", lines)
    return digest_lines(lines)


_MEMO_VERSION = _source_version(__file__)

try:
    _take_snapshot(_HAMMER, _FRAMEWORK_FILES)
except Exception:
    pass
