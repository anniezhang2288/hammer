# SledgeHammer command reference

Every console command the venv installs, what it is for, and the flags that
matter. Two commands are SledgeHammer's own (`sledgehammer`, `studio`); the
rest are Hammer's, unchanged.

Everything here works from a terminal. The web UI is optional — there is no
step in a normal flow that requires it.

| Command | Purpose |
|---|---|
| **`sledgehammer`** | Launch the Airflow stack; run flows from the CLI; Airflow passthrough |
| **`studio`** | Admin + cache CLI for the shared PD database (43 subcommands) |
| `hammer-vlsi` | Hammer's flow driver (syn, par, drc, lvs, build, ...) |
| `get-config` | Print a resolved config key |
| `hammer-generate-mdf` | Generate a macro definition file |
| `hammer-shell-test` | Shell-integration smoke test |
| `readlink-array`, `yaml2json`, `asap7_gds_scale` | Small utilities |


## Getting set up

From a fresh clone, one command builds the environment:

```bash
./scripts/uv_setup.sh
```

It neutralizes conda/mamba/any active venv first (psycopg2 and python-ldap
compile from source, and a foreign OpenSSL on the linker path gets baked in as
an RPATH that breaks them in every later shell), creates the venv, runs
`uv sync --group dev`, then installs Airflow, asyncpg and the edge3 provider
against the official constraint file, the fab provider on top with SQLAlchemy
held to 2.0.x, and builds psycopg2 and python-ldap with `--no-binary`.

It also links `sledgehammer` into `~/.local/bin`. Open a new shell and it runs
from any directory, inside a Chipyard tree or not, with nothing sourced. The
link points at the checkout that ran the script last. If you set up before
this change, rerun `./scripts/uv_setup.sh` and open a new shell: it removes the
old `sledgehammer()` function from `~/.bashrc` (which shadowed this command and
refused to run inside Chipyard) and keeps a backup in
`~/.bashrc.sledgehammer.bak`.

**Do not run `uv sync` on its own afterwards.** Airflow is installed outside
the lock, so a bare sync uninstalls it. To refresh just this package:

```bash
uv pip install -e . --no-deps
```

Then a database and the schema -- [DATABASE_SETUP.md](DATABASE_SETUP.md) for an
existing Postgres, [LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md) to stand
one up yourself:

```bash
source venv.sh && studio init && airflow db migrate
```

### The environment file

Write the stack's settings once to `~/.sledgehammer/env.sh` and nothing needs
sourcing again -- the CLI reads it itself:

```bash
mkdir -p ~/.sledgehammer
ln -s /path/to/your/stack_env.sh ~/.sledgehammer/env.sh
```

Discovery order, first hit wins: `$SLEDGE_ENV_FILE`, `~/.sledgehammer/env.sh`,
then a `stack_env.sh` found by walking up from the current directory (so a
workspace can carry its own stack). Anything already exported beats the file.

That file should set `AIRFLOW_HOME`, `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`,
`AIRFLOW__CORE__DAGS_FOLDER`, `HAMMER_PD_CACHE=1`, and the `HAMMER_PG_*`
connection variables. See LOCAL_POSTGRES_SETUP.md for a complete example.

### Running a design

Legacy generated the per-stage targets first, then ran them:

```bash
cd rocket-vlsi
make buildfile        # hammer build -> hammer.d
make syn
make par
```

SledgeHammer keeps the same two steps -- only the second word changes:

```bash
cd rocket-vlsi
make buildfile        # unchanged: hammer build -> hammer.d AND the DAG
sledgehammer syn
sledgehammer par
```

`make buildfile` stays the way a DAG is created; the same `build` action emits
the Makefile include and the DAG, and registers it. `sledgehammer` only runs
what is already registered -- it never builds, so it cannot surprise you by
regenerating a DAG you customized. Both read `OBJ_DIR` from the Makefile, so
no flags are needed inside a workspace.

If you have hand-edited a generated DAG, a later `make buildfile` will not
silently discard it: the file is copied to
`hammer_dag.py.hand-edited-<timestamp>` and the rebuild says so. For changes
meant to last, prefer driver hooks (`get_extra_hierarchical_par_hooks` and
friends) -- those survive regeneration because they live in the driver.

The one prerequisite `make` does not have: the Airflow stack must be running
(api-server, scheduler, dag-processor, triggerer). It is a service you start
once per machine, not per run.

## sledgehammer

### Running flows (no GUI)

The flags are Hammer's, so a Makefile switches over by changing the binary.

```bash
# trigger, stream to the terminal, exit with the run's status
sledgehammer run syn par --obj_dir build/ChipTop

# stages compose; these two run in parallel
sledgehammer run drc lvs --obj_dir build/ChipTop

# fire and forget
sledgehammer run syn --obj_dir build/ChipTop --no-wait
```

Stages: `sim_rtl power_rtl syn sim_syn timing_syn formal_syn power_syn par
sim_par timing_par formal_par power_par drc lvs` (dashes accepted).

| Flag | Meaning |
|---|---|
| `--obj_dir` | build directory (required) |
| `--design` | design name; defaults to the obj_dir basename |
| `--module M` | restrict to these modules, repeatable (hierarchical flows) |
| `--force`, `--redo` | rerun the named stages even if nothing changed: skip the cache lookup, overwrite the cache entry, mark later stages to rerun. Earlier stages the run needs keep their normal check |
| `--forceall`, `--redo-all` | the same for every stage the run executes, earlier ones included |
| `--local` | skip the DB cache pull, run the tool locally |
| `--from-step` / `--to-step` / `--only-step` | sub-step control |
| `--steps-stage {syn,par}` | which stage the step flags apply to; defaults to whichever of syn/par the run names, and is required when it names both or neither |
| `--workspace`, `--project` | workspace routing, ledger project label |
| `--run-id` | name the run instead of an auto `cli_<epoch>` |
| `--no-wait` | trigger and return immediately |

DAGs generated before `--forceall` existed still force every stage under `--force`;
regenerate them (`make buildfile`) and restart Airflow. `sledgehammer` warns when it sees one.

The exit code follows the run, so `sledgehammer run ... && next-step` behaves.
Ctrl-C detaches from the stream; the run keeps going.

### Reading results

```bash
sledgehammer status --design ChipTop     # latest run, per-task states
sledgehammer status --design ChipTop --run-id lvsfinal2_1787205495
sledgehammer runs --design ChipTop -n 5  # recent runs
```

### Stack and passthrough

```bash
sledgehammer                  # launch with LDAP + TOTP (sets SLEDGE_2FA=1)
sledgehammer standalone       # same
SLEDGE_2FA=0 sledgehammer     # plain LDAP, no second factor
sledgehammer db migrate       # any airflow subcommand, secrets pre-loaded
sledgehammer dags list
SLEDGE_DRYRUN=1 sledgehammer  # print what it would run
```

The launcher decrypts the GPG secrets and pins `AIRFLOW_HOME` first, so
Airflow reaches the metadata DB with nothing exported by hand. The flow
commands (`run`, `status`, `runs`) instead *respect* an `AIRFLOW_HOME` you
already set, so they compose with an existing stack.

## studio

### Time saved — `time-saved` (the paper numbers)

```bash
studio time-saved                          # headline totals
studio time-saved --group-by design        # per design
studio time-saved --design ChipTop --since 2026-08-03
studio time-saved --csv tat.csv --group-by project
studio time-saved --cache-only             # exclude legacy-equivalent skips
```

Parallel-flow savings are measured from Airflow task windows. When the ledger
spans more than one Airflow instance (after a migration or a merge), point at
the others or every run recorded elsewhere scores as sequential:

```bash
export SLEDGE_EXTRA_METADATA_CONNS="postgresql://user:pw@host:5433/airflow_a,postgresql://user:pw@host:5433/airflow_b"
```

### Cache and artifacts

`blob-list` `blob-find` `blob-delete` `blob-reassign` `stage-key` `stage-push`
`stage-pull` `cache-status`

```bash
studio cache-status                     # is the cache and ledger on and reachable
studio blob-list -n 20
studio blob-delete --design gcd --dry-run
```

The cache is **off unless enabled** — `HAMMER_PD_CACHE=1` or
`vlsi.pd_cache.enabled`. A run that records nothing usually means this.

### Checkpoints

`checkpoints` `checkpoints-push` `checkpoints-fetch` `checkpoints-clear`

Sub-step checkpoints stream to the database while syn and par run, so a killed
task loses at most one push interval. Tune with
`HAMMER_CHECKPOINT_STREAM_SECS` (default 300); `HAMMER_CHECKPOINT_STREAM=0`
turns streaming off.

### Master database

`master-push` `master-pull` `master-list`, plus the older artifact verbs
`list` `get` `put`

### Users and access

`onboard` `grant` `revoke` `whitelist` `2fa` `admin`

### Workspaces

`workspace-list` `workspace-show` `workspace-set` `workspace-unset`

```bash
studio workspace-set alice /scratch/alice/build
```

Runs never create workspace rows; an unregistered user is an error naming the
fix, not a silent default.

### Notifications

`notify-email` `smtp-setup` `notify-test`

```bash
studio smtp-setup --user berkeley.sledgehammer.studio@gmail.com --password-stdin
studio notify-email lawrencejrhee "a@berkeley.edu, b@gmail.com"
studio notify-test
```

Completion mail is sent by a task in the DAG, not an Airflow callback —
Airflow 3.1 serializes DAGs without their callbacks, so a DAG-level callback
never fires.

### Design setup and housekeeping

`design-register` `augment` `make-dag` · `init` `reap` `project-set`
`cache-events-clear` `wipe-blobs` `wipe-master` `wipe-artifacts` `wipe-all`

```bash
studio init            # create the schema on a fresh database
studio reap            # list EDA processes left by dead runs (--kill to end them)
```

## Related documents

Setup lives in its own files: [DATABASE_SETUP.md](DATABASE_SETUP.md) (an
existing Postgres), [LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md)
(standing one up), [WORKSPACE_SETUP.md](WORKSPACE_SETUP.md),
[AIRFLOW_LDAP_SETUP.md](AIRFLOW_LDAP_SETUP.md),
[AIRFLOW_2FA_SETUP.md](AIRFLOW_2FA_SETUP.md), [SECRETS.md](SECRETS.md),
[TIME_SAVED_TRACKER.md](TIME_SAVED_TRACKER.md),
[SUBSTEP_RESUME.md](SUBSTEP_RESUME.md).

## Coming from legacy Hammer

Same flags, same target names. The differences are that stages compose in one
command, modules run in parallel, and the bridges between stages are DAG edges
instead of things you type.

### Running a flow

| Legacy Hammer | SledgeHammer |
|---|---|
| `make syn` | `sledgehammer syn` |
| `make par` (chains syn) | `sledgehammer par` |
| `make all` | `sledgehammer run syn par` |
| `make drc` then `make lvs` | `sledgehammer run drc lvs` *(parallel)* |
| `make syn-RocketTile` | `sledgehammer syn-RocketTile` |
| `make par-RocketTile` | `sledgehammer par-RocketTile` |
| `make redo-syn-RocketTile` | `sledgehammer redo-syn-RocketTile` |
| `make buildfile` | `make buildfile` *(unchanged — sledgehammer never builds)* |
| `make hier-par-to-syn-Top` | *(implicit — a DAG edge)* |
| `make par-A; make par-B; ...` (7×, serial) | `sledgehammer par` (all leaves, parallel) |

Raw driver form, if you prefer it over the Makefile:

```bash
# legacy: every config re-passed, one action, blocking
./iris-vlsi -e env.yml -p pdks/techname.yml -p design.yml -p io/iris-bumps.yml \
    -p specs/constr/iris-rockettile.yml ... -p build/IrisVLSITop/inputs.yml \
    --obj_dir build/IrisVLSITop par

# sledgehammer: make buildfile once, then the DAG remembers the configs
make buildfile
sledgehammer par -t IrisVLSITop        # every run, from anywhere
```

### Flags

Identical to `hammer-vlsi` unless noted.

| Flag | Legacy | SledgeHammer |
|---|---|---|
| `-e`, `-p` | yes | n/a — configs are baked in at `make buildfile` |
| `--obj_dir` | yes | optional — read from the registered DAG |
| `-t`, `--top` | yes — sets `synthesis.inputs.top_module` | yes, but it picks the registered DAG by design name (the obj_dir basename), not the top module (`--design` also accepted) |
| `--force` | yes | yes (`--redo` also accepted); forces only the named stages |
| `--forceall` | n/a | forces every stage the run executes (`--redo-all` also accepted) |
| `--local` | yes | yes |
| `--start_before_step` / `--from_step` | yes | yes |
| `--stop_after_step` / `--to_step` | yes | yes |
| `--only_step` | yes | yes |
| `--module M` | n/a (separate target per module) | restrict to modules, repeatable |
| `--workspace`, `--project` | n/a | workspace routing, ledger label |
| `--no-wait`, `--run-id` | n/a | trigger-and-return, name a run |
| `--syn_rundir` etc. | yes | not mirrored — the DAG derives rundirs per module |
| `-v`, `-f`, `-o` | yes | not mirrored — inputs and outputs flow along DAG edges |

### Where obj_dir comes from

Legacy took it from the Makefile, so you had to `cd vlsi` first. A generated
DAG has `OBJ_DIR` baked in, so running one needs neither:

1. `--obj_dir` when given
2. `-t <top>` → read out of that DAG
3. bare command → the DAG whose `OBJ_DIR` is under the current directory
4. `$OBJ_DIR`, then the Makefile (`make --eval` asks it directly)

### Actions with no legacy equivalent

```bash
sledgehammer status --design Top        # per-task state of the latest run
sledgehammer runs --design Top          # run history
sledgehammer run par --from_step route_opt_design   # resume mid-stage
studio time-saved                       # what the flow saved vs a make-style rerun
```

Substep resume (a killed par restarts from its last tool-confirmed checkpoint)
and the shared cache (a colleague's identical syn restores in seconds) have no
legacy counterpart at all.
