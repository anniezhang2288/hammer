"""
Fingerprints of what a stage's result depends on beyond its config values: the files the config,
libraries and decks name, inputs in the build dir, and tool, hook and framework code.

Every file becomes one line. A file under a content root (the tech cache, the build dir, the
hammer package, the directory the action runs from) is named by a portable token and hashed by
content, so the same bytes in another build dir, or in a partner's pulled copy of the design, give
the same line and a rewrite with identical bytes changes nothing; one too big to
hash gives its size and whole-second mtime, which a PD cache restore keeps. Any other file is
hashed by path, size, mtime and ctime, which keeps multi-GB PDK files cheap.
"""
from __future__ import annotations

import contextlib
import errno
import functools
import hashlib
import importlib
import json
import os
import re
import stat as stat_mod
import sys
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Set,
                    Tuple)

from pydantic import ValidationError

from hammer.config.config_src import RUN_CONTROL_KEYS, StageGraph, load_config_from_defaults

CONTENT_HASH_LIMIT = 256 * 1024 * 1024
DIR_WALK_CAP = 5000
DEBUG_ENV = "HAMMER_PD_COLLAT_DEBUG"

OWNED_STAGE_TAGS: Tuple[str, ...] = tuple(StageGraph().stageTagTuple)
GLOBAL_SCOPE = "vlsi"
SPICE_FIELDS = frozenset({"spice_file", "spice_model_file"})
EXTRA_COLLATERAL_SUFFIXES = (".spice", ".pvl", ".sdc", ".tcl", ".io", ".map", ".yml", ".yaml")
RTL_KEY = "synthesis.inputs.input_files"
ILM_KEY = "vlsi.inputs.ilms"
ILM_OUTPUT_KEY = "par.outputs.output_ilms"
NARROWED_SCOPE = "vlsi.narrowed"
LIB_DIR_FIELDS = frozenset({"milkyway_lib_in_dir", "power_grid_library"})
LIB_NAMED_FIELDS = frozenset({"verilog_sim", "verilog_synth"}) | LIB_DIR_FIELDS
RTL_INCLUDE_SUFFIXES = (".v", ".sv", ".vh", ".svh", ".h", ".inc", ".vinc", ".svi")

Roots = Sequence[Tuple[str, str]]

_MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG, errno.ELOOP})
_SKIP_SUFFIXES = (".swp", ".swo", ".swx", ".orig", ".rej", ".bak", ".tmp", ".pyc", ".md", ".rst")
_TMP_RE = re.compile(r"\.tmp\.\d+\.\d+$")
PATH_SEPARATORS = r"[\s\"'{}\[\]();,=<>|]+"
_TOKEN_SPLIT_RE = re.compile(PATH_SEPARATORS)
_MEMO_ATTR = "_fingerprint_memo"
_UNRESOLVABLE = (AssertionError, ValueError, KeyError, TypeError, ValidationError)
_BESIDE_DECK_SKIP = (".log", ".json")
_UNKNOWN = object()


def hammer_dir() -> str:
    """The hammer package directory; hammer is a namespace package, so hammer.__file__ is None."""
    import hammer.vlsi
    return str(Path(hammer.vlsi.__file__).resolve().parent.parent)


def _forms(path: str) -> Tuple[str, str]:
    return os.path.normpath(os.path.abspath(path)), os.path.realpath(path)


def make_roots(tech_cache: Optional[str], obj_dir: Optional[str],
               hammer_root: Optional[str] = None, work_dir: Optional[str] = None) -> List[Tuple[str, str]]:
    """(prefix ending in a separator, token) pairs in normpath and realpath form, longest first.

    work_dir, the directory the action runs from (make's and the DAG's working directory), is a root
    too, so a partner who pulls the design into a directory of their own names its RTL, configs and
    collateral by the same tokens and hits the same cache entries. It is left out when it is /, the
    home directory, or inside another root, where it would hide that root's token.
    """
    pairs = set()
    for path, token in ((tech_cache, "<TECH_CACHE>"), (obj_dir, "<OBJ_DIR>"),
                        (hammer_root if hammer_root is not None else hammer_dir(), "<HAMMER>")):
        if not path:
            continue
        for form in _forms(path):
            pairs.add((form.rstrip(os.sep) + os.sep, token))
    if work_dir:
        forms = _forms(work_dir)
        too_broad = {os.sep} | set(_forms(os.path.expanduser("~")))
        inside = any((form.rstrip(os.sep) + os.sep).startswith(prefix) for form in forms for prefix, _ in pairs)
        if not inside and not too_broad.intersection(forms):
            for form in forms:
                pairs.add((form.rstrip(os.sep) + os.sep, "<WORK_DIR>"))
    return sorted(pairs, key=lambda p: (-len(p[0]), p[0]))


def path_roots(driver: Any) -> List[Tuple[str, str]]:
    tech_cache = None
    tech = getattr(driver, "tech", None)
    if tech is not None:
        try:
            tech_cache = tech.cache_dir
        except (ValueError, AttributeError):
            tech_cache = None
    try:
        work_dir: Optional[str] = os.getcwd()
    except OSError:
        work_dir = None
    return make_roots(tech_cache, getattr(driver, "obj_dir", None), work_dir=work_dir)


def _match(path: str, roots: Roots) -> Optional[str]:
    for prefix, token in roots:
        if path.startswith(prefix):
            return token + "/" + path[len(prefix):].replace(os.sep, "/")
    return None


def tokenize(path: str, roots: Roots, resolve: bool = True) -> Optional[str]:
    """The tokenized form of path if it (or, failing that and with resolve, its realpath) is under a root,
    else None."""
    if not roots:
        return None
    norm = os.path.normpath(os.path.abspath(path))
    hit = _match(norm, roots)
    if hit is None and resolve:
        try:
            real = os.path.realpath(norm)
        except (OSError, ValueError):
            return None
        if real != norm:
            hit = _match(real, roots)
    return hit


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_line(path: str, roots: Roots = (), memo: Optional[Dict[str, str]] = None) -> str:
    """One fingerprint line for path; memo caches lines (and so stats) within one computation."""
    if memo is not None and path in memo:
        return memo[path]
    line = _file_line(path, roots)
    if memo is not None:
        memo[path] = line
    return line


def _file_line(path: str, roots: Roots) -> str:
    tok = tokenize(path, roots)
    shown = tok if tok is not None else os.path.normpath(path)
    try:
        st = os.stat(path)
    except ValueError:
        return f"MISSING:{shown}"
    except PermissionError:
        return f"UNREADABLE:{shown}"
    except OSError as e:
        if e.errno in _MISSING_ERRNOS:
            return f"MISSING:{shown}"
        raise
    if not stat_mod.S_ISREG(st.st_mode):
        return f"NOTFILE:{shown}"
    if tok is None:
        return f"{shown}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
    if st.st_size > CONTENT_HASH_LIMIT:
        return f"{tok}:{st.st_size}:{st.st_mtime_ns // 10**9}"
    try:
        return f"{tok}:sha256={sha256_file(path)}"
    except PermissionError:
        return f"UNREADABLE:{tok}:{st.st_size}:{st.st_mtime_ns // 10**9}"


def skip_name(name: str) -> bool:
    """Editor, VCS, patch and documentation artefacts that never feed a tool."""
    return (name.startswith(".") or name.endswith("~") or (name.startswith("#") and name.endswith("#"))
            or name.lower().endswith(_SKIP_SUFFIXES) or bool(_TMP_RE.search(name))
            or name.startswith("README") or name == "__pycache__")


def walk_dir(path: str, roots: Roots = (), recursive: bool = True, cap: int = DIR_WALK_CAP,
             memo: Optional[Dict[str, str]] = None, skip: Optional[Callable[[str], bool]] = None,
             prune: Optional[Callable[[str], bool]] = None) -> List[str]:
    """Lines for the regular files in a directory; past cap files, one DIRSTAT summary line instead.

    skip drops further file names on top of skip_name, and prune drops subdirectories by path.
    """
    if not os.path.isdir(path):
        return [file_line(path, roots, memo)]
    found: List[Tuple[str, os.stat_result]] = []
    # Follow symlinked subdirectories (a generated-src or shared include tree is
    # often linked in), visiting each real directory once so a link loop ends.
    seen = {os.path.realpath(path)}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=True):
        kept = []
        for d in sorted(dirnames):
            full_dir = os.path.join(dirpath, d)
            if skip_name(d) or (prune is not None and prune(full_dir)):
                continue
            real = os.path.realpath(full_dir)
            if real not in seen:
                seen.add(real)
                kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            if skip_name(name) or (skip is not None and skip(name)):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except (OSError, ValueError):
                continue
            if stat_mod.S_ISREG(st.st_mode):
                found.append((full, st))
        if not recursive:
            break
    if len(found) > cap:
        tok = tokenize(path, roots)
        size = sum(st.st_size for _, st in found)
        newest = max(st.st_mtime_ns for _, st in found)
        if tok is not None:
            return [f"DIRSTAT:{tok}:{len(found)}:{newest // 10**9}:{size}"]
        return [f"DIRSTAT:{os.path.normpath(path)}:{len(found)}:{newest}:"
                f"{max(st.st_ctime_ns for _, st in found)}:{size}"]
    return [file_line(full, roots, memo) for full, _ in found]


def digest_lines(lines: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8", "surrogatepass")).hexdigest()


_DEBUG_WARNED: Set[str] = set()


def debug_record(scope: str, lines: Iterable[str]) -> None:
    """With $HAMMER_PD_COLLAT_DEBUG set, append '<scope>\\t<line>' records to that file; a failed write only
    warns."""
    out = os.environ.get(DEBUG_ENV)
    if not out:
        return
    try:
        with open(out, "a", encoding="utf-8", errors="surrogateescape") as f:
            for line in sorted(lines):
                f.write(f"{scope}\t{line}\n")
    except (OSError, UnicodeError) as e:
        if out not in _DEBUG_WARNED:
            _DEBUG_WARNED.add(out)
            print(f"fingerprints: cannot write the ${DEBUG_ENV} file {out}: {e}", file=sys.stderr)


def begin_action_memo(driver: Any) -> Dict[str, str]:
    """A fresh line memo for one action; the PD cache key of that action reuses it."""
    memo: Dict[str, str] = {}
    setattr(driver, _MEMO_ATTR, memo)
    return memo


def action_memo(driver: Any) -> Optional[Dict[str, str]]:
    memo = getattr(driver, _MEMO_ATTR, None)
    return memo if isinstance(memo, dict) else None


def collateral_suffixes() -> Tuple[str, ...]:
    """Suffixes that make a missing config path count as a file."""
    from hammer.vlsi.pd_store import COLLATERAL_EXTENSIONS
    return tuple(dict.fromkeys(COLLATERAL_EXTENSIONS + EXTRA_COLLATERAL_SUFFIXES))


def owner(key: str) -> str:
    """The stage tag whose dotted prefix the key has, else the global scope."""
    head, dot, _ = key.partition(".")
    return head if dot and head in OWNED_STAGE_TAGS else GLOBAL_SCOPE


def upstream_scope(stage_tag: str) -> str:
    return stage_tag + ".upstream"


def skip_key(key: str) -> bool:
    """Keys whose values never name a stage input: outputs, run bookkeeping, fingerprints and RTL."""
    parts = key.split(".")
    return ((len(parts) > 1 and parts[1] == "outputs") or key.endswith(".needsToRerun")
            or key.endswith("fingerprint_sha256") or key in RUN_CONTROL_KEYS or key == RTL_KEY)


def path_candidates(text: str) -> List[str]:
    """The whole string if it is absolute, plus every absolute path token inside it."""
    found = [text] if os.path.isabs(text) else []
    if "/" in text or os.sep in text:
        found += [t for t in _TOKEN_SPLIT_RE.split(text) if t and t != text and os.path.isabs(t)]
    return list(dict.fromkeys(found))


def tool_packages(stage_tag: str) -> List[str]:
    """Tool plugins installed for a stage, across every portion of the hammer.<stage> namespace."""
    try:
        pkg = importlib.import_module(f"hammer.{stage_tag}")
    except ImportError:
        return []
    names: Set[str] = set()
    for base in list(getattr(pkg, "__path__", [])):
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        names.update(n for n in entries
                     if n.isidentifier() and os.path.isfile(os.path.join(base, n, "__init__.py")))
    return sorted(names)


def selected_tool(db: Dict[str, Any], stage_tag: str) -> str:
    """The leaf name of vlsi.core.<stage>_tool, or '' when no tool is selected."""
    selected = db.get(f"vlsi.core.{stage_tag}_tool")
    return selected.split(".")[-1] if isinstance(selected, str) else ""


def unselected_tool_prefixes(db: Dict[str, Any], stage_tag: str) -> Tuple[str, ...]:
    """'<stage>.<tool>.' for every installed tool of the stage except the selected one."""
    leaf = selected_tool(db, stage_tag)
    if not leaf:
        return ()
    return tuple(f"{stage_tag}.{name}." for name in tool_packages(stage_tag) if name != leaf)


def _strings(value: Any, spice: bool) -> Iterator[Tuple[str, bool]]:
    if isinstance(value, str):
        yield value, spice
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, spice or k in SPICE_FIELDS)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, spice)


def _string_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _dir_prefixes(path: Optional[str]) -> Tuple[str, ...]:
    if not path:
        return ()
    return tuple({_norm(path).rstrip(os.sep) + os.sep, os.path.realpath(path).rstrip(os.sep) + os.sep})


def _unreadable(path: str, roots: Roots) -> str:
    return f"UNREADABLE:{tokenize(path, roots) or os.path.normpath(path)}"


def _candidate_line(path: str, roots: Roots, memo: Dict[str, str],
                    suffixes: Tuple[str, ...]) -> Optional[str]:
    try:
        line = file_line(path, roots, memo)
    except (OSError, ValueError):
        line = _unreadable(path, roots)
    if line.startswith("NOTFILE:"):
        return None
    if line.startswith(("MISSING:", "UNREADABLE:")) and not path.lower().endswith(suffixes):
        return None
    return line


def _in_obj_dir(line: str) -> bool:
    for prefix in ("MISSING:", "UNREADABLE:"):
        if line.startswith(prefix):
            line = line[len(prefix):]
            break
    return line.startswith("<OBJ_DIR>/")


def _walk_lines(path: str, roots: Roots, memo: Dict[str, str]) -> List[str]:
    try:
        return walk_dir(path, roots, recursive=True, cap=DIR_WALK_CAP, memo=memo)
    except (OSError, ValueError):
        return [_unreadable(path, roots)]


def include_dir_lines(dirs: Iterable[str], roots: Roots = (), memo: Optional[Dict[str, str]] = None) -> List[str]:
    """Lines for the Verilog sources and headers under each include dir, a relative one taken from the cwd as
    slang takes it, without entering the build dir, the tech cache or a '*-rundir' directory."""
    memo = {} if memo is None else memo
    blocked = {prefix for prefix, token in roots if token in ("<TECH_CACHE>", "<OBJ_DIR>")}

    def prune(path: str) -> bool:
        return os.path.basename(path).endswith("-rundir") or bool(set(_dir_prefixes(path)) & blocked)

    def skip(name: str) -> bool:
        return not name.lower().endswith(RTL_INCLUDE_SUFFIXES)

    found: Set[str] = set()
    for d in dirs:
        if not isinstance(d, str) or not d:
            continue
        path = os.path.abspath(d)
        try:
            found.update(walk_dir(path, roots, recursive=True, memo=memo, skip=skip, prune=prune))
        except (OSError, ValueError):
            found.add(_unreadable(path, roots))
    return sorted(found)


def _ilm_dirs(value: Any) -> List[str]:
    """The dir and data_dir of each ILM, minus any nested inside another one."""
    dirs = {ilm[f] for ilm in (value if isinstance(value, list) else []) if isinstance(ilm, dict)
            for f in ("dir", "data_dir") if isinstance(ilm.get(f), str) and os.path.isabs(ilm[f])}
    kept: List[str] = []
    for d in sorted(dirs, key=lambda p: (len(_norm(p)), p)):
        n = _norm(d)
        if not any(n == _norm(k) or n.startswith(_norm(k).rstrip(os.sep) + os.sep) for k in kept):
            kept.append(d)
    return kept


def _config_entries(db: Dict[str, Any], roots: Roots, stage_tag: Optional[str],
                    rtl_set: Optional[Iterable[str]], memo: Dict[str, str], own_rundir: Optional[str],
                    upstream_only: bool) -> Iterator[Tuple[str, str]]:
    suffixes = collateral_suffixes()
    rtl = {_norm(p) for p in (_string_list(db.get(RTL_KEY)) if rtl_set is None else rtl_set)}
    own_prefixes = _dir_prefixes(own_rundir) if stage_tag else ()
    skip_tools = unselected_tool_prefixes(db, stage_tag) if stage_tag else ()
    for key, value in db.items():
        if skip_key(key) or (upstream_only and not key.startswith(f"{stage_tag}.inputs.")):
            continue
        if skip_tools and key.startswith(skip_tools):
            continue
        own = owner(key)
        inputs = own != GLOBAL_SCOPE and key.startswith(own + ".inputs.")
        for text, spice in _strings(value, key.rpartition(".")[2] in SPICE_FIELDS):
            base = "lvs" if spice else own
            if stage_tag is not None and base not in (GLOBAL_SCOPE, stage_tag):
                continue
            for path in path_candidates(text):
                norm = _norm(path)
                if norm in rtl or (own_prefixes and base == stage_tag and norm.startswith(own_prefixes)):
                    continue
                line = _candidate_line(path, roots, memo, suffixes)
                if line is None:
                    continue
                scope = upstream_scope(own) if inputs and base == own and _in_obj_dir(line) else base
                if not upstream_only or scope == upstream_scope(own):
                    yield scope, line
        if key == ILM_KEY and not upstream_only:
            for d in _ilm_dirs(value):
                for line in _walk_lines(d, roots, memo):
                    yield GLOBAL_SCOPE, line


def config_lines(db: Dict[str, Any], roots: Roots, stage_tag: Optional[str] = None,
                 rtl_set: Optional[Iterable[str]] = None, memo: Optional[Dict[str, str]] = None,
                 own_rundir: Optional[str] = None,
                 rtl_include_dirs: Sequence[str] = ()) -> Dict[str, List[str]]:
    """Lines for the files the flattened config names, keyed by scope.

    Scopes are 'vlsi' (keys no stage owns), each stage tag (its dotted keys, and for 'lvs' every
    spice_file or spice_model_file value) and '<tag>.upstream' (build-dir files named by
    '<tag>.inputs.*'). With stage_tag set, only 'vlsi', that tag and its upstream are computed,
    and files under own_rundir stay out of the stage's own scopes. rtl_set defaults to
    synthesis.inputs.input_files; include_dir_lines of rtl_include_dirs go to 'vlsi' for an RTL
    byte hash that cannot follow `include.
    """
    if stage_tag is not None and stage_tag not in OWNED_STAGE_TAGS:
        raise ValueError(f"Unknown stage tag {stage_tag!r}. Expected one of {OWNED_STAGE_TAGS}.")
    memo = {} if memo is None else memo
    found: Dict[str, Set[str]] = {GLOBAL_SCOPE: set()}
    for tag in (OWNED_STAGE_TAGS if stage_tag is None else (stage_tag,)):
        found[tag] = set()
        found[upstream_scope(tag)] = set()
    for scope, line in _config_entries(db, roots, stage_tag, rtl_set, memo, own_rundir, False):
        found[scope].add(line)
    found[GLOBAL_SCOPE].update(include_dir_lines(rtl_include_dirs, roots, memo))
    return {scope: sorted(lines) for scope, lines in found.items()}


def upstream_lines(db: Dict[str, Any], roots: Roots, stage_tag: str,
                   rtl_set: Optional[Iterable[str]] = None, memo: Optional[Dict[str, str]] = None,
                   own_rundir: Optional[str] = None) -> List[str]:
    """The '<stage_tag>.upstream' lines of config_lines, reading only '<stage_tag>.inputs.*'; none for a
    tag no stage owns."""
    if stage_tag not in OWNED_STAGE_TAGS:
        return []
    memo = {} if memo is None else memo
    return sorted({line for _, line in _config_entries(db, roots, stage_tag, rtl_set, memo,
                                                         own_rundir, True)})


def merge_scopes(*parts: Mapping[str, Iterable[str]]) -> Dict[str, List[str]]:
    """The union of scope -> lines maps; the deck files under 'vlsi.narrowed' leave the global scope."""
    merged: Dict[str, Set[str]] = {}
    for part in parts:
        for scope, lines in part.items():
            merged.setdefault(scope, set()).update(lines)
    narrowed = merged.pop(NARROWED_SCOPE, set())
    if GLOBAL_SCOPE in merged:
        merged[GLOBAL_SCOPE] -= narrowed
    return {scope: sorted(lines) for scope, lines in merged.items()}


def tokenize_value(value: Any, roots: Roots, resolve: bool = True) -> Any:
    """value with every content root inside its strings replaced by the root's token; resolve also follows
    the symlinks of a string that is one whole path."""
    if isinstance(value, dict):
        return {k: tokenize_value(v, roots, resolve) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tokenize_value(v, roots, resolve) for v in value]
    if isinstance(value, str) and roots:
        if os.path.isabs(value):
            tok = tokenize(value, roots, resolve)
            if tok is not None:
                return tok
        for prefix, token in roots:
            if value + os.sep == prefix:
                return token
            if prefix in value:
                value = value.replace(prefix, token + "/")
    return value


def _key_line(key: str, db: Dict[str, Any], roots: Roots) -> str:
    if key not in db:
        return f"KEY:{key}=<absent>"
    return f"KEY:{key}=" + json.dumps(tokenize_value(db[key], roots), sort_keys=True, default=str)


def _text_lines(text: Any, roots: Roots, memo: Dict[str, str], exclude: Tuple[str, ...] = ()) -> Set[str]:
    """Lines for the absolute paths in text that count as files, as for a config string."""
    found: Set[str] = set()
    if not isinstance(text, str):
        return found
    suffixes = collateral_suffixes()
    for path in path_candidates(text):
        if exclude and _norm(path).startswith(exclude):
            continue
        line = _candidate_line(path, roots, memo, suffixes)
        if line is not None:
            found.add(line)
    return found


def _resolve(tech: Any, raw: str, lib: Any = None) -> Optional[str]:
    try:
        return tech.prepend_dir_path(raw, lib)
    except _UNRESOLVABLE:
        return None


def _library_paths(lib: Any, models: Tuple[type, type]) -> Iterator[Tuple[str, str]]:
    """(field, raw path) for each path field of a Library, the spice model path and min/max pairs included."""
    spice_model, min_max = models
    for name in type(lib).model_fields:
        value = getattr(lib, name, None)
        if isinstance(value, spice_model):
            raws: List[Any] = [value.path]
        elif isinstance(value, min_max):
            raws = [value.max_cap, value.min_cap]
        elif isinstance(value, str) and ("file" in name or "path" in name or name in LIB_NAMED_FIELDS):
            raws = [value]
        else:
            continue
        for raw in raws:
            if isinstance(raw, str) and raw:
                yield name, raw


def library_lines(tech: Any, roots: Roots = (), memo: Optional[Dict[str, str]] = None,
                  stage_tag: Optional[str] = None) -> Dict[str, List[str]]:
    """Lines for every path field of every library the tech offers, keyed by scope.

    The libraries are the tech's own plus vlsi.technology.extra_libraries. Spice netlists and
    models go to 'lvs' and every other field to 'vlsi'; with stage_tag set to another stage the
    spice fields are left out. A raw path is resolved once per set of library prefixes, and one
    that cannot be resolved gives 'UNRESOLVED:<raw>'. Directory fields are walked.
    """
    from hammer.tech import MinMaxCap, SpiceModelFile
    memo = {} if memo is None else memo
    found: Dict[str, Set[str]] = {GLOBAL_SCOPE: set()}
    if stage_tag in (None, "lvs"):
        found["lvs"] = set()
    resolved: Dict[Tuple[Any, ...], Optional[str]] = {}
    done: Set[Tuple[Any, ...]] = set()
    for lib in (tech.get_available_libraries() if tech is not None else []):
        prefixes = tuple((p.id, p.path) for p in (lib.extra_prefixes or []))
        for name, raw in _library_paths(lib, (SpiceModelFile, MinMaxCap)):
            scope = "lvs" if name in SPICE_FIELDS else GLOBAL_SCOPE
            walk = name in LIB_DIR_FIELDS
            if scope not in found or (raw, prefixes, scope, walk) in done:
                continue
            done.add((raw, prefixes, scope, walk))
            if (raw, prefixes) not in resolved:
                resolved[(raw, prefixes)] = _resolve(tech, raw, lib)
            path = resolved[(raw, prefixes)]
            if path is None:
                found[scope].add(f"UNRESOLVED:{raw}")
            elif walk and os.path.isdir(path):
                found[scope].update(_walk_lines(path, roots, memo))
            else:
                found[scope].add(file_line(path, roots, memo))
    return {scope: sorted(lines) for scope, lines in found.items()}


def _beside_deck_skip(name: str) -> bool:
    return name.lower().endswith(_BESIDE_DECK_SKIP)


def _prefix_forms(paths: Iterable[str]) -> Set[str]:
    return {form for path in paths for form in _dir_prefixes(path)}


def sibling_lines(path: str, roots: Roots = (), memo: Optional[Dict[str, str]] = None,
                  covered: Iterable[str] = ()) -> List[str]:
    """Lines for the regular files directly beside path, without logs, JSON or editor artefacts.

    Nothing is listed when that directory is the tech-cache root or the build dir, which tools
    write into, or lies inside a covered directory that a recursive walk already reads.
    """
    directory = os.path.dirname(_norm(path))
    forms = set(_dir_prefixes(directory))
    blocked = {prefix for prefix, token in roots if token in ("<TECH_CACHE>", "<OBJ_DIR>")}
    covered_forms = _prefix_forms(covered)
    if (not os.path.isdir(directory) or forms & blocked
            or any(form.startswith(c) for form in forms for c in covered_forms)):
        return []
    try:
        return walk_dir(directory, roots, recursive=False, memo=memo, skip=_beside_deck_skip)
    except (OSError, ValueError):
        return [_unreadable(directory, roots)]


def _decks(tech: Any, kind: str, tool_names: Iterable[str]) -> List[Tuple[str, Optional[str]]]:
    """(raw, resolved path or None) for the tech's drc or lvs decks of the given tools; deck.path is never changed."""
    names = {n for n in tool_names if n}
    decks = getattr(tech.config, f"{kind}_decks", None) or []
    return [(deck.path, _resolve(tech, deck.path)) for deck in decks
            if deck.tool_name in names and isinstance(deck.path, str) and deck.path]


def deck_lines(tech: Any, db: Dict[str, Any], stage_tag: Optional[str] = None, roots: Roots = (),
               memo: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
    """Lines for the selected tools' rule decks and the tech gds map, keyed by scope.

    DRC decks go to 'drc', and also to 'lvs' when netgen LVS loads the DRC deck as its magic tech
    file (drc.magic.rcfile unset); LVS decks go to 'lvs'. Each deck brings the files beside it,
    magic DRC brings the files beside its rcfile, and the tech's additional DRC or LVS text brings
    the absolute paths inside it. The tech gds map goes to 'par' unless par.inputs.gds_map_mode
    is manual or empty. 'vlsi.narrowed' holds the deck files themselves, for merge_scopes to drop
    from the global scope; it does not depend on stage_tag, so every action narrows alike.
    Siblings inside a _DIR_INPUTS directory are left to reader_lines.
    """
    memo = {} if memo is None else memo
    stages = ("drc", "lvs", "par") if stage_tag is None else (stage_tag,)
    found: Dict[str, Set[str]] = {s: set() for s in stages if s in ("drc", "lvs", "par")}
    found[NARROWED_SCOPE] = set()
    if tech is None or getattr(tech, "config", None) is None:
        return {scope: [] for scope in found}
    drc_tool = selected_tool(db, "drc")
    lvs_tool = selected_tool(db, "lvs")
    decks = {"drc": _decks(tech, "drc", (drc_tool,)), "lvs": _decks(tech, "lvs", (lvs_tool,))}
    if lvs_tool == "netgen" and not db.get("drc.magic.rcfile"):
        decks["lvs"] += _decks(tech, "drc", (drc_tool, str(db.get("vlsi.core.drc_tool") or "")))
    found[NARROWED_SCOPE].update(file_line(path, roots, memo)
                                 for scope_decks in decks.values() for _, path in scope_decks if path is not None)
    for scope in ("drc", "lvs"):
        if scope not in found:
            continue
        covered = dir_input_paths(db, scope)
        for raw, path in decks[scope]:
            if path is None:
                found[scope].add(f"UNRESOLVED:{raw}")
                continue
            found[scope].add(file_line(path, roots, memo))
            found[scope].update(sibling_lines(path, roots, memo, covered))
        if db.get(f"{scope}.inputs.additional_{scope}_text_mode") != "manual":
            found[scope].update(_text_lines(getattr(tech.config, f"additional_{scope}_text", None), roots, memo))
        rcfile = db.get("drc.magic.rcfile")
        if scope == "drc" and drc_tool == "magic" and isinstance(rcfile, str) and os.path.isabs(rcfile):
            found[scope].update(sibling_lines(rcfile, roots, memo, covered))
    gds_map = getattr(tech.config, "gds_map_file", None)
    if "par" in found and isinstance(gds_map, str) and gds_map \
            and db.get("par.inputs.gds_map_mode") not in ("manual", "empty"):
        path = _resolve(tech, gds_map)
        found["par"].add(f"UNRESOLVED:{gds_map}" if path is None else file_line(path, roots, memo))
    return {scope: sorted(lines) for scope, lines in found.items()}


class Reader(NamedTuple):
    """Settings outside the stage's own files that its selected tool reads; a key ending in '.' is a prefix."""
    stage: str
    tools: Tuple[str, ...]
    keys: Tuple[str, ...]
    siblings: Tuple[str, ...] = ()
    when: Optional[Callable[[Dict[str, Any]], bool]] = None


class DirInput(NamedTuple):
    """A setting naming directories the stage's selected tool reads; '{tech}' is the technology name."""
    stage: str
    tools: Tuple[str, ...]
    key: str


def _tool_default(db: Dict[str, Any], stage_tag: str, key: str) -> Any:
    """key in the selected tool's defaults, which load only with the tool, or _UNKNOWN."""
    try:
        configs, _ = load_config_from_defaults(str(db.get(f"vlsi.core.{stage_tag}_tool")))
    except Exception:
        return _UNKNOWN
    for config in reversed(configs):
        if key in config:
            return config[key]
    return _UNKNOWN


def _genus_physical(db: Dict[str, Any]) -> bool:
    key = "synthesis.genus.phys_flow_effort"
    effort = db[key] if key in db else _tool_default(db, "synthesis", key)
    return effort is _UNKNOWN or str(effort).lower() != "none"


_READERS: Tuple[Reader, ...] = (
    Reader("lvs", ("netgen",), ("drc.magic.magic_bin", "drc.magic.rcfile"), siblings=("drc.magic.rcfile",)),
    Reader("synthesis", ("genus",), ("par.innovus.innovus_bin",), when=_genus_physical),
    Reader("par", ("vivado",), ("synthesis.vivado.",)),
    Reader("synthesis", ("genus", "innovus_plus"), (ILM_OUTPUT_KEY,)),
    Reader("par", ("innovus", "mockpar", "openroad"), (ILM_OUTPUT_KEY,)),
    Reader("timing", ("tempus",), (ILM_OUTPUT_KEY,)),
)

_DIR_INPUTS: Tuple[DirInput, ...] = (
    DirInput("drc", ("pegasus",), "technology.{tech}.pegasus_drc_rules_dir"),
    DirInput("drc", ("icv",), "drc.icv.include_dirs"),
    DirInput("lvs", ("icv",), "lvs.icv.include_dirs"),
    DirInput("synthesis", ("vivado",), "synthesis.vivado.board_files"),
    DirInput("synthesis", ("vivado",), "synthesis.vivado.dcp_macro_dir"),
    DirInput("par", ("vivado",), "synthesis.vivado.board_files"),
    DirInput("par", ("vivado",), "synthesis.vivado.dcp_macro_dir"),
)


def dir_input_paths(db: Dict[str, Any], stage_tag: str) -> List[str]:
    """The absolute directories _DIR_INPUTS gives the stage's selected tool."""
    tool = selected_tool(db, stage_tag)
    tech = str(db.get("vlsi.core.technology") or "").split(".")[-1]
    paths: List[str] = []
    for entry in _DIR_INPUTS:
        if entry.stage == stage_tag and tool in entry.tools:
            paths += [p for p in _string_list(db.get(entry.key.format(tech=tech))) if os.path.isabs(p)]
    return list(dict.fromkeys(paths))


def _reader_keys(db: Dict[str, Any], keys: Iterable[str]) -> List[str]:
    out: List[str] = []
    for key in keys:
        out += sorted(k for k in db if k.startswith(key)) if key.endswith(".") else [key]
    return out


def reader_lines(db: Dict[str, Any], stage_tag: Optional[str] = None, roots: Roots = (),
                 memo: Optional[Dict[str, str]] = None, own_rundir: Optional[str] = None) -> Dict[str, List[str]]:
    """Lines for what a stage's selected tool reads beyond its own config files, keyed by stage.

    Each _READERS entry of the selected tool gives one 'KEY:<key>=<json>' line per setting
    ('<absent>' when unset, content roots tokenized), the files the values name, the dir and
    data_dir of ILM values, and the files beside its sibling-key values. Each _DIR_INPUTS
    directory of the selected tool is walked recursively. Files under own_rundir are left out.
    """
    if stage_tag is not None and stage_tag not in OWNED_STAGE_TAGS:
        raise ValueError(f"Unknown stage tag {stage_tag!r}. Expected one of {OWNED_STAGE_TAGS}.")
    memo = {} if memo is None else memo
    own = _dir_prefixes(own_rundir)
    found: Dict[str, Set[str]] = {s: set() for s in (OWNED_STAGE_TAGS if stage_tag is None else (stage_tag,))}
    for reader in _READERS:
        if reader.stage not in found or selected_tool(db, reader.stage) not in reader.tools:
            continue
        if reader.when is not None and not reader.when(db):
            continue
        lines = found[reader.stage]
        for key in _reader_keys(db, reader.keys):
            lines.add(_key_line(key, db, roots))
            for text, _ in _strings(db.get(key), False):
                lines.update(_text_lines(text, roots, memo, own))
            if key == ILM_OUTPUT_KEY:
                for d in _ilm_dirs(db.get(key)):
                    if not (own and (_norm(d).rstrip(os.sep) + os.sep).startswith(own)):
                        lines.update(_walk_lines(d, roots, memo))
        for key in reader.siblings:
            value = db.get(key)
            if isinstance(value, str) and os.path.isabs(value):
                lines.update(sibling_lines(value, roots, memo))
    for stage, lines in found.items():
        for d in dir_input_paths(db, stage):
            lines.update(_walk_lines(d, roots, memo))
    return {stage: sorted(lines) for stage, lines in found.items()}


def _sky130_lvs_primitives(func: Callable[..., Any], setting: Callable[[str], Any]) -> List[str]:
    """The primitive spice files pegasus_lvs_add_130a_primitives adds to the Pegasus LVS control file."""
    tech_class = sys.modules[func.__module__].SKY130Tech
    sky130a = setting("technology.sky130.sky130A")
    misc = setting("technology.sky130.misc_tapeout_collateral")
    paths = list(tech_class.sky130_sram_primitive_names(sky130a, misc))
    if misc:
        paths += tech_class.sky130_por_primitive_names(sky130a, misc)
    return list(dict.fromkeys(paths))


_HOOK_INPUTS: Dict[Tuple[str, str], Callable[[Callable[..., Any], Callable[[str], Any]], List[str]]] = {
    ("hammer.technology.sky130", "pegasus_lvs_add_130a_primitives"): _sky130_lvs_primitives,
}


def _step_function(action: Any) -> Any:
    step = getattr(action, "step", action)
    func = getattr(step, "func", step)
    while isinstance(func, functools.partial):
        func = func.func
    return func


def _setting_getter(driver: Any) -> Callable[[str], Any]:
    database = getattr(driver, "database", None)

    def get(key: str) -> Any:
        try:
            return database.get_setting(key)
        except KeyError:
            return None
    return get


def hook_input_lines(driver: Any, hooks: Mapping[str, Iterable[Any]], roots: Roots = (),
                     memo: Optional[Dict[str, str]] = None) -> Dict[str, List[str]]:
    """Lines for the files that _HOOK_INPUTS steps read by paths they build at run time, keyed by stage.

    hooks maps a stage tag ('syn' counts as 'synthesis') to its hook actions or steps; the files
    of a registered step go to the stage whose list holds it.
    """
    memo = {} if memo is None else memo
    get = _setting_getter(driver)
    found: Dict[str, Set[str]] = {}
    for stage, actions in hooks.items():
        lines = found.setdefault("synthesis" if stage == "syn" else stage, set())
        for action in actions or ():
            func = _step_function(action)
            inputs = _HOOK_INPUTS.get((getattr(func, "__module__", ""), getattr(func, "__name__", "")))
            if inputs is not None:
                lines.update(file_line(path, roots, memo) for path in inputs(func, get))
    return {stage: sorted(lines) for stage, lines in found.items()}


FINGERPRINT_STAGES = {"syn": "synthesis", "synthesis": "synthesis", "par": "par", "drc": "drc", "lvs": "lvs"}
_ACTION_NAMES = {"synthesis": "syn", "par": "par", "drc": "drc", "lvs": "lvs"}


class FingerprintError(RuntimeError):
    """A fingerprint could not be computed; the message names the setting or inputs and the cause."""


@contextlib.contextmanager
def _computing(what: str) -> Iterator[None]:
    try:
        yield
    except FingerprintError:
        raise
    except Exception as e:
        raise FingerprintError(f"{what}: {type(e).__name__}: {e}") from e


def _own_rundir(driver: Any, cli: Any, stage_tag: str) -> Optional[str]:
    name = _ACTION_NAMES[stage_tag]
    chosen = getattr(cli, f"{name}_rundir", None) if cli is not None else None
    if isinstance(chosen, str) and chosen:
        return chosen
    obj_dir = getattr(driver, "obj_dir", None)
    return os.path.join(obj_dir, f"{name}-rundir") if obj_dir else None


def stage_fingerprints(driver: Any, cli: Any, stage_tag: str, extra_hooks: Optional[Iterable[Any]] = None,
                       rtl_include_dirs: Sequence[str] = ()) -> Dict[str, str]:
    """The settings a syn, par, drc or lvs action stores before its dependency check; the vlsi.* values
    never depend on the action."""
    from hammer.vlsi import code_fingerprints
    tag = FINGERPRINT_STAGES.get(stage_tag)
    if tag is None:
        raise ValueError(f"No fingerprints for stage {stage_tag!r}. Expected one of {tuple(FINGERPRINT_STAGES)}.")
    memo = begin_action_memo(driver)
    tech = driver.tech
    own_rundir = _own_rundir(driver, cli, tag)
    with _computing("the configuration"):
        roots = path_roots(driver)
        db = json.loads(driver.database.get_database_json())
    with _computing("files the config names"):
        config = config_lines(db, roots, tag, memo=memo, own_rundir=own_rundir, rtl_include_dirs=rtl_include_dirs)
    with _computing("library files"):
        libraries = library_lines(tech, roots, memo, tag)
    with _computing("rule decks"):
        decks = deck_lines(tech, db, tag, roots, memo)
    with _computing("vlsi.tech_fingerprint_sha256"):
        tech_fp = code_fingerprints.tech_fingerprint(tech, roots)
    with _computing("vlsi.framework_fingerprint_sha256"):
        framework_fp = code_fingerprints.framework_fingerprint()
    tool = db.get(f"vlsi.core.{tag}_tool")
    tool = tool if isinstance(tool, str) else ""
    getter = f"get_tech_{_ACTION_NAMES[tag]}_hooks"
    with _computing(f"{type(tech).__name__}.{getter}"):
        tech_hooks = list(getattr(tech, getter)(tool.split(".")[-1]) or [])
    user_hooks = list(extra_hooks or [])
    with _computing(f"settings and directories the {tag} tool reads"):
        readers = reader_lines(db, tag, roots, memo, own_rundir)
    with _computing("files hook steps read"):
        hook_inputs = hook_input_lines(driver, {tag: tech_hooks + user_hooks}, roots, memo)
    scopes = merge_scopes(config, libraries, decks, readers, hook_inputs)
    out = {
        "vlsi.collateral_fingerprint_sha256": digest_lines(scopes.get(GLOBAL_SCOPE, [])),
        "vlsi.tech_fingerprint_sha256": tech_fp,
        "vlsi.framework_fingerprint_sha256": framework_fp,
        f"{tag}.collateral_fingerprint_sha256": digest_lines(scopes.get(tag, [])),
        f"{tag}.upstream_fingerprint_sha256": digest_lines(scopes.get(upstream_scope(tag), [])),
    }
    with _computing(f"{tag}.tool_fingerprint_sha256 ({tool or 'no tool'})"):
        out[f"{tag}.tool_fingerprint_sha256"] = code_fingerprints.tool_fingerprint(tool or None)
    with _computing(f"{tag}.hooks_fingerprint_sha256"):
        out[f"{tag}.hooks_fingerprint_sha256"] = code_fingerprints.hooks_fingerprint(
            driver, cli, tag, tech_hooks, user_hooks, tool or None)
    debug_record("vlsi.collateral", scopes.get(GLOBAL_SCOPE, []))
    debug_record(f"{tag}.collateral", scopes.get(tag, []))
    debug_record(upstream_scope(tag), scopes.get(upstream_scope(tag), []))
    debug_record("KEY", [f"{key}\t{value}" for key, value in out.items()])
    return out
