"""sledgehammer: the branded launcher + CLI for the SledgeHammer Airflow stack.

Running ``sledgehammer`` with no arguments brings the stack up through the
standalone launcher. A checkout's first ``scripts/uv_setup.sh`` makes the lite
stack its default; checkouts set up before that start the full stack. The UI
has one login, you, with the password in .sledgehammer/airflow-ui-password.json.
Any other arguments pass straight through to the Airflow CLI, with the database
secrets and AIRFLOW_HOME loaded first, so commands like ``sledgehammer db
migrate`` work without exporting anything by hand.

  sledgehammer                 launch this checkout's default stack
  sledgehammer standalone      same as above
  sledgehammer lite            scheduler, dag-processor and api-server, sized for a small VM
  sledgehammer full            adds the triggerer, live logs of running tasks and pgAdmin
  sledgehammer db migrate      run an airflow command with secrets loaded
  sledgehammer dags list       (any airflow subcommand works)

Flow commands -- hammer's flags, the DAG as plumbing, no GUI required:

  sledgehammer run syn par --obj_dir build/Top
      Trigger the selected stages on an already-registered DAG and stream
      task states until the run finishes. Exit code follows the run, so it
      drops into a Makefile exactly where hammer-vlsi did. Generate the DAG
      the usual way first: cd <vlsi dir> && make buildfile.
  sledgehammer run drc lvs --obj_dir build/Top
  sledgehammer run syn --obj_dir build/Top --no-wait
      Trigger and return immediately (prints the run id).
  cd vlsi && sledgehammer par-RocketTile
      The Makefile target names work verbatim: <stage>, <stage>-<module>,
      and redo-<stage>[-<module>]. Run from the vlsi directory and the
      DAG generated there is found with no flags; make never runs.
  sledgehammer par --force
      Rerun par even if nothing changed (redo-par is the same); stages before
      it still rerun only if they changed. --forceall reruns every stage the
      run executes.

  sledgehammer status --design Top          latest run, per-task table
  sledgehammer runs --design Top            recent runs

Which Postgres Airflow and the cache use:

  sledgehammer postgres show        where each points, and whether it answers
  sledgehammer postgres use local   switch both to your own local Postgres
  sledgehammer postgres configure   switch both to any other Postgres

Set SLEDGE_DRYRUN=1 to print what it would run instead of running it.
"""
import json
import os
import subprocess
import sys
import time

# hammer/shell/sledgehammer_cli.py -> repo root is three levels up.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO, "scripts", "airflow-standalone-ldap.py")
# First-arg values that mean "bring the server up" rather than an airflow passthrough.
LAUNCH_WORDS = {"", "standalone", "up", "start", "serve", "lite", "full"}
HELP_WORDS = {"-h", "--help", "help"}


def _venv_bin(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name)


def _load_secrets() -> None:
    """Decrypt the same GPG secrets the launcher uses into the environment, so
    airflow passthrough commands can reach the metadata DB. No-op if there's no
    secrets file, apart from one stderr line whenever no metadata DB connection
    ends up set, naming the airflow.cfg that Airflow then falls back to.
    """
    enc = os.path.expanduser(os.environ.get(
        "SLEDGE_SECRETS_FILE", os.path.join(REPO, ".sledgehammer", "airflow-secrets.env.gpg")))
    # reuse-already-loaded secrets: skip a redundant decrypt (and passphrase
    # prompt) when they're already exported into this environment.
    if os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
        return
    cfg = os.environ.get("AIRFLOW_CONFIG") or os.path.join(
        os.environ.get("AIRFLOW_HOME", REPO), "airflow.cfg")
    if not os.path.exists(enc):
        print(f"[sledgehammer] no secrets file at {enc} and AIRFLOW__DATABASE__SQL_ALCHEMY_CONN "
              f"is unset, so Airflow falls back to sql_alchemy_conn in {cfg}.", file=sys.stderr)
        return
    try:
        if sys.stdin.isatty():
            os.environ.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass
    attempts = 3
    res = None
    for attempt in range(1, attempts + 1):
        res = subprocess.run(
            ["gpg", "--quiet", "--no-symkey-cache", "--decrypt", enc],
            capture_output=True)
        if res.returncode == 0:
            break
        if attempt < attempts:
            print(f"[sledgehammer] that passphrase didn't work "
                  f"(attempt {attempt}/{attempts}) -- try again, or Ctrl-C to quit.")
        else:
            sys.stderr.write(res.stderr.decode("utf-8", "ignore"))
            sys.exit(f"[sledgehammer] could not decrypt secrets after {attempts} tries.")
    for raw in res.stdout.decode("utf-8", "ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val
    if not os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
        print(f"[sledgehammer] {enc} does not set AIRFLOW__DATABASE__SQL_ALCHEMY_CONN, "
              f"so Airflow falls back to sql_alchemy_conn in {cfg}.", file=sys.stderr)


# The stage names the generated DAGs accept as trigger-conf booleans.
_STAGES = ("sim_rtl", "power_rtl", "syn", "sim_syn", "timing_syn", "formal_syn",
           "power_syn", "par", "sim_par", "timing_par", "formal_par",
           "power_par", "drc", "lvs", "pcb")


def _shown(task_id):
    """Whether run streaming and status list a task: the stages (in module groups, and pcb) and the mail."""
    return task_id.startswith("module_") or task_id in ("pcb", "notify_")


_FOREIGN_LIB_MARKERS = ("conda", "miniforge", "mamba", "pixi")


def _airflow_env():
    """os.environ minus conda-style LD_LIBRARY_PATH entries, whose libcrypto breaks psycopg2."""
    env = dict(os.environ)
    ld = env.get("LD_LIBRARY_PATH")
    if ld:
        prefix = (env.get("CONDA_PREFIX") or "").rstrip("/")
        keep = [p for p in ld.split(os.pathsep)
                if p and not any(m in p.lower() for m in _FOREIGN_LIB_MARKERS)
                and not (prefix and (p == prefix or p.startswith(prefix + "/")))]
        if keep:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(keep)
        else:
            del env["LD_LIBRARY_PATH"]
    return env


def _airflow(*a, capture=True):
    r = subprocess.run([_venv_bin("airflow"), *a],
                       capture_output=capture, text=True, env=_airflow_env())
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def _run_state(dag_id, run_id):
    _, out, _ = _airflow("dags", "list-runs", dag_id, "-o", "plain")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == run_id:
            return parts[2]
    return None


def _task_states(dag_id, run_id):
    _, out, _ = _airflow("tasks", "states-for-dag-run", dag_id, run_id, "-o", "json")
    try:
        rows = json.loads(out)
    except ValueError:
        return {}
    return {r.get("task_id", ""): (r.get("state") or "")
            for r in rows if r.get("task_id")}


# Legacy make targets, in dash form, longest first so "formal-par" wins over "par".
_TARGET_STAGES = sorted(
    [st.replace("_", "-") for st in _STAGES], key=len, reverse=True)


def _parse_target(word):
    """Split a legacy make target into (stage, module, redo).

    Accepts what `make` accepted for a hierarchical flow -- `syn`,
    `par-RocketTile`, `redo-formal-syn-ScratchpadBank_1` -- so muscle memory
    from the Makefile carries over unchanged. Returns None when the word is
    not a target, in which case the caller falls through to the airflow
    passthrough.
    """
    redo = False
    w = word
    if w.startswith("redo-"):
        redo, w = True, w[len("redo-"):]
    for st in _TARGET_STAGES:
        if w == st:
            return st.replace("-", "_"), None, redo
        if w.startswith(st + "-"):
            mod = w[len(st) + 1:]
            # a module name, not another stage fragment
            if mod and not mod.startswith("to-"):
                return st.replace("-", "_"), mod, redo
    return None


# Well-known places a stack environment file may live, tried in order.
STACK_ENV_HOME = os.path.expanduser("~/.sledgehammer/env.sh")


def _find_stack_env():
    """The shell file that configures this stack, or None.

    Order: $SLEDGE_ENV_FILE, ~/.sledgehammer/env.sh, then a stack_env.sh found
    by walking up from the current directory. The last one means a workspace
    can carry its own stack without anyone configuring anything.
    """
    cand = os.environ.get("SLEDGE_ENV_FILE")
    if cand and os.path.isfile(cand):
        return cand
    if os.path.isfile(STACK_ENV_HOME):
        return STACK_ENV_HOME
    d = os.path.abspath(os.getcwd())
    while True:
        f = os.path.join(d, "stack_env.sh")
        if os.path.isfile(f):
            return f
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _load_stack_env():
    """Source the stack env file into this process, if one is found.

    Makes `sledgehammer par -t Top` work in a bare shell: the CLI configures
    itself instead of requiring `source stack_env.sh` first. Values already in
    the environment win, so an explicit export always beats the file. A no-op
    when the stack is already configured or no file exists.
    """
    if os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN") and \
            os.environ.get("HAMMER_PG_HOST"):
        return None
    f = _find_stack_env()
    if not f:
        return None
    try:
        # run the file in a shell and diff the environment it produces
        res = subprocess.run(
            # the path goes in as $1, never into the script text, so a
            # directory name with quotes or $(...) cannot run as code
            ["bash", "-c", '_sledgehammer_env_file=$1; set --; set -a; '
             'source "$_sledgehammer_env_file" >/dev/null 2>&1; env -0', "_", f],
            capture_output=True, timeout=60)
        if res.returncode != 0:
            return None
        for chunk in res.stdout.split(b"\0"):
            if not chunk or b"=" not in chunk:
                continue
            k, v = chunk.decode("utf-8", "ignore").split("=", 1)
            # never override what the caller set explicitly
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        return None
    return f


def _dags_folder():
    """Where registered DAGs live, and how we knew.

    Priority: HAMMER_DAGS_FOLDER, then Airflow's own configured dags_folder
    (authoritative -- it is what the scheduler actually reads), then
    $AIRFLOW_HOME/dags. Asking Airflow mirrors how --obj_dir asks the
    Makefile: consult the tool that owns the answer rather than guessing.
    """
    env = os.environ.get("HAMMER_DAGS_FOLDER")
    if env:
        return env, "$HAMMER_DAGS_FOLDER"
    try:
        rc, out, _ = _airflow("config", "get-value", "core", "dags_folder")
        got = (out or "").strip().splitlines()
        if rc == 0 and got and got[-1].strip():
            return got[-1].strip(), "airflow config"
    except Exception:
        pass
    return os.path.join(os.environ.get("AIRFLOW_HOME", REPO), "dags"), "$AIRFLOW_HOME/dags"


def _dag_obj_dir(dag_file, key="OBJ_DIR"):
    """OBJ_DIR (or another path, e.g. WORK_DIR) baked into a generated DAG, or None."""
    try:
        with open(dag_file) as f:
            for line in f:
                if line.startswith(key):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        return None
    return None


def _dag_supports_forceall(dag_file):
    """True if the generated DAG forces per stage (it knows the 'forceall' Param)."""
    try:
        with open(dag_file) as f:
            return "forceall" in f.read()
    except OSError:
        return True


def _dag_has_pcb(dag_file):
    """True if the generated DAG has the pcb task (it knows the 'pcb' Param)."""
    try:
        with open(dag_file) as f:
            return "'pcb': Param" in f.read()
    except OSError:
        return True


def _dags_for_cwd(dags_folder, user):
    """[(design, obj_dir)] for registered DAGs that build under the cwd or were generated from it.

    Lets `cd vlsi && sledgehammer par` find its own DAG with no flags and no
    Makefile, including one whose OBJ_DIR is on a scratch disk.
    """
    here = os.path.realpath(os.getcwd())
    hits = []
    try:
        names = sorted(os.listdir(dags_folder))
    except OSError:
        return hits
    for fn in names:
        if not (fn.startswith("sledgehammer_") and fn.endswith(f"_{user}.py")):
            continue
        path = os.path.join(dags_folder, fn)
        od = _dag_obj_dir(path)
        wd = _dag_obj_dir(path, "WORK_DIR")
        if od and ((os.path.realpath(od) + os.sep).startswith(here + os.sep)
                   or (wd and os.path.realpath(wd) == here)):
            hits.append((fn[len("sledgehammer_"):-len(f"_{user}.py")], od))
    return hits


def _infer_obj_dir():
    """Where the build lives when no registered DAG says so: $OBJ_DIR, else the only build/<x>.

    Never asks make: GNU make remakes included makefiles before it reads any
    goal, so even a probe would rebuild hammer.d and the DAG.
    """
    if os.environ.get("OBJ_DIR"):
        return os.environ["OBJ_DIR"], "$OBJ_DIR"
    if os.path.isdir("build"):
        subs = [d for d in sorted(os.listdir("build"))
                if os.path.isdir(os.path.join("build", d))]
        if len(subs) == 1:
            return os.path.abspath(os.path.join("build", subs[0])), "build/"
    return None, None


def _no_dag_exit(design, dag_file, dags_src):
    sys.exit(
        f"[sledgehammer] no DAG registered for {design}.\n"
        f"  looked for: {dag_file}\n"
        f"  (dags folder from {dags_src})\n"
        f"  Generate it first, the same way you always have:\n"
        f"      cd <vlsi dir> && make buildfile")


def _cmd_run(args) -> int:
    import argparse
    import getpass
    p = argparse.ArgumentParser(
        prog="sledgehammer run",
        description="Run flow stages through the DAG with hammer's flags.")
    p.add_argument("actions", nargs="+",
                   help=f"stages to run: {', '.join(_STAGES)} (dashes ok)")
    p.add_argument("--obj_dir", help="build directory; taken from the "
                   "registered DAG, or $OBJ_DIR / the only build/<x>, when omitted")
    # hammer spells the design's top module -t/--top; --design is our alias
    p.add_argument("-t", "--top", "--design", dest="top",
                   help="top module / design name (default: obj_dir basename)")
    p.add_argument("--module", action="append", default=[],
                   help="restrict to these modules (hierarchical flows)")
    # hammer's --force; --redo is the DAG conf key and stays as an alias
    p.add_argument("--force", "--redo", dest="force", action="store_true",
                   help="rerun the named stages even if nothing changed; "
                        "earlier stages keep their normal check")
    p.add_argument("--forceall", "--redo-all", dest="forceall", action="store_true",
                   help="rerun every stage this run executes, earlier ones included")
    p.add_argument("--local", action="store_true",
                   help="do not pull cached results from the PD store")
    p.add_argument("--tools",
                   help="tool config to build with, one of the DAG's Tools choices "
                        "(default: the one the DAG was generated with)")
    p.add_argument("--workspace")
    p.add_argument("--project")
    p.add_argument("--start_before_step", "--from_step", "--from-step",
                   dest="from_step")
    p.add_argument("--stop_after_step", "--to_step", "--to-step", dest="to_step")
    p.add_argument("--only_step", "--only-step", dest="only_step")
    p.add_argument("--steps_stage", "--steps-stage", dest="steps_stage",
                   choices=["syn", "par"], default="syn")
    p.add_argument("--run-id")
    p.add_argument("--no-wait", action="store_true",
                   help="trigger and return; do not stream the run")
    a = p.parse_args(args)

    actions = [x.replace("-", "_") for x in a.actions]
    bad = [x for x in actions if x not in _STAGES]
    if bad:
        sys.exit(f"[sledgehammer] unknown stage(s): {', '.join(bad)}. "
                 f"Valid: {', '.join(_STAGES)}")

    user = getpass.getuser()
    dags_folder, dags_src = _dags_folder()

    # A registered DAG already carries its OBJ_DIR and design, baked in when it
    # was generated -- running one needs no Makefile and no working directory.
    # Explicit flags win; otherwise the DAG that builds under, or was generated
    # from, the cwd is the one meant.
    obj_dir = os.path.abspath(a.obj_dir) if a.obj_dir else None
    design = a.top
    if design and not obj_dir:
        named = os.path.join(dags_folder, f"sledgehammer_{design}_{user}.py")
        if not os.path.exists(named):
            _no_dag_exit(design, named, dags_src)
        got = _dag_obj_dir(named)
        if got:
            obj_dir, src = got, "the registered DAG"
    if not obj_dir and not design:
        hits = _dags_for_cwd(dags_folder, user)
        if len(hits) > 1:
            sys.exit(f"[sledgehammer] more than one registered DAG belongs to this directory: "
                     f"{', '.join(d for d, _ in hits)}. Pick one with -t <design>.")
        if hits:
            design, obj_dir, src = hits[0][0], hits[0][1], "the registered DAG"
    if not obj_dir:
        obj_dir, src = _infer_obj_dir()
        if not obj_dir:
            sys.exit("[sledgehammer] could not work out obj_dir. Pass "
                     "--obj_dir, or -t <top> for a DAG that is already "
                     "registered, or run from the vlsi directory.\n"
                     "  No DAG yet? Generate it first: cd <vlsi dir> && make buildfile")
        obj_dir = os.path.abspath(obj_dir)
    if not a.obj_dir:
        print(f"[sledgehammer] obj_dir from {src}: {obj_dir}")
    design = design or os.path.basename(obj_dir.rstrip("/"))
    dag_id = f"sledgehammer_{design}_{user}"
    dag_file = os.path.join(dags_folder, f"{dag_id}.py")

    if not os.path.exists(dag_file):
        _no_dag_exit(design, dag_file, dags_src)
    baked = _dag_obj_dir(dag_file)
    if baked and os.path.realpath(baked) != os.path.realpath(obj_dir) \
            and os.path.isfile(os.path.join(obj_dir, "hammer_dag.py")):
        sys.exit(
            f"[sledgehammer] {dag_id} is registered for {baked}, not {obj_dir}.\n"
            f"  Both builds are named {design}, and only the one generated last is registered.\n"
            f"  Regenerate the DAG from {obj_dir} (make buildfile) to switch to it.")

    if "pcb" in actions and not _dag_has_pcb(dag_file):
        sys.exit(f"[sledgehammer] {dag_file} predates the pcb stage, so asking it for pcb would run "
                 f"nothing. Regenerate it (make buildfile), or run make pcb.")

    _airflow("dags", "unpause", dag_id)
    conf = {s: True for s in actions}
    if a.module:
        conf["modules"] = a.module
    if a.force or a.forceall:
        conf["redo"] = True
    if a.forceall:
        conf["forceall"] = True
    if a.force and not a.forceall and not _dag_supports_forceall(dag_file):
        print(f"[sledgehammer] {dag_file} predates per-stage --force, so it still forces "
              f"every stage this run executes; regenerate it (make buildfile) to force "
              f"only {' '.join(actions)}.")
    if a.local:
        conf["local"] = True
    if a.tools:
        conf["tools"] = a.tools
    if a.workspace:
        conf["workspace"] = a.workspace
    if a.project:
        conf["project"] = a.project
    for k, v in (("from_step", a.from_step), ("to_step", a.to_step),
                 ("only_step", a.only_step)):
        if v:
            conf[k] = v
            conf["steps_stage"] = a.steps_stage
    run_id = a.run_id or f"cli_{int(time.time())}"
    rc, _, err = _airflow("dags", "trigger", dag_id, "-r", run_id,
                          "-c", json.dumps(conf))
    if rc != 0:
        sys.stderr.write(err)
        sys.exit("[sledgehammer] trigger failed")
    print(f"[sledgehammer] {dag_id} run {run_id} triggered: "
          f"{' '.join(actions)}")
    if a.no_wait:
        return 0

    print("[sledgehammer] streaming (Ctrl-C detaches; the run keeps going)")
    last = {}
    try:
        while True:
            for t, s in sorted(_task_states(dag_id, run_id).items()):
                if not s or s == "skipped" or last.get(t) == s:
                    continue
                if _shown(t):
                    print(f"  [{time.strftime('%H:%M')}] "
                          f"{t.replace('module_', ''):42s} {s}")
                last[t] = s
            state = _run_state(dag_id, run_id)
            if state in ("success", "failed"):
                print(f"[sledgehammer] run {run_id}: {state.upper()}")
                return 0 if state == "success" else 1
            time.sleep(30)
    except KeyboardInterrupt:
        print(f"\n[sledgehammer] detached; check later with: "
              f"sledgehammer status --design {design}")
        return 0


def _cmd_status(args, list_only=False) -> int:
    import argparse
    import getpass
    p = argparse.ArgumentParser(prog="sledgehammer status")
    p.add_argument("--design")
    p.add_argument("--run-id")
    p.add_argument("-n", type=int, default=8)
    a = p.parse_args(args)
    user = getpass.getuser()
    if a.design:
        dag_ids = [f"sledgehammer_{a.design}_{user}"]
    else:
        _, out, _ = _airflow("dags", "list", "-o", "plain")
        # match the _<user> suffix, not a substring: user "u" must not see
        # sledgehammer_Top_uma
        dag_ids = [l.split()[0] for l in out.splitlines()
                   if l.startswith("sledgehammer_") and l.split()[0].endswith(f"_{user}")]
    for dag_id in dag_ids:
        _, out, _ = _airflow("dags", "list-runs", dag_id, "-o", "plain")
        rows = [l.split() for l in out.splitlines()[1:] if l.split()]
        rows = [r for r in rows if len(r) >= 3][:a.n]
        if not rows:
            continue
        if list_only:
            for r in rows:
                print(f"  {dag_id[len('sledgehammer_'):-len('_' + user)]:18s} "
                      f"{r[1]:44s} {r[2]}")
            continue
        rid = a.run_id or rows[0][1]
        state = next((r[2] for r in rows if r[1] == rid), "?")
        print(f"{dag_id}  run {rid}: {state}")
        for t, s in sorted(_task_states(dag_id, rid).items()):
            if s and s != "skipped" and _shown(t):
                print(f"  {t.replace('module_', ''):44s} {s}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    sub = args[0] if args else ""

    if sub == "postgres":
        from hammer.shell import sledgehammer_postgres
        return sledgehammer_postgres.main(args[1:])

    # Flow commands compose with an existing stack (stack_env, iris-db, ...):
    # respect AIRFLOW_HOME when the caller set one, default to the checkout.
    if sub in ("run", "status", "runs"):
        loaded = _load_stack_env()
        if loaded:
            print(f"[sledgehammer] stack env from {loaded}")
        os.environ.setdefault("AIRFLOW_HOME", REPO)
        _load_secrets()
        if sub == "run":
            return _cmd_run(args[1:])
        return _cmd_status(args[1:], list_only=(sub == "runs"))

    # Legacy make-target spelling: `sledgehammer par-RocketTile` is the same
    # request as `make par-RocketTile` was, and runs through the DAG.
    if sub and sub not in HELP_WORDS:
        parsed = _parse_target(sub)
        if parsed:
            stage, module, redo = parsed
            loaded = _load_stack_env()
            if loaded:
                print(f"[sledgehammer] stack env from {loaded}")
            os.environ.setdefault("AIRFLOW_HOME", REPO)
            _load_secrets()
            fwd = [stage]
            if module:
                fwd += ["--module", module]
            if redo:
                fwd += ["--redo"]
            return _cmd_run(fwd + args[1:])
        if sub.startswith("hier-par-to-syn-"):
            print("[sledgehammer] the leaf-to-top bridge is a DAG edge -- it "
                  "runs automatically. Just ask for the top-level stage, e.g."
                  f"\n  sledgehammer par --obj_dir <obj_dir>")
            return 0

    # Pin AIRFLOW_HOME so the checkout's airflow.cfg is read, not the ~/airflow defaults on port 8080.
    os.environ["AIRFLOW_HOME"] = REPO

    if sub in HELP_WORDS:
        sys.stdout.write(__doc__)
        return 0

    if sub in LAUNCH_WORDS:
        if sub in ("lite", "full"):
            os.environ["SLEDGE_LITE"] = "1" if sub == "lite" else "0"
        # venv.sh sets up the BWRC tool environment that the workers inherit.
        cmd = ["bash", "-c", 'cd "$1" && shift && _sledgehammer_argv=("$@") && set -- && '
               'source ./venv.sh && exec "${_sledgehammer_argv[@]}"',
               "sledgehammer", REPO, sys.executable, LAUNCHER] + args[1:]
        if os.environ.get("SLEDGE_DRYRUN"):
            print(f"[dryrun] launch  "
                  f"SLEDGE_LITE={os.environ.get('SLEDGE_LITE', '(checkout default)')} "
                  f"AIRFLOW_HOME={os.environ['AIRFLOW_HOME']}  ->  {' '.join(cmd)}")
            return 0
        os.execvp("bash", cmd)

    # Otherwise pass straight through to the airflow CLI, secrets loaded first.
    # The stack env file applies here too; AIRFLOW_HOME stays pinned above.
    loaded = _load_stack_env()
    if loaded:
        print(f"[sledgehammer] stack env from {loaded}", file=sys.stderr)
    cmd = [_venv_bin("airflow")] + args
    if os.environ.get("SLEDGE_DRYRUN"):
        print(f"[dryrun] passthrough  AIRFLOW_HOME={os.environ['AIRFLOW_HOME']} "
              f"(secrets loaded)  ->  {' '.join(cmd)}")
        return 0
    _load_secrets()
    os.execve(cmd[0], cmd, _airflow_env())


if __name__ == "__main__":
    sys.exit(main())
