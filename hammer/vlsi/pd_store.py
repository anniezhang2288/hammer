"""
Proof-of-concept Postgres-backed store for PD artifacts.

This module provides a minimal round-trip write/read path for Hammer
PD artifacts (currently just ``par-input.json``) into Postgres. Content
is keyed by the SHA256 of its canonical JSON representation, so identical
inputs deduplicate naturally.

Connection settings are resolved with this precedence (first hit wins per
field):

    1. HAMMER_PG_* environment variables
         HAMMER_PG_HOST / HAMMER_PG_PORT / HAMMER_PG_DB /
         HAMMER_PG_USER / HAMMER_PG_PASSWORD
    2. Airflow's ``sql_alchemy_conn``, from
         ``$AIRFLOW__DATABASE__SQL_ALCHEMY_CONN``, else ``postgres.env``
         beside the secrets file (host, port and user, as written by
         ``sledgehammer postgres``), else ``airflow.cfg``
         (the same connection string Airflow uses for its metadata DB)
    3. Hardcoded defaults
         host=localhost, port=5544,
         db=sledgehammer_studio (HAMMER_PG_DB in ``postgres.env`` wins),
         user=$USER
       There is no default password: if none resolves from the env,
       Airflow's connection, libpq's PGPASSWORD or pgpass file or, for your
       local Postgres while it runs on this host, its password file,
       connecting raises rather than trying without one.

So after the default setup, which writes ``postgres.env`` for your local
Postgres, ``pd_store`` needs no env vars on the host that Postgres runs on.

Which ``airflow.cfg`` is used:
    ``$AIRFLOW_HOME/airflow.cfg`` if AIRFLOW_HOME is set, else
    ``<cwd>/airflow.cfg`` if present, else
    ``~/airflow/airflow.cfg``.

All data lives in the ``hammer_poc`` schema so it stays isolated from
Airflow's own tables.
"""

from __future__ import annotations

import configparser
import errno
import getpass
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import unquote

try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    # The Postgres driver is OPTIONAL. pd_store is imported on the hammer run
    # path (pd_cache wraps every stage), but synthesis itself doesn't need
    # Postgres. If psycopg2 isn't installed, leave it unset rather than crash
    # the whole run: cache_or_run already falls back to running the stage
    # normally on any pd_store error, so plain hammer keeps working in envs that
    # never installed the cache driver (e.g. a chipyard conda env). Install
    # psycopg2 (see UV_SETUP.md; `conda install psycopg2` in a conda env) to
    # turn the cache back on.
    psycopg2 = None  # type: ignore
    Json = None      # type: ignore

from hammer.config import HammerJSONEncoder
from hammer.config.config_src import RUN_CONTROL_KEYS

__all__ = [
    "store_par_input",
    "store_artifact",
    "load_artifact",
    "list_artifacts",
    "ensure_schema",
    "compute_sha256",
    "compute_stage_key",
    "compute_rtl_fingerprint",
    "compute_collateral_fingerprint",
    "grant_access",
    "revoke_access",
    "store_master_database",
    "load_master_database",
    "store_stage_blob",
    "load_stage_blob",
    "list_stage_blobs",
    "find_blobs",
    "count_blobs",
    "delete_blobs",
    "reassign_blobs",
    "tar_directory",
    "untar_to_directory",
    "KNOWN_STAGE_TAGS",
    "record_cache_event",
    "fetch_cache_events",
    "count_cache_events",
    "clear_cache_events",
    "set_cache_event_project",
    "current_db_user",
    "publish_design_bundle",
    "fetch_design_bundle",
    "share_design",
    "unshare_design",
    "design_has_shares",
    "list_design_shares",
]


SCHEMA_NAME = "hammer_poc"
TABLE_NAME = "pd_artifacts"
FQ_TABLE = f"{SCHEMA_NAME}.{TABLE_NAME}"

MASTER_TABLE = "master_databases"
BLOB_TABLE = "pd_blobs"
WORKSPACE_TABLE = "user_workspaces"
FQ_MASTER = f"{SCHEMA_NAME}.{MASTER_TABLE}"
FQ_BLOB = f"{SCHEMA_NAME}.{BLOB_TABLE}"
FQ_WORKSPACE = f"{SCHEMA_NAME}.{WORKSPACE_TABLE}"
NOTIFY_EMAIL_TABLE = "user_notify_email"
FQ_NOTIFY_EMAIL = f"{SCHEMA_NAME}.{NOTIFY_EMAIL_TABLE}"

# Durable cache-event ledger. Mirrors the per-run JSONL events that pd_cache
# writes to $AIRFLOW_HOME/cache_events, but survives clear_run_cache_events
# (the exit_ task deletes the JSONL after summarizing one run). This is the
# table the time-saved tracker reads to total savings across every run of a
# tapeout, since the JSONL files are ephemeral.
CACHE_EVENT_TABLE = "pd_cache_events"
FQ_CACHE_EVENT = f"{SCHEMA_NAME}.{CACHE_EVENT_TABLE}"
CHECKPOINT_TABLE = "pd_checkpoints"
FQ_CHECKPOINT = f"{SCHEMA_NAME}.{CHECKPOINT_TABLE}"
BLOB_CHUNK_TABLE = "pd_blob_chunks"
FQ_BLOB_CHUNK = f"{SCHEMA_NAME}.{BLOB_CHUNK_TABLE}"

# Split blobs bigger than this across pd_blob_chunks rows. Postgres refuses
# any single allocation over 1 GiB, and processing a bytea parameter can
# roughly double its footprint server-side, so one ~500 MB tarball already
# fails to insert (a Rocket syn rundir tars to that). 128 MiB per value
# stays far from the cliff.
BLOB_CHUNK_BYTES = 128 * 1024 * 1024

# Checkpoints still use a single bytea value; past this compressed size the
# insert would die at the same 1 GiB wall, so store_checkpoint refuses with
# a clear message instead (the local checkpoint still works).
# Overall checkpoint ceiling. Checkpoints past one bytea's practical size are
# split across pd_checkpoint_chunks (BLOB_CHUNK_BYTES pieces), so this is a
# sanity bound against runaway rundirs, not a Postgres limit.
CHECKPOINT_MAX_BYTES = 4 * 1024 * 1024 * 1024
CHECKPOINT_CHUNK_TABLE = "pd_checkpoint_chunks"
FQ_CHECKPOINT_CHUNK = f"{SCHEMA_NAME}.{CHECKPOINT_CHUNK_TABLE}"

# Design sharing (studio share / studio pull). A bundle is the compressed set of files a partner needs
# to build one design locally: its configs, RTL and the collateral they name. One row per (owner,
# design) holds the latest; a share row lets one other Postgres role read it. Row-level security, not
# the client, decides who sees which bundle, so a teammate in sledgehammer_users still cannot read a
# design nobody shared with them.
DESIGN_BUNDLE_TABLE = "pd_design_bundles"
FQ_DESIGN_BUNDLE = f"{SCHEMA_NAME}.{DESIGN_BUNDLE_TABLE}"
DESIGN_BUNDLE_CHUNK_TABLE = "pd_design_bundle_chunks"
FQ_DESIGN_BUNDLE_CHUNK = f"{SCHEMA_NAME}.{DESIGN_BUNDLE_CHUNK_TABLE}"
DESIGN_SHARE_TABLE = "pd_design_shares"
FQ_DESIGN_SHARE = f"{SCHEMA_NAME}.{DESIGN_SHARE_TABLE}"

# Everyone with access to the SledgeHammer Studio tables is in this role.
# Nobody gets direct table grants; access is purely group membership.
# A DBA has to create the role once on the cluster:
#   CREATE ROLE sledgehammer_users NOLOGIN;
#   GRANT sledgehammer_users TO lawrencejrhee WITH ADMIN OPTION;
SLEDGEHAMMER_GROUP = "sledgehammer_users"

KNOWN_STAGE_TAGS = (
    "synthesis", "par", "drc", "lvs",
    "sram_generator", "sim", "power", "formal", "timing", "pcb",
)


def _find_airflow_cfg() -> Optional[Path]:
    """Locate an ``airflow.cfg`` to use as a fallback settings source."""
    candidates = []
    if os.environ.get("AIRFLOW_HOME"):
        candidates.append(Path(os.environ["AIRFLOW_HOME"]) / "airflow.cfg")
    candidates.append(Path.cwd() / "airflow.cfg")
    candidates.append(Path.home() / "airflow" / "airflow.cfg")
    for p in candidates:
        if p.is_file():
            return p
    return None


# The password runs to the first @ that leaves a valid host, so a raw / ? # or @ in it still parses.
_CONN_URI = re.compile(r"(?P<scheme>[\w+]+)://(?:(?P<user>[^:/]*)(?::(?P<password>.*?))?@)??"
                       r"(?P<host>\[[^\]]*\]|[^/:?@\[\]]*)(?::(?P<port>\d*))?(?:/(?P<dbname>[^?@]*))?(?:\?.*)?\Z",
                       re.S)


def _parse_conn_uri(conn_str: str) -> Dict[str, Any]:
    """Parse a SQLAlchemy/libpq Postgres URI into psycopg2 connect kwargs.

    Returns an empty dict for anything that isn't a usable postgres URI.
    """
    url = _CONN_URI.match((conn_str or "").strip())
    if url is None or url.group("scheme").split("+", 1)[0] not in ("postgres", "postgresql"):
        return {}
    out: Dict[str, Any] = {}
    host = url.group("host").strip("[]").lower()
    if host:
        out["host"] = host
    if url.group("port") and int(url.group("port")):
        out["port"] = int(url.group("port"))
    if url.group("user"):
        out["user"] = unquote(url.group("user"))
    if url.group("password"):
        out["password"] = unquote(url.group("password"))
    if url.group("dbname"):
        out["dbname"] = url.group("dbname")
    return out


def _parse_airflow_cfg_conn() -> Dict[str, Any]:
    """
    Pull host/port/db/user/password from ``sql_alchemy_conn`` in airflow.cfg.

    Returns an empty dict on any failure - this is a best-effort fallback,
    not a required source.
    """
    cfg_path = _find_airflow_cfg()
    if cfg_path is None:
        return {}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(cfg_path)
    except Exception:
        return {}
    if not parser.has_option("database", "sql_alchemy_conn"):
        return {}
    return _parse_conn_uri(parser.get("database", "sql_alchemy_conn"))


def _read_conn_file(path: str) -> Dict[str, Any]:
    """Parse a Postgres URI stored on its own line in a locked file.

    Empty dict if the file is missing or doesn't hold a usable URI.
    """
    if not path:
        return {}
    try:
        with open(path) as f:
            return _parse_conn_uri(f.read().strip())
    except Exception:
        return {}


def airflow_metadata_conn_settings() -> Dict[str, Any]:
    """psycopg2 connect kwargs for the Airflow metadata DB (not the cache DB).

    A normal Airflow process (scheduler, CLI) has the real URI in
    AIRFLOW__DATABASE__SQL_ALCHEMY_CONN. Tasks and callbacks don't: Airflow 3
    hands them a decoy sqlite conn instead. The completion callback still needs
    the dag_run row, so we fall back to a SLEDGE_-prefixed channel the sandbox
    leaves alone -- an inline SLEDGE_METADATA_CONN, or a .sledge_metadata_conn
    file kept beside the SMTP password file (the launcher writes it; the SMTP env
    var carries the path through). Falls back to airflow's config / airflow.cfg
    for callers outside Airflow. Empty dict if nothing usable turns up.
    """
    for var in ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "SLEDGE_METADATA_CONN"):
        settings = _parse_conn_uri(os.environ.get(var, ""))
        if settings:
            return settings

    conn_file = os.environ.get("SLEDGE_METADATA_CONN_FILE")
    if not conn_file:
        smtp_pw = os.environ.get("SLEDGE_SMTP_PASSWORD_FILE")
        if smtp_pw:
            conn_file = os.path.join(os.path.dirname(smtp_pw), ".sledge_metadata_conn")
    settings = _read_conn_file(conn_file)
    if settings:
        return settings

    try:
        from airflow.configuration import conf
        settings = _parse_conn_uri(conf.get("database", "sql_alchemy_conn"))
        if settings:
            return settings
    except Exception:
        pass
    return _parse_airflow_cfg_conn()


def export_conn_defaults() -> None:
    """Copy host, port and user from the Airflow conn into unset HAMMER_PG_* vars, which DAG tasks inherit though they lose the conn itself."""
    settings = _parse_conn_uri(os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", ""))
    for field, var in (("host", "HAMMER_PG_HOST"), ("port", "HAMMER_PG_PORT"), ("user", "HAMMER_PG_USER")):
        if field in settings:
            os.environ.setdefault(var, str(settings[field]))


SECRETS_FILE = str(Path(__file__).resolve().parents[2] / ".sledgehammer" / "airflow-secrets.env.gpg")

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

_PG_VARS = (("host", "HAMMER_PG_HOST"), ("port", "HAMMER_PG_PORT"), ("dbname", "HAMMER_PG_DB"),
            ("user", "HAMMER_PG_USER"), ("password", "HAMMER_PG_PASSWORD"))


def secrets_file(environ: Optional[Mapping[str, str]] = None) -> str:
    """The encrypted secrets: $SLEDGE_SECRETS_FILE, else .sledgehammer/airflow-secrets.env.gpg in this checkout."""
    environ = os.environ if environ is None else environ
    return os.path.expanduser(environ.get("SLEDGE_SECRETS_FILE") or SECRETS_FILE)


def postgres_target_file(secrets: Optional[str] = None) -> str:
    """postgres.env beside the secrets, where `sledgehammer postgres` keeps the host, port, user and cache database it chose."""
    return os.path.join(os.path.dirname(secrets or secrets_file()), "postgres.env")


def postgres_target(path: Optional[str] = None) -> Dict[str, str]:
    """The KEY=value lines of postgres_target_file(), or {} when it cannot be read."""
    out: Dict[str, str] = {}
    try:
        with open(path or postgres_target_file()) as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    out[key.strip()] = value.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return out


def pg_fields(environ: Optional[Mapping[str, str]] = None,
              target: Optional[Dict[str, str]] = None) -> Dict[str, Tuple[Any, str]]:
    """Each cache connection field as (value, source), the source being env, conn, target, cfg or default."""
    environ = os.environ if environ is None else environ
    if target is None:
        target = postgres_target(postgres_target_file(secrets_file(environ)))
    fallback, source = _parse_conn_uri(environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "")), "conn"
    if not fallback:
        fallback = {field: target[var] for field, var in _PG_VARS
                    if field in ("host", "port", "user") and target.get(var)}
        source = "target"
    if not fallback:
        fallback, source = _parse_airflow_cfg_conn(), "cfg"
    try:
        default_user = getpass.getuser()
    except Exception:
        default_user = "postgres"
    defaults = {"host": "localhost", "port": 5544, "dbname": "sledgehammer_studio", "user": default_user}
    fields: Dict[str, Tuple[Any, str]] = {}
    for field, var in _PG_VARS:
        if environ.get(var):
            fields[field] = (environ[var], "env")
        elif field == "dbname" and target.get(var):
            fields[field] = (target[var], "target")
        elif field != "dbname" and fallback.get(field):
            fields[field] = (fallback[field], source)
        elif field in defaults:
            fields[field] = (defaults[field], "default")
    fields["port"] = (int(fields["port"][0]), fields["port"][1])
    return fields


def local_postgres_dir(environ: Optional[Mapping[str, str]] = None,
                       target: Optional[Dict[str, str]] = None) -> str:
    """Where sledge-local-postgres.sh keeps your local Postgres: $SLEDGE_PG_DIR, else postgres.env's, else ~/.sledgehammer/postgres."""
    environ = os.environ if environ is None else environ
    if target is None:
        target = postgres_target(postgres_target_file(secrets_file(environ)))
    pg_dir = (environ.get("SLEDGE_PG_DIR") or target.get("SLEDGE_PG_DIR")
              or os.path.join(os.path.expanduser("~"), ".sledgehammer", "postgres"))
    return os.path.abspath(os.path.expanduser(pg_dir))


def _local_port(pg_dir: str) -> Optional[int]:
    """The port in pg_dir's postgresql.conf, read as sledge-local-postgres.sh reads it; None without that file."""
    try:
        with open(os.path.join(pg_dir, "data", "postgresql.conf")) as f:
            ports = re.findall(r"^[ \t]*port[ \t]*=[ \t]*([0-9]+)", f.read(), re.M)
    except (OSError, ValueError):
        return None
    return int(ports[-1]) if ports else 5432


def _not_running_here(pg_dir: str) -> str:
    """Why pg_dir's Postgres is not running on this host, as sledge-local-postgres.sh records it, or "" when it is."""
    here = os.uname().nodename
    try:
        with open(os.path.join(pg_dir, "host")) as f:
            host = f.readline().strip()
        with open(os.path.join(pg_dir, "data", "postmaster.pid")) as f:
            pid = int(f.readline())
    except (OSError, ValueError):
        return f"is not running on {here}"
    if host != here:
        return f"was started on {host}, not on {here}"
    if pid > 0:
        try:
            os.kill(pid, 0)
            return ""
        except (OSError, OverflowError):
            pass
    return f"is not running on {here}"


def local_password(fields: Dict[str, Tuple[Any, str]], environ: Optional[Mapping[str, str]] = None,
                   target: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], str]:
    """Your local Postgres's password when fields point at its host and port and it runs here, else None and why its file was not used ("" when it does not apply)."""
    environ = os.environ if environ is None else environ
    if not hasattr(os, "getuid") or str(fields["host"][0]).lower() not in LOCAL_HOSTS:
        return None, ""
    pg_dir = local_postgres_dir(environ, target)
    path = os.path.join(pg_dir, "password")
    if _local_port(pg_dir) != fields["port"][0] or not os.path.lexists(path):
        return None, ""
    for var in ("PGHOSTADDR", "PGSERVICE"):
        if environ.get(var):
            return None, f"{path} was not used because {var} is set and can send the login to another server"
    elsewhere = _not_running_here(pg_dir)
    if elsewhere:
        return None, f"{path} was not used because your local Postgres {elsewhere}"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, ""
    except OSError as e:
        if e.errno == errno.ELOOP:
            return None, f"{path} was not used because it is a symbolic link"
        return None, f"{path} could not be read ({e.strerror})"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, f"{path} was not used because it is not a regular file"
        if st.st_uid != os.getuid():
            return None, f"{path} was not used because it is owned by someone else"
        if st.st_mode & 0o077:
            return None, (f"{path} was not used because group or others have access to it "
                          f"(mode {stat.S_IMODE(st.st_mode):03o}); chmod 600 it")
        with os.fdopen(fd, closefd=False) as f:
            password = f.readline().rstrip("\n")
    except (OSError, ValueError):
        return None, f"{path} could not be read"
    finally:
        os.close(fd)
    return (password, "") if password else (None, f"{path} is empty")


def _pg_settings(environ: Optional[Mapping[str, str]] = None,
                 target: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Gather Postgres connection settings.

    Precedence (first hit wins per field):
        1. HAMMER_PG_* environment variables
        2. Airflow's sql_alchemy_conn, from $AIRFLOW__DATABASE__SQL_ALCHEMY_CONN, else postgres.env
           beside the secrets, else airflow.cfg
        3. Hardcoded defaults (the database: postgres.env's HAMMER_PG_DB, else sledgehammer_studio)
        4. Password only: libpq's own PGPASSWORD or pgpass file ($PGPASSFILE, else ~/.pgpass)
        5. Password only, when the host is local, the port is the one in local_postgres_dir()'s
           postgresql.conf and that Postgres runs on this host: its password file, if it is yours
           and only you can read it
    """
    environ = os.environ if environ is None else environ
    if target is None:
        target = postgres_target(postgres_target_file(secrets_file(environ)))
    fields = pg_fields(environ, target)
    settings: Dict[str, Any] = {field: fields[field][0] for field in ("host", "port", "dbname", "user")}
    settings["connect_timeout"] = _connect_timeout()
    if "password" in fields:
        settings["password"] = fields["password"][0]
        return settings
    if os.environ.get("PGPASSWORD"):
        return settings
    pgpass = os.environ.get("PGPASSFILE") or os.path.join(os.path.expanduser("~"), ".pgpass")
    if os.path.isfile(pgpass):
        settings["passfile"] = pgpass
        return settings
    password, why = local_password(fields, environ, target)
    if password is not None:
        settings["password"] = password
        return settings
    raise DatabaseUnavailable(
        "No Postgres password found." + (f" {why}." if why else "") + " Run \"source venv.sh\" to load "
        "HAMMER_PG_PASSWORD from the encrypted secrets, export HAMMER_PG_PASSWORD, or add a ~/.pgpass line "
        "for this server; \"sledgehammer postgres show\" says where each setting comes from."
    )


class DatabaseUnavailable(RuntimeError):
    """The cache database cannot be reached: no driver, or no password configured."""


def _connect_timeout() -> int:
    """Seconds libpq waits for the server, from HAMMER_PG_CONNECT_TIMEOUT (default 10; 0, or more than libpq can hold, waits forever)."""
    try:
        seconds = float(os.environ.get("HAMMER_PG_CONNECT_TIMEOUT") or 10)
    except ValueError:
        return 10
    if math.isnan(seconds):
        return 10
    return 0 if seconds <= 0 or seconds > 2**31 - 1 else math.ceil(seconds)


def _connect():
    """Open a new psycopg2 connection using env-var config."""
    if psycopg2 is None:
        raise DatabaseUnavailable(
            "psycopg2 is not installed; the Postgres PD cache is unavailable. "
            "Install psycopg2 (see UV_SETUP.md, or `conda install psycopg2` in a "
            "conda env) to enable caching."
        )
    return psycopg2.connect(**_pg_settings())


_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME};

CREATE TABLE IF NOT EXISTS {FQ_TABLE} (
    sha256          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    top_module      TEXT,
    data            JSONB NOT NULL,
    owner           TEXT NOT NULL DEFAULT current_user,
    triggering_user TEXT,
    dag_id          TEXT,
    dag_run_id      TEXT,
    workspace       TEXT,
    design          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {FQ_MASTER} (
    design          TEXT PRIMARY KEY,
    db              JSONB NOT NULL,
    owner           TEXT NOT NULL DEFAULT current_user,
    triggering_user TEXT,
    dag_id          TEXT,
    dag_run_id      TEXT,
    workspace       TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS {FQ_BLOB} (
    sha256           TEXT PRIMARY KEY,
    stage            TEXT NOT NULL,
    data             BYTEA NOT NULL,
    size_bytes       BIGINT NOT NULL,
    owner            TEXT NOT NULL DEFAULT current_user,
    -- Wall-clock seconds the original tool run took (Genus, Innovus, etc.).
    -- Recorded on the MISS path when we actually invoke the tool, then used
    -- on the HIT path to report "saved ~X seconds" to the user.
    duration_seconds REAL,
    -- CPU seconds (user + sys, summed across all child processes) the
    -- original tool run consumed. Always >= duration_seconds for multi-
    -- threaded tools like Innovus. Lets us report CPU-time saved as well
    -- as wall-clock saved on cache hits.
    cpu_seconds      REAL,
    -- Provenance: which Airflow user / DAG / run / workspace / design
    -- produced this blob. All nullable so non-Airflow callers (e.g. direct
    -- hammer-vlsi invocations from the shell) don't need to set them.
    triggering_user  TEXT,
    dag_id           TEXT,
    dag_run_id       TEXT,
    workspace        TEXT,
    design           TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Overflow storage for pd_blobs rows too big for one bytea value (see
-- BLOB_CHUNK_BYTES). The parent row keeps stage/size/provenance with an
-- empty data column; the tarball bytes live here in seq order and
-- load_stage_blob reassembles them.
CREATE TABLE IF NOT EXISTS {FQ_BLOB_CHUNK} (
    sha256 TEXT NOT NULL,
    seq    INT NOT NULL,
    data   BYTEA NOT NULL,
    PRIMARY KEY (sha256, seq)
);

-- Per-user NAMED workspaces. A user may register multiple workspaces (keyed by
-- workspace_name) so they can operate in several at the same time -- e.g. one
-- per branch / tool config / experiment. The active workspace for a given DAG
-- run is selected per-run (dag_run.conf["workspace"] or $HAMMER_WORKSPACE),
-- defaulting to 'default'. Each (username, workspace_name) maps to one
-- workspace_root into which that run's build artifacts are written. Nobody's
-- tasks ever touch anyone else's directory: clean wipes only the triggering
-- user's resolved <workspace_root>/<design>; the shared pd_blobs cache is the
-- only thing crossing user boundaries (read-only-via-tarball).
CREATE TABLE IF NOT EXISTS {FQ_WORKSPACE} (
    username       TEXT NOT NULL,
    workspace_name TEXT NOT NULL DEFAULT 'default',
    workspace_root TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (username, workspace_name)
);

-- Migrate older single-column-PK workspace tables (username was the sole PK)
-- to the composite key in place, so existing deployments gain multi-workspace
-- support without a manual DROP/recreate. Existing rows become each user's
-- 'default' workspace. Idempotent: the DO block only fires while the PK is
-- still 1 column.
ALTER TABLE {FQ_WORKSPACE} ADD COLUMN IF NOT EXISTS workspace_name TEXT NOT NULL DEFAULT 'default';
DO $$
DECLARE pk_name text; pk_cols int;
BEGIN
    SELECT conname, array_length(conkey, 1) INTO pk_name, pk_cols
    FROM pg_constraint
    WHERE conrelid = '{FQ_WORKSPACE}'::regclass AND contype = 'p';
    IF pk_cols = 1 THEN
        EXECUTE format('ALTER TABLE {FQ_WORKSPACE} DROP CONSTRAINT %I', pk_name);
        EXECUTE 'ALTER TABLE {FQ_WORKSPACE} ADD PRIMARY KEY (username, workspace_name)';
    END IF;
END $$;

-- Backfill the owner column for tables that already exist from earlier inits.
ALTER TABLE {FQ_MASTER} ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT current_user;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT current_user;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS duration_seconds REAL;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS cpu_seconds      REAL;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS triggering_user TEXT;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS dag_id          TEXT;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS dag_run_id      TEXT;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS workspace       TEXT;
ALTER TABLE {FQ_BLOB}   ADD COLUMN IF NOT EXISTS design          TEXT;

ALTER TABLE {FQ_TABLE}  ADD COLUMN IF NOT EXISTS owner           TEXT NOT NULL DEFAULT current_user;
ALTER TABLE {FQ_TABLE}  ADD COLUMN IF NOT EXISTS triggering_user TEXT;
ALTER TABLE {FQ_TABLE}  ADD COLUMN IF NOT EXISTS dag_id          TEXT;
ALTER TABLE {FQ_TABLE}  ADD COLUMN IF NOT EXISTS dag_run_id      TEXT;
ALTER TABLE {FQ_TABLE}  ADD COLUMN IF NOT EXISTS workspace       TEXT;
ALTER TABLE {FQ_TABLE}  ADD COLUMN IF NOT EXISTS design          TEXT;

ALTER TABLE {FQ_MASTER} ADD COLUMN IF NOT EXISTS triggering_user TEXT;
ALTER TABLE {FQ_MASTER} ADD COLUMN IF NOT EXISTS dag_id          TEXT;
ALTER TABLE {FQ_MASTER} ADD COLUMN IF NOT EXISTS dag_run_id      TEXT;
ALTER TABLE {FQ_MASTER} ADD COLUMN IF NOT EXISTS workspace       TEXT;

CREATE INDEX IF NOT EXISTS idx_{BLOB_TABLE}_stage ON {FQ_BLOB} (stage);
CREATE INDEX IF NOT EXISTS idx_{BLOB_TABLE}_owner ON {FQ_BLOB} (owner);
CREATE INDEX IF NOT EXISTS idx_{MASTER_TABLE}_owner ON {FQ_MASTER} (owner);

-- Addresses users opted into for flow-completion emails. Each person sets their
-- own through the self-service web form, which scopes the write to their
-- logged-in identity.
CREATE TABLE IF NOT EXISTS {FQ_NOTIFY_EMAIL} (
    uid        TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Durable PD-cache event ledger: one row per cache decision (HIT / MISS_STORE /
-- SKIP_*). The per-run JSONL log under $AIRFLOW_HOME/cache_events feeds the
-- exit_ task's one-run summary and is then deleted; this table keeps every
-- event so the time-saved tracker can total savings across all runs of a
-- tapeout. Append-only; one INSERT per event. All provenance columns nullable
-- so direct (non-Airflow) hammer runs can record too.
CREATE TABLE IF NOT EXISTS {FQ_CACHE_EVENT} (
    id                BIGSERIAL PRIMARY KEY,
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stage             TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    saved_seconds     REAL,
    tool_seconds      REAL,
    restore_seconds   REAL,
    store_seconds     REAL,
    saved_cpu_seconds REAL,
    tool_cpu_seconds  REAL,
    owner             TEXT NOT NULL DEFAULT current_user,
    triggering_user   TEXT,
    dag_id            TEXT,
    dag_run_id        TEXT,
    workspace         TEXT,
    design            TEXT,
    -- Optional human-assigned grouping (e.g. "ee290_tapeout"). Unlike dag_id /
    -- design (auto-derived from the build dir), project lets you bucket several
    -- designs/dags under one tapeout. Set it via HAMMER_PD_PROJECT, the
    -- vlsi.pd_cache.project config key, the DAG trigger conf 'project' key,
    -- or relabel existing rows with `studio project-set`.
    project           TEXT,
    -- For dep-check skips: would legacy make (mtime rule over the hammer.d
    -- prerequisites) have rerun this stage? TRUE means the skip is a
    -- SledgeHammer-only saving; FALSE means make would have skipped it too;
    -- NULL means unknown (old rows, or the mtime check failed).
    make_would_rerun  BOOLEAN,
    -- The block the stage ran on: the per-module name in hierarchical flows
    -- (syn-SubModA records module=SubModA), the top module in flat flows.
    module            TEXT,
    sha256            TEXT
);
ALTER TABLE {FQ_CACHE_EVENT} ADD COLUMN IF NOT EXISTS project TEXT;
ALTER TABLE {FQ_CACHE_EVENT} ADD COLUMN IF NOT EXISTS make_would_rerun BOOLEAN;
ALTER TABLE {FQ_CACHE_EVENT} ADD COLUMN IF NOT EXISTS module TEXT;
ALTER TABLE {FQ_CACHE_EVENT} ADD COLUMN IF NOT EXISTS store_seconds REAL;
CREATE INDEX IF NOT EXISTS idx_{CACHE_EVENT_TABLE}_stage   ON {FQ_CACHE_EVENT} (stage);
CREATE INDEX IF NOT EXISTS idx_{CACHE_EVENT_TABLE}_dag     ON {FQ_CACHE_EVENT} (dag_id);
CREATE INDEX IF NOT EXISTS idx_{CACHE_EVENT_TABLE}_design  ON {FQ_CACHE_EVENT} (design);
CREATE INDEX IF NOT EXISTS idx_{CACHE_EVENT_TABLE}_project ON {FQ_CACHE_EVENT} (project);
CREATE INDEX IF NOT EXISTS idx_{CACHE_EVENT_TABLE}_ts      ON {FQ_CACHE_EVENT} (ts);

-- Sub-step checkpoints of crashed or paused stages: the tool's own
-- pre_<step> design database, tarballed/gzipped, so a rerun can resume on a
-- different machine or a fresh checkout. Rows exist only while a stage is
-- broken: pushed when a stage fails, replaced by newer attempts (one row per
-- stage_key + step), and deleted when the stage later commits successfully.
CREATE TABLE IF NOT EXISTS {FQ_CHECKPOINT} (
    id               BIGSERIAL PRIMARY KEY,
    stage_key        TEXT NOT NULL,
    stage            TEXT NOT NULL,
    step             TEXT NOT NULL,
    data             BYTEA NOT NULL,
    size_bytes       BIGINT NOT NULL,
    -- innovus checkpoints are directories (stored as tar.gz); genus
    -- checkpoints are single files (stored gzipped)
    is_dir           BOOLEAN NOT NULL DEFAULT FALSE,
    owner            TEXT NOT NULL DEFAULT current_user,
    triggering_user  TEXT,
    dag_id           TEXT,
    dag_run_id       TEXT,
    workspace        TEXT,
    design           TEXT,
    module           TEXT,
    project          TEXT,
    -- measured wall time of the completed steps up to this checkpoint, so a
    -- cross-machine resume can credit the skip in the time-saved ledger
    saved_seconds    REAL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (stage_key, step)
);
ALTER TABLE {FQ_CHECKPOINT} ADD COLUMN IF NOT EXISTS saved_seconds REAL;
ALTER TABLE {FQ_CHECKPOINT} ADD COLUMN IF NOT EXISTS saved_cpu_seconds REAL;
CREATE INDEX IF NOT EXISTS idx_{CHECKPOINT_TABLE}_key    ON {FQ_CHECKPOINT} (stage_key);
CREATE INDEX IF NOT EXISTS idx_{CHECKPOINT_TABLE}_design ON {FQ_CHECKPOINT} (design);

-- Overflow chunks for checkpoints past one bytea's practical size, same
-- scheme as pd_blob_chunks. The parent pd_checkpoints row keeps the metadata
-- with an empty data column; fetch_checkpoint reassembles by seq. Without
-- this, rocket-scale par checkpoints (innovus DB dirs beyond the old 450 MB
-- cap) were refused and a failed par's progress lived only on local disk.
CREATE TABLE IF NOT EXISTS {FQ_CHECKPOINT_CHUNK} (
    stage_key TEXT NOT NULL,
    step      TEXT NOT NULL,
    seq       INT NOT NULL,
    data      BYTEA NOT NULL,
    PRIMARY KEY (stage_key, step, seq)
);

-- Design sharing. The latest bundle of each (owner, design): its configs, RTL and the collateral they
-- name, tarred and gzipped, with a JSON manifest. Bundles past one bytea's practical size spill into
-- pd_design_bundle_chunks, as pd_blobs does.
CREATE TABLE IF NOT EXISTS {FQ_DESIGN_BUNDLE} (
    owner       TEXT NOT NULL DEFAULT current_user,
    design      TEXT NOT NULL,
    bundle_sha  TEXT NOT NULL,
    manifest    JSONB NOT NULL,
    data        BYTEA NOT NULL,
    size_bytes  BIGINT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner, design)
);
CREATE TABLE IF NOT EXISTS {FQ_DESIGN_BUNDLE_CHUNK} (
    owner  TEXT NOT NULL DEFAULT current_user,
    design TEXT NOT NULL,
    seq    INT NOT NULL,
    data   BYTEA NOT NULL,
    PRIMARY KEY (owner, design, seq)
);
-- Who may pull which design: one row per (owner, design, grantee). The owner adds and removes them.
CREATE TABLE IF NOT EXISTS {FQ_DESIGN_SHARE} (
    owner      TEXT NOT NULL DEFAULT current_user,
    design     TEXT NOT NULL,
    grantee    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (owner, design, grantee)
);

-- The database, not the client, enforces sharing. Each user connects as their own role; an owner reads
-- and writes only their own rows, and a grantee can only read a bundle whose owner shared that design
-- with them. FORCE holds the table owner (whoever ran studio init) to the same rules.
ALTER TABLE {FQ_DESIGN_SHARE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {FQ_DESIGN_SHARE} FORCE ROW LEVEL SECURITY;
ALTER TABLE {FQ_DESIGN_BUNDLE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {FQ_DESIGN_BUNDLE} FORCE ROW LEVEL SECURITY;
ALTER TABLE {FQ_DESIGN_BUNDLE_CHUNK} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {FQ_DESIGN_BUNDLE_CHUNK} FORCE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = '{SCHEMA_NAME}'
                   AND tablename = '{DESIGN_SHARE_TABLE}' AND policyname = 'design_shares_owner') THEN
        CREATE POLICY design_shares_owner ON {FQ_DESIGN_SHARE} FOR ALL
            USING (owner = current_user) WITH CHECK (owner = current_user);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = '{SCHEMA_NAME}'
                   AND tablename = '{DESIGN_SHARE_TABLE}' AND policyname = 'design_shares_grantee') THEN
        CREATE POLICY design_shares_grantee ON {FQ_DESIGN_SHARE} FOR SELECT
            USING (grantee = current_user);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = '{SCHEMA_NAME}'
                   AND tablename = '{DESIGN_BUNDLE_TABLE}' AND policyname = 'design_bundles_owner') THEN
        CREATE POLICY design_bundles_owner ON {FQ_DESIGN_BUNDLE} FOR ALL
            USING (owner = current_user) WITH CHECK (owner = current_user);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = '{SCHEMA_NAME}'
                   AND tablename = '{DESIGN_BUNDLE_TABLE}' AND policyname = 'design_bundles_shared') THEN
        CREATE POLICY design_bundles_shared ON {FQ_DESIGN_BUNDLE} FOR SELECT
            USING (EXISTS (SELECT 1 FROM {FQ_DESIGN_SHARE} s
                           WHERE s.owner = {DESIGN_BUNDLE_TABLE}.owner AND s.design = {DESIGN_BUNDLE_TABLE}.design
                             AND s.grantee = current_user));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = '{SCHEMA_NAME}'
                   AND tablename = '{DESIGN_BUNDLE_CHUNK_TABLE}' AND policyname = 'design_bundle_chunks_owner') THEN
        CREATE POLICY design_bundle_chunks_owner ON {FQ_DESIGN_BUNDLE_CHUNK} FOR ALL
            USING (owner = current_user) WITH CHECK (owner = current_user);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = '{SCHEMA_NAME}'
                   AND tablename = '{DESIGN_BUNDLE_CHUNK_TABLE}' AND policyname = 'design_bundle_chunks_shared') THEN
        CREATE POLICY design_bundle_chunks_shared ON {FQ_DESIGN_BUNDLE_CHUNK} FOR SELECT
            USING (EXISTS (SELECT 1 FROM {FQ_DESIGN_SHARE} s
                           WHERE s.owner = {DESIGN_BUNDLE_CHUNK_TABLE}.owner
                             AND s.design = {DESIGN_BUNDLE_CHUNK_TABLE}.design AND s.grantee = current_user));
    END IF;
END $$;

-- Nobody gets access by default. The group role is the only way in.
REVOKE ALL ON SCHEMA {SCHEMA_NAME} FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA_NAME} FROM PUBLIC;

-- Hand read+write on the schema to the group role, if it exists yet.
-- If a DBA hasn't created the role, this block just no-ops and we re-run init later.
-- UPDATE and DELETE included so INSERT ... ON CONFLICT DO UPDATE works for group members.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{SLEDGEHAMMER_GROUP}') THEN
        EXECUTE 'GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {SLEDGEHAMMER_GROUP}';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA_NAME} TO {SLEDGEHAMMER_GROUP}';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_NAME} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {SLEDGEHAMMER_GROUP}';
        -- BIGSERIAL ids: inserts into the ledger tables need nextval on their sequences
        EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA_NAME} TO {SLEDGEHAMMER_GROUP}';
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_NAME} GRANT USAGE, SELECT ON SEQUENCES TO {SLEDGEHAMMER_GROUP}';
    END IF;
END $$;
"""

# The DDL stamps its own hash on pd_checkpoints, so hot paths can tell whether the schema is current without taking locks.
_DDL_VERSION = "sledgehammer:" + hashlib.sha256(_DDL.encode("utf-8")).hexdigest()[:16]
_DDL_VERSIONED = _DDL + f"\nCOMMENT ON TABLE {FQ_CHECKPOINT} IS '{_DDL_VERSION}';\n"
_schema_settled: Set[Tuple[Any, ...]] = set()


def _ensure_schema(conn, quiet: bool = False) -> None:
    """Create the schema + table if they don't exist. Safe to call repeatedly.

    If ``quiet`` is True, swallow InsufficientPrivilege errors. The DDL needs
    CREATE-on-database to evaluate (even ``IF NOT EXISTS``), so a non-owner
    user who's already in sledgehammer_users would otherwise fail here on
    every write. The schema already exists when that user is calling, so
    swallowing the error and continuing is the correct behavior.

    Quiet calls (every hot path) run the DDL only when the version stamp is
    behind, at most once per process and database, and give up after 2 s
    rather than queue their ACCESS EXCLUSIVE locks behind a long transfer.
    """
    info = getattr(conn, "info", None)
    key = (getattr(info, "host", None), getattr(info, "port", None), getattr(info, "dbname", None))
    if not quiet:
        with conn.cursor() as cur:
            cur.execute(_DDL_VERSIONED)
        conn.commit()
        _schema_settled.add(key)
        return
    if key in _schema_settled:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT obj_description(to_regclass(%s), 'pg_class')", (FQ_CHECKPOINT,))
        row = cur.fetchone()
    conn.commit()
    if row is not None and row[0] == _DDL_VERSION:
        _schema_settled.add(key)
        return
    try:
        with conn.cursor() as cur:
            # one execute, so the timeout holds even on an autocommit connection
            cur.execute("SET LOCAL lock_timeout = '2s';\n" + _DDL_VERSIONED)
        conn.commit()
        _schema_settled.add(key)
    except psycopg2.errors.InsufficientPrivilege:
        conn.rollback()
        _schema_settled.add(key)
    except psycopg2.errors.LockNotAvailable:
        conn.rollback()


def ensure_schema() -> None:
    """Public entry point to create the schema + table (idempotent)."""
    conn = _connect()
    try:
        _ensure_schema(conn)
    finally:
        conn.close()


def _valid_role(role: str) -> str:
    import re
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
        raise ValueError(f"not a valid Postgres role name: {role!r}")
    return role


def grant_schema_access(role: str) -> None:
    """Grant a role direct read/write on the hammer_poc schema.

    The group-role path (sledgehammer_users) needs CREATEROLE to exist; on
    clusters where nobody has it (the BWRC one), each teammate gets these
    direct grants instead.
    """
    _valid_role(role)
    with _connect() as conn:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            for sql in (
                f"GRANT USAGE ON SCHEMA {SCHEMA_NAME} TO {role}",
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA_NAME} TO {role}",
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA_NAME} TO {role}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_NAME} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA_NAME} GRANT USAGE, SELECT ON SEQUENCES TO {role}",
            ):
                cur.execute(sql)
        conn.commit()


def onboard_user(role: str) -> List[str]:
    """Everything a new teammate needs, idempotently. Returns action log.

    1. their Airflow metadata database (airflow_<role>)
    2. direct grants on the hammer_poc schema (cache, ledger, checkpoints)
    """
    _valid_role(role)
    actions: List[str] = []
    conn = _connect()
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if not cur.fetchone():
                raise RuntimeError(
                    f"no Postgres role {role!r} on the cluster; ask the DB admin to CREATE ROLE")
            db = f"airflow_{role}"
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
            if cur.fetchone():
                actions.append(f"database {db}: already exists")
            else:
                cur.execute(f'CREATE DATABASE "{db}"')
                actions.append(f"database {db}: created")
    finally:
        conn.close()
    grant_schema_access(role)
    actions.append("hammer_poc access: granted")
    return actions


def grant_access(role: str) -> None:
    """
    Add a Postgres role to the sledgehammer_users group.

    The user then inherits read and write on every SledgeHammer table. You
    need ADMIN OPTION on the group role for this to work; the DBA hands that
    over when they first create the role.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"GRANT {SLEDGEHAMMER_GROUP} TO {role}")
        conn.commit()
    finally:
        conn.close()


def revoke_access(role: str) -> None:
    """Remove a Postgres role from the sledgehammer_users group."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"REVOKE {SLEDGEHAMMER_GROUP} FROM {role}")
        conn.commit()
    finally:
        conn.close()


def _valid_email(email: str) -> bool:
    """Conservative address check: one @, a dotted domain, and none of the
    whitespace or separators that could smuggle extra recipients into a header.
    """
    email = (email or "").strip()
    if not email or any(c in email for c in " \t\r\n,;<>"):
        return False
    local, _, domain = email.partition("@")
    return bool(local) and "@" not in domain and "." in domain \
        and not domain.startswith(".") and not domain.endswith(".")


def set_notify_email(uid: str, email: str) -> None:
    """Register or update the address a user opted into for completion emails.

    Upsert keyed on uid. The caller is responsible for making sure uid is the
    person setting their own address -- the self-service web form takes it from
    the logged-in session, never from request input.
    """
    uid = (uid or "").strip().lower()
    # Accept a comma-separated list; the mailer sends to every address in the
    # To: header, so multiple recipients need no other support.
    parts = [a.strip() for a in (email or "").split(",") if a.strip()]
    email = ", ".join(parts)
    if not uid:
        raise ValueError("no uid given for notify-email registration")
    if not parts or not all(_valid_email(a) for a in parts):
        raise ValueError(f"not a valid email address: {email!r}")
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {FQ_NOTIFY_EMAIL} (uid, email) VALUES (%s, %s) "
                f"ON CONFLICT (uid) DO UPDATE SET email = EXCLUDED.email, updated_at = NOW()",
                (uid, email),
            )
        conn.commit()
    finally:
        conn.close()


def get_notify_email(uid: str) -> Optional[str]:
    """Return the address a user opted into, or None if they haven't set one."""
    uid = (uid or "").strip().lower()
    if not uid:
        return None
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT email FROM {FQ_NOTIFY_EMAIL} WHERE uid = %s", (uid,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    finally:
        conn.close()


def delete_notify_email(uid: str) -> bool:
    """Drop a user's opted-in address (opt back out). True if a row was removed."""
    uid = (uid or "").strip().lower()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {FQ_NOTIFY_EMAIL} WHERE uid = %s", (uid,))
            removed = cur.rowcount > 0
        conn.commit()
        return removed
    finally:
        conn.close()


def list_notify_emails() -> list:
    """Return [(uid, email, updated_at), ...] for everyone who opted in."""
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(f"SELECT uid, email, updated_at FROM {FQ_NOTIFY_EMAIL} ORDER BY uid")
            return cur.fetchall()
    finally:
        conn.close()


def lookup_triggering_user(dag_id: str, run_id: str) -> Optional[str]:
    """Read a dag_run's triggering_user_name from the metadata DB.

    Airflow 3 hides triggering_user_name from tasks and callbacks, so we read the
    row directly over the metadata connection (the SLEDGE_ conn file on the
    callback path, since the sandbox conn is a decoy). None on any error.
    """
    if not dag_id or not run_id:
        return None
    try:
        import psycopg2
        settings = airflow_metadata_conn_settings()
        if not settings:
            print("[notify] db lookup: no metadata conn resolved "
                  "(env/SLEDGE_METADATA_CONN/file/conf/cfg all empty)")
            return None
        conn = psycopg2.connect(**{"connect_timeout": _connect_timeout(), **settings})
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT triggering_user_name FROM dag_run "
                    "WHERE dag_id = %s AND run_id = %s",
                    (dag_id, run_id),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception as e:
        print(f"[notify] db lookup error: {e}")
        return None


def get_user_workspace(username: Optional[str], workspace_name: str = "default",
                       auto_register: bool = True) -> Optional[str]:
    """
    Resolve the workspace root for the given user + named workspace.

    Returns an absolute path under which the user's build artifacts for this
    workspace must live. A user may have MANY named workspaces (so they can
    operate in several at once); ``workspace_name`` selects which one, and
    defaults to ``'default'``. No row for that (user, workspace) pair means it
    hasn't been registered yet, and this function auto-registers one with a
    sensible default (a per-user[-workspace] subdirectory under the Airflow
    daemon user's ``hammer`` checkout, so file-system permissions work out of
    the box on a shared deployment).

    Callers should append a per-design subdirectory (e.g. ``/gcd``) to the
    returned path before using it as ``OBJ_DIR``.

    Falls back to the OS user if ``username`` is empty or None, and to the
    'default' workspace if ``workspace_name`` is empty or None.
    """
    if not username:
        try:
            username = getpass.getuser()
        except Exception:
            username = "default"
    if not workspace_name:
        workspace_name = "default"

    conn = _connect()
    conn.autocommit = True
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT workspace_root FROM {FQ_WORKSPACE} "
                f"WHERE username = %s AND workspace_name = %s",
                (username, workspace_name),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]

            # With auto_register=False this is a pure read: report the miss and
            # write nothing. Flow runs use this so resolving a workspace can
            # never author a row as a side effect -- only an explicit
            # `studio workspace-set` creates rows.
            if not auto_register:
                return None

            # Auto-register this (user, workspace). Anchor the default under the
            # checkout THIS code is running from -- i.e. the user's OWN hammer
            # working copy -- so each user's builds land in their own directory,
            # not whichever daemon happened to create the row first. (Anchoring
            # under the daemon's home meant a row created by user A's Airflow
            # pointed every other user at A's home.) __file__ here is
            # <checkout>/hammer/vlsi/pd_store.py, so three dirnames up is the
            # checkout root; fall back to the triggering user's home only if that
            # checkout has no e2e/. The build dir is always per-user
            # (build-sky130-cm-<user>); named workspaces add a '-<name>' suffix.
            checkout = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            e2e_dir = os.path.join(checkout, "e2e")
            if not os.path.isdir(e2e_dir):
                e2e_dir = os.path.join(
                    os.path.expanduser(f"~{username}"), "hammer", "e2e"
                )
            suffix = "" if workspace_name == "default" else f"-{workspace_name}"
            default = os.path.join(
                e2e_dir, f"build-sky130-cm-{username}{suffix}",
            )
            try:
                cur.execute(
                    f"INSERT INTO {FQ_WORKSPACE} (username, workspace_name, workspace_root) "
                    f"VALUES (%s, %s, %s) "
                    f"ON CONFLICT (username, workspace_name) DO NOTHING",
                    (username, workspace_name, default),
                )
            except Exception:
                # If we can't write the row (e.g. read-only role), just
                # return the default in-memory.
                pass
            return default
    finally:
        conn.close()


def list_user_workspaces(username: Optional[str] = None) -> List[Tuple[str, str, str, Any]]:
    """Return rows from user_workspaces as
    (username, workspace_name, workspace_root, updated_at). Pass ``username`` to
    list only that user's workspaces; omit it to list everyone's."""
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            if username:
                cur.execute(
                    f"SELECT username, workspace_name, workspace_root, updated_at "
                    f"FROM {FQ_WORKSPACE} WHERE username = %s "
                    f"ORDER BY username, workspace_name",
                    (username,),
                )
            else:
                cur.execute(
                    f"SELECT username, workspace_name, workspace_root, updated_at "
                    f"FROM {FQ_WORKSPACE} ORDER BY username, workspace_name"
                )
            return list(cur.fetchall())
    finally:
        conn.close()


def delete_stage_blobs(stage_tag: Optional[str] = None) -> int:
    """
    Delete rows from ``pd_blobs``. With no filter, deletes ALL rows.
    With ``stage_tag``, deletes only rows for that stage (e.g. 'synthesis').
    Then drops every pd_blob_chunks row left without a parent, which also
    clears chunks orphaned by earlier wipes.

    Returns the number of rows deleted.
    """
    conn = _connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if stage_tag:
                cur.execute(
                    f"DELETE FROM {FQ_BLOB} WHERE stage = %s",
                    (stage_tag,),
                )
            else:
                cur.execute(f"DELETE FROM {FQ_BLOB}")
            n = cur.rowcount
            cur.execute(
                f"DELETE FROM {FQ_BLOB_CHUNK} c WHERE NOT EXISTS "
                f"(SELECT 1 FROM {FQ_BLOB} b WHERE b.sha256 = c.sha256)")
            return n
    finally:
        conn.close()


def delete_master_databases(design: Optional[str] = None) -> int:
    """
    Delete rows from ``master_databases``. With no filter, deletes ALL rows.
    With ``design``, deletes only that design's row.

    Returns the number of rows deleted.
    """
    conn = _connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if design:
                cur.execute(
                    f"DELETE FROM {FQ_MASTER} WHERE design = %s",
                    (design,),
                )
            else:
                cur.execute(f"DELETE FROM {FQ_MASTER}")
            return cur.rowcount
    finally:
        conn.close()


def delete_artifacts(kind: Optional[str] = None) -> int:
    """
    Delete rows from ``pd_artifacts``. With no filter, deletes ALL rows.
    With ``kind``, deletes only rows of that kind (e.g. 'par-input').

    Returns the number of rows deleted.
    """
    conn = _connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if kind:
                cur.execute(
                    f"DELETE FROM {FQ_TABLE} WHERE kind = %s",
                    (kind,),
                )
            else:
                cur.execute(f"DELETE FROM {FQ_TABLE}")
            return cur.rowcount
    finally:
        conn.close()


def delete_user_workspace(username: str, workspace_name: Optional[str] = None) -> bool:
    """Remove a user's workspace registration(s). Returns True if a row was deleted.

    With ``workspace_name``, removes only that one named workspace; without it,
    removes ALL of the user's workspaces."""
    conn = _connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if workspace_name:
                cur.execute(
                    f"DELETE FROM {FQ_WORKSPACE} "
                    f"WHERE username = %s AND workspace_name = %s",
                    (username, workspace_name),
                )
            else:
                cur.execute(
                    f"DELETE FROM {FQ_WORKSPACE} WHERE username = %s",
                    (username,),
                )
            return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Filter-based management of pd_blobs (find / count / delete / reassign).
#
# Powers the blob-find / blob-delete / blob-reassign CLI verbs. Every filter
# is optional and ANDed together. delete/reassign refuse to run with no filter
# at all, so a stray command can never touch the whole table -- use wipe-blobs
# to clear everything on purpose.
# ---------------------------------------------------------------------------

# Columns that filter as a simple `col = %s` equality.
_BLOB_EQ_FILTERS = ("owner", "triggering_user", "design", "stage", "dag_id",
                    "workspace")


def _blob_filter_sql(
    *,
    user: Optional[str] = None,
    owner: Optional[str] = None,
    triggering_user: Optional[str] = None,
    design: Optional[str] = None,
    stage: Optional[str] = None,
    dag_id: Optional[str] = None,
    workspace: Optional[str] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    sha: Optional[str] = None,
) -> Tuple[str, list, int]:
    """
    Build a parameterized WHERE clause for pd_blobs from optional filters.

    ``user`` matches either ``owner`` OR ``triggering_user`` (the common
    "everything from this person" case); ``owner``/``triggering_user`` match
    that exact column. ``before``/``after`` bound ``created_at`` (any string
    Postgres can cast to timestamptz, e.g. '2026-06-01'). ``sha`` matches a
    sha256 prefix.

    Returns (where_sql, params, num_filters); where_sql is '' when no filter
    was given (num_filters == 0).
    """
    clauses: list = []
    params: list = []
    eq = {"owner": owner, "triggering_user": triggering_user, "design": design,
          "stage": stage, "dag_id": dag_id, "workspace": workspace}
    for col in _BLOB_EQ_FILTERS:
        if eq[col] is not None:
            clauses.append(f"{col} = %s")
            params.append(eq[col])
    if user is not None:
        clauses.append("(owner = %s OR triggering_user = %s)")
        params.extend([user, user])
    if after is not None:
        clauses.append("created_at >= %s::timestamptz")
        params.append(after)
    if before is not None:
        clauses.append("created_at < %s::timestamptz")
        params.append(before)
    if sha is not None:
        # A bare LIKE would let "" (or "%") match every blob and slip past the
        # callers' "refuse with no filter" guard.
        if not re.fullmatch(r"[0-9a-fA-F]+", sha):
            raise ValueError(f"--sha must be a non-empty hex prefix, got {sha!r}")
        clauses.append("left(sha256, %s) = %s")
        params.extend([len(sha), sha.lower()])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, len(clauses)


# Columns returned by find_blobs -- same shape/order as list_stage_blobs.
_BLOB_LIST_COLS = ("sha256, stage, size_bytes, duration_seconds, cpu_seconds, "
                   "owner, triggering_user, dag_id, design, workspace, created_at")


def find_blobs(limit: Optional[int] = 50, **filters: Any) -> List[Tuple[Any, ...]]:
    """List pd_blobs rows matching the filters (see _blob_filter_sql), newest
    first. ``limit`` caps the row count (None = no cap)."""
    where, params, _ = _blob_filter_sql(**filters)
    sql = f"SELECT {_BLOB_LIST_COLS} FROM {FQ_BLOB}{where} ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT %s"
        params = params + [limit]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def count_blobs(**filters: Any) -> Tuple[int, int]:
    """Return (row_count, total_size_bytes) for pd_blobs rows matching filters."""
    where, params, _ = _blob_filter_sql(**filters)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM {FQ_BLOB}{where}",
                params,
            )
            row = cur.fetchone()
            return int(row[0]), int(row[1])
    finally:
        conn.close()


def delete_blobs(**filters: Any) -> int:
    """
    Delete pd_blobs rows matching the filters. Returns rows deleted.

    Refuses to run with no filter (raises ValueError) so a stray blob-delete
    can never clear the whole table -- use wipe-blobs for that.
    """
    where, params, n = _blob_filter_sql(**filters)
    if n == 0:
        raise ValueError(
            "refusing to delete with no filter; pass at least one of "
            "--user/--design/--stage/--before/--after/--sha (or use wipe-blobs)."
        )
    conn = _connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # the bytes live in the chunk table with no cascade, so drop them
            # first or a delete frees nothing
            cur.execute(
                f"DELETE FROM {FQ_BLOB_CHUNK} WHERE sha256 IN "
                f"(SELECT sha256 FROM {FQ_BLOB}{where})", params)
            cur.execute(f"DELETE FROM {FQ_BLOB}{where}", params)
            return cur.rowcount
    finally:
        conn.close()


def reassign_blobs(
    *,
    set_owner: Optional[str] = None,
    set_triggering_user: Optional[str] = None,
    set_design: Optional[str] = None,
    set_workspace: Optional[str] = None,
    **filters: Any,
) -> int:
    """
    Update provenance columns (owner / triggering_user / design / workspace)
    on pd_blobs rows matching the filters. Returns rows updated.

    Refuses to run with no filter, or with nothing to set. Handy for
    "move user A's blobs to workspace X" or "retag a design".
    """
    where, params, n = _blob_filter_sql(**filters)
    if n == 0:
        raise ValueError("refusing to update with no filter; narrow it with --user/--design/etc.")
    set_cols: list = []
    set_params: list = []
    for col, val in (("owner", set_owner),
                     ("triggering_user", set_triggering_user),
                     ("design", set_design),
                     ("workspace", set_workspace)):
        if val is not None:
            set_cols.append(f"{col} = %s")
            set_params.append(val)
    if not set_cols:
        raise ValueError("nothing to set; pass at least one --set-* value.")
    sql = f"UPDATE {FQ_BLOB} SET {', '.join(set_cols)}{where}"
    conn = _connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, set_params + params)
            return cur.rowcount
    finally:
        conn.close()


def set_user_workspace(username: str, workspace_root: str,
                       workspace_name: str = "default") -> None:
    """Explicitly set or update one named workspace root for a user.

    ``workspace_name`` defaults to 'default'; pass a distinct name to register
    an additional workspace the user can run in concurrently."""
    if not username:
        raise ValueError("username must be non-empty")
    if not workspace_root:
        raise ValueError("workspace_root must be non-empty")
    if not workspace_name:
        workspace_name = "default"
    conn = _connect()
    conn.autocommit = True
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {FQ_WORKSPACE} (username, workspace_name, workspace_root) "
                f"VALUES (%s, %s, %s) "
                f"ON CONFLICT (username, workspace_name) "
                f"DO UPDATE SET workspace_root = EXCLUDED.workspace_root, "
                f"              updated_at     = NOW()",
                (username, workspace_name, workspace_root),
            )
    finally:
        conn.close()


def _canonical_json(data: Dict[str, Any]) -> str:
    """
    Produce a deterministic JSON string for hashing.

    Uses sorted keys and no whitespace so logically equal dicts
    produce identical bytes across runs/machines.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        cls=HammerJSONEncoder,
        ensure_ascii=False,
    )


def compute_sha256(data: Dict[str, Any]) -> str:
    """Return the hex SHA256 of the canonical JSON form of ``data``."""
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def _extract_top_module(data: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of the top module name for convenience."""
    for key in ("synthesis.inputs.top_module", "par.inputs.top_module", "vlsi.inputs.top_module"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def _env_provenance() -> Dict[str, Optional[str]]:
    """Pull airflow / workspace / design provenance from env vars set by
    the AIRFlow class. Returns a dict with the relevant fields, or Nones
    when not running under Airflow."""
    return {
        "triggering_user": os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER") or None,
        "dag_id":          os.environ.get("HAMMER_AIRFLOW_DAG_ID") or None,
        "dag_run_id":      os.environ.get("HAMMER_AIRFLOW_RUN_ID") or None,
        "workspace":       os.environ.get("HAMMER_AIRFLOW_WORKSPACE") or None,
        "design":          os.environ.get("design") or None,
    }


def store_artifact(
    data: Dict[str, Any],
    kind: str,
    triggering_user: Optional[str] = None,
    dag_id: Optional[str] = None,
    dag_run_id: Optional[str] = None,
    workspace: Optional[str] = None,
    design: Optional[str] = None,
) -> str:
    """
    Store ``data`` as an artifact of the given ``kind`` and return its SHA256 hex.

    Provenance kwargs default to the env-var values populated by AIRFlow.
    Pass explicit values to override.
    """
    sha = compute_sha256(data)
    top_module = _extract_top_module(data)
    prov = _env_provenance()
    triggering_user = triggering_user or prov["triggering_user"]
    dag_id          = dag_id          or prov["dag_id"]
    dag_run_id      = dag_run_id      or prov["dag_run_id"]
    workspace       = workspace       or prov["workspace"]
    design          = design          or prov["design"]
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_TABLE} (
                    sha256, kind, top_module, data,
                    triggering_user, dag_id, dag_run_id, workspace, design
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                """,
                (sha, kind, top_module, Json(data),
                 triggering_user, dag_id, dag_run_id, workspace, design),
            )
        conn.commit()
    finally:
        conn.close()
    return sha


def store_par_input(data: Dict[str, Any]) -> str:
    """Convenience wrapper: store a par-input dict and return its SHA256 hex."""
    return store_artifact(data, kind="par-input")


def load_artifact(sha256: str) -> Optional[Dict[str, Any]]:
    """Fetch an artifact by SHA256. Returns the JSON payload as a dict, or None."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT data FROM {FQ_TABLE} WHERE sha256 = %s",
                (sha256,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row[0]


def list_artifacts(limit: int = 20) -> List[Tuple[Any, ...]]:
    """
    List the most recent artifacts.

    Returns tuples of
      (sha256, kind, top_module, owner, triggering_user, dag_id, design,
       workspace, created_at).
    """
    cols = ("sha256, kind, top_module, owner, triggering_user, dag_id, "
            "design, workspace, created_at")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM {FQ_TABLE} ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SledgeHammer Studio: master_database + per-stage blob store.
#
# A stage's cache key is the SHA256 of the slice of master_database that
# stage_change_check actually compares: every key whose prefix is the stage tag
# (excluding outputs), plus every "global" key that is not owned by any stage.
# Stage-internal flags like *.needsToRerun are excluded since they are run
# bookkeeping, not cache inputs.
# ---------------------------------------------------------------------------


def _stage_relevant_keys(master_db: Dict[str, Any], stage_tag: str) -> Dict[str, Any]:
    from hammer.vlsi.fingerprints import OWNED_STAGE_TAGS
    own_prefix = stage_tag + "."
    output_prefix = stage_tag + ".outputs"
    out: Dict[str, Any] = {}
    for k, v in master_db.items():
        if k.endswith(".needsToRerun") or k in RUN_CONTROL_KEYS:
            continue
        if k.startswith(own_prefix):
            if not k.startswith(output_prefix):
                out[k] = v
        elif not k.startswith(OWNED_STAGE_TAGS):
            out[k] = v
    return out


def compute_stage_key(master_db: Dict[str, Any], stage_tag: str) -> str:
    """SHA256 over the master_database slice that determines this stage's output."""
    if stage_tag not in KNOWN_STAGE_TAGS:
        raise ValueError(
            f"Unknown stage tag {stage_tag!r}. Expected one of {KNOWN_STAGE_TAGS}."
        )
    return compute_sha256(_stage_relevant_keys(master_db, stage_tag))


def compute_rtl_fingerprint(file_paths: List[str],
                            defines: Optional[List[str]] = None,
                            top_module: Optional[str] = None,
                            include_dirs: Optional[List[str]] = None) -> str:
    """Fingerprint the given RTL files by their elaborated SystemVerilog design.

    Delegates to :mod:`hammer.vlsi.rtl_check` so that this and the cli_driver's
    ``vlsi.rtl_fingerprint_sha256`` are the same value
    """
    from hammer.vlsi import rtl_check
    overall, _fallback = rtl_check.digest_or_bytes(file_paths,
                                                   include_dirs=include_dirs or [],
                                                   defines=defines or [],
                                                   top_module=top_module)
    return overall


# Tool collateral the config names by path: physical/timing libraries,
# extraction decks, layout.
COLLATERAL_EXTENSIONS = (
    ".lef", ".tlef", ".lib", ".lib.gz", ".db", ".ldb",
    ".tluplus", ".captable", ".qrc", ".qrctech",
    ".gds", ".gds.gz", ".oas", ".cdl", ".spi", ".sp", ".spice",
    ".v", ".sv",
)


def _walk_config_paths(value: Any, out: Set[str], field: Optional[str] = None,
                       include_fields: Optional[Tuple[str, ...]] = None,
                       exclude_fields: Tuple[str, ...] = ()) -> None:
    """Collect collateral-looking absolute paths from config values.

    ``field`` is the nearest enclosing dict key, so paths can be kept or
    dropped by the field that names them (a list under "spice_file" keeps
    that field for every element). include_fields keeps ONLY those fields;
    exclude_fields drops them; both default to the old keep-everything walk.
    """
    if isinstance(value, str):
        lower = value.lower()
        if lower.endswith(COLLATERAL_EXTENSIONS) and os.path.isabs(value):
            if field in exclude_fields:
                return
            if include_fields is not None and field not in include_fields:
                return
            out.add(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk_config_paths(v, out, field=k, include_fields=include_fields,
                               exclude_fields=exclude_fields)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_config_paths(v, out, field=field, include_fields=include_fields,
                               exclude_fields=exclude_fields)


# Kept for the EECS 151 Lab 5 launcher, which wraps this function at import.
def compute_collateral_fingerprint(
    config: Dict[str, Any],
    exclude_files: Optional[Set[str]] = None,
    exclude_prefixes: Tuple[str, ...] = (),
    extra_files: Optional[List[str]] = None,
    include_fields: Optional[Tuple[str, ...]] = None,
    exclude_fields: Tuple[str, ...] = (),
) -> str:
    """Stat-fingerprint (path, size, mtime, ctime) of the tool collateral: library-ish
    absolute paths found in the config values, plus ``extra_files`` (the
    technology's own library list, which is not in the config). Stat follows
    symlinks and is consistent across machines on shared storage. Exclusions:
    RTL (content-fingerprinted separately) and the stage output dir (mtimes
    churn every run).

    include_fields / exclude_fields scope the config walk by the field name
    that holds each path, so a fingerprint can cover only the collateral a
    given stage consumes (e.g. spice files belong to LVS alone -- an edit
    there must not mark synthesis stale). Fields nobody classified stay in
    the default fingerprint: the safe failure mode is a wasted rebuild,
    never a stale result that skips.
    """
    exclude_files = exclude_files or set()
    paths: Set[str] = set()
    _walk_config_paths(config, paths, include_fields=include_fields,
                       exclude_fields=exclude_fields)
    keep = sorted(
        p for p in paths
        if p not in exclude_files
        and not (exclude_prefixes and p.startswith(exclude_prefixes))
    )
    keep += sorted(set(extra_files or []))
    from hammer.vlsi import fingerprints
    lines = [fingerprints.file_line(path) for path in keep]
    fingerprints.debug_record("collateral", lines)
    return fingerprints.digest_lines(lines)


def store_master_database(
    design: str,
    master_db: Dict[str, Any],
    triggering_user: Optional[str] = None,
    dag_id: Optional[str] = None,
    dag_run_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> None:
    """Upsert the master_database for ``design``. Latest write wins.

    Provenance kwargs default to env-var values populated by AIRFlow.
    """
    prov = _env_provenance()
    triggering_user = triggering_user or prov["triggering_user"]
    dag_id          = dag_id          or prov["dag_id"]
    dag_run_id      = dag_run_id      or prov["dag_run_id"]
    workspace       = workspace       or prov["workspace"]
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_MASTER} (
                    design, db, owner, triggering_user, dag_id,
                    dag_run_id, workspace, updated_at
                )
                VALUES (%s, %s, current_user, %s, %s, %s, %s, NOW())
                ON CONFLICT (design) DO UPDATE
                  SET db              = EXCLUDED.db,
                      owner           = current_user,
                      triggering_user = COALESCE(EXCLUDED.triggering_user,
                                                 {FQ_MASTER}.triggering_user),
                      dag_id          = COALESCE(EXCLUDED.dag_id,
                                                 {FQ_MASTER}.dag_id),
                      dag_run_id      = COALESCE(EXCLUDED.dag_run_id,
                                                 {FQ_MASTER}.dag_run_id),
                      workspace       = COALESCE(EXCLUDED.workspace,
                                                 {FQ_MASTER}.workspace),
                      updated_at      = NOW()
                """,
                (design, Json(master_db), triggering_user, dag_id,
                 dag_run_id, workspace),
            )
        conn.commit()
    finally:
        conn.close()


def list_master_databases(limit: int = 50) -> List[Tuple[Any, ...]]:
    """
    Browse master_databases rows.

    Returns (design, owner, triggering_user, dag_id, workspace, updated_at).
    """
    cols = ("design, owner, triggering_user, dag_id, workspace, updated_at")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {cols} FROM {FQ_MASTER} ORDER BY updated_at DESC LIMIT %s",
                (limit,),
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def load_master_database(design: str) -> Optional[Dict[str, Any]]:
    """Fetch the master_database for ``design``, or None if not present."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT db FROM {FQ_MASTER} WHERE design = %s",
                (design,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def store_stage_blob(
    stage_tag: str,
    sha256: str,
    data: bytes,
    duration_seconds: Optional[float] = None,
    cpu_seconds: Optional[float] = None,
    triggering_user: Optional[str] = None,
    dag_id: Optional[str] = None,
    dag_run_id: Optional[str] = None,
    workspace: Optional[str] = None,
    design: Optional[str] = None,
) -> None:
    """
    Store a tarball under ``sha256``. Latest write wins.

    Tool outputs (Genus, Innovus, etc.) aren't byte-deterministic even when
    inputs are: log timestamps, machine IDs, and synthesis-tool internal
    randomness mean two runs with the same config + RTL produce tarballs
    that differ slightly even though they're functionally equivalent. We
    upsert on ``sha256`` (which is the content hash of the *inputs*, not
    the tarball bytes) so the cache always reflects the freshest tool
    output rather than whoever happened to write first.

    Optional provenance fields (triggering_user, dag_id, dag_run_id,
    workspace, design) are recorded so blob-list can tell you which run
    produced each row. They default to None when the caller doesn't have
    them, in which case any existing value on UPDATE is preserved.

    Blobs above BLOB_CHUNK_BYTES don't fit in one bytea value (Postgres
    caps single allocations at 1 GiB), so they're split across
    pd_blob_chunks rows in the same transaction; the parent row keeps the
    metadata with an empty data column and load_stage_blob reassembles.
    """
    chunks = [data[i:i + BLOB_CHUNK_BYTES]
              for i in range(0, len(data), BLOB_CHUNK_BYTES)]
    inline = data if len(chunks) <= 1 else b""
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_BLOB} (
                    sha256, stage, data, size_bytes, duration_seconds,
                    cpu_seconds,
                    triggering_user, dag_id, dag_run_id, workspace, design
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO UPDATE
                  SET stage            = EXCLUDED.stage,
                      data             = EXCLUDED.data,
                      size_bytes       = EXCLUDED.size_bytes,
                      owner            = current_user,
                      duration_seconds = COALESCE(EXCLUDED.duration_seconds,
                                                  {FQ_BLOB}.duration_seconds),
                      cpu_seconds      = COALESCE(EXCLUDED.cpu_seconds,
                                                  {FQ_BLOB}.cpu_seconds),
                      triggering_user  = COALESCE(EXCLUDED.triggering_user,
                                                  {FQ_BLOB}.triggering_user),
                      dag_id           = COALESCE(EXCLUDED.dag_id,
                                                  {FQ_BLOB}.dag_id),
                      dag_run_id       = COALESCE(EXCLUDED.dag_run_id,
                                                  {FQ_BLOB}.dag_run_id),
                      workspace        = COALESCE(EXCLUDED.workspace,
                                                  {FQ_BLOB}.workspace),
                      design           = COALESCE(EXCLUDED.design,
                                                  {FQ_BLOB}.design),
                      created_at       = NOW()
                """,
                (sha256, stage_tag, psycopg2.Binary(inline), len(data),
                 duration_seconds, cpu_seconds, triggering_user, dag_id,
                 dag_run_id, workspace, design),
            )
            # Drop chunks from any previous write of this sha unconditionally
            # so an inline rewrite can't leave stale overflow rows behind.
            cur.execute(f"DELETE FROM {FQ_BLOB_CHUNK} WHERE sha256 = %s",
                        (sha256,))
            if len(chunks) > 1:
                for seq, chunk in enumerate(chunks):
                    cur.execute(
                        f"INSERT INTO {FQ_BLOB_CHUNK} (sha256, seq, data) "
                        f"VALUES (%s, %s, %s)",
                        (sha256, seq, psycopg2.Binary(chunk)),
                    )
        conn.commit()
    finally:
        conn.close()


def stage_blob_runtime(sha256: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    """(duration_seconds, cpu_seconds) of a stored blob without fetching its bytes, or None if absent."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT duration_seconds, cpu_seconds FROM {FQ_BLOB} WHERE sha256 = %s", (sha256,))
            row = cur.fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def load_stage_blob(
    sha256: str,
) -> Optional[Tuple[str, bytes, Optional[float], Optional[float]]]:
    """
    Fetch a tarball by hash. Returns
    ``(stage, bytes, duration_seconds, cpu_seconds)`` or None.

    The third and fourth tuple elements are the wall-clock and CPU
    (user + sys, summed across child procs) seconds the tool took when this
    blob was originally produced. Either may be None for blobs that predate
    the corresponding column.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT stage, data, duration_seconds, cpu_seconds "
                f"FROM {FQ_BLOB} WHERE sha256 = %s",
                (sha256,),
            )
            row = cur.fetchone()
            chunk_rows = []
            if row is not None and (row[1] is None or len(row[1]) == 0):
                # An empty data column means the tarball was too big for one
                # bytea value and lives in pd_blob_chunks (see store side).
                try:
                    cur.execute(
                        f"SELECT data FROM {FQ_BLOB_CHUNK} "
                        f"WHERE sha256 = %s ORDER BY seq",
                        (sha256,),
                    )
                    chunk_rows = cur.fetchall()
                except psycopg2.Error:
                    conn.rollback()
    finally:
        conn.close()
    if row is None:
        return None
    stage, data, duration_seconds, cpu_seconds = row
    blob = bytes(data) if data is not None else b""
    if not blob and chunk_rows:
        blob = b"".join(bytes(c[0]) for c in chunk_rows)
    return stage, blob, duration_seconds, cpu_seconds


# --- Durable cache-event ledger (powers the time-saved tracker) ---------------

# Columns selected/returned by fetch_cache_events, in order. Kept as a module
# constant so the SELECT and the row->dict mapping can't drift apart.
_CACHE_EVENT_COLS = (
    "ts", "stage", "outcome",
    "saved_seconds", "tool_seconds", "restore_seconds", "store_seconds",
    "saved_cpu_seconds", "tool_cpu_seconds",
    "owner", "triggering_user", "dag_id", "dag_run_id", "workspace", "design",
    "project", "make_would_rerun", "module",
    "sha256",
)


def _cache_event_where(
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
    dag: Optional[str] = None,
    design: Optional[str] = None,
    stage: Optional[str] = None,
    user: Optional[str] = None,
    project: Optional[str] = None,
    module: Optional[str] = None,
    outcome: Optional[str] = None,
) -> Tuple[str, List[Any], int]:
    """Build the shared WHERE clause for cache-event queries.

    Returns ``(where_sql, params, n_filters)``. ``n_filters`` lets destructive
    callers (clear_cache_events) refuse to run unfiltered unless told to.
    """
    clauses: List[str] = []
    params: List[Any] = []
    if since is not None:
        clauses.append("ts >= to_timestamp(%s)")
        params.append(float(since))
    if until is not None:
        clauses.append("ts <= to_timestamp(%s)")
        params.append(float(until))
    if dag:
        clauses.append("dag_id ILIKE %s")
        params.append(f"%{dag}%")
    if design:
        clauses.append("design ILIKE %s")
        params.append(f"%{design}%")
    if stage:
        clauses.append("stage ILIKE %s")
        params.append(f"%{stage}%")
    if project:
        clauses.append("project ILIKE %s")
        params.append(f"%{project}%")
    if module:
        clauses.append("module ILIKE %s")
        params.append(f"%{module}%")
    if outcome:
        clauses.append("outcome = %s")
        params.append(outcome)
    if user:
        clauses.append("(triggering_user ILIKE %s OR owner ILIKE %s)")
        params.extend([f"%{user}%", f"%{user}%"])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params, len(clauses)


def record_cache_event(
    stage: str,
    outcome: str,
    *,
    saved_seconds: Optional[float] = None,
    tool_seconds: Optional[float] = None,
    restore_seconds: Optional[float] = None,
    store_seconds: Optional[float] = None,
    saved_cpu_seconds: Optional[float] = None,
    tool_cpu_seconds: Optional[float] = None,
    triggering_user: Optional[str] = None,
    dag_id: Optional[str] = None,
    dag_run_id: Optional[str] = None,
    workspace: Optional[str] = None,
    design: Optional[str] = None,
    project: Optional[str] = None,
    make_would_rerun: Optional[bool] = None,
    module: Optional[str] = None,
    sha256: Optional[str] = None,
) -> None:
    """Append one cache event to the durable Postgres ledger ({FQ_CACHE_EVENT}).

    Mirrors the JSONL event pd_cache writes per run, but persists past the
    exit_ task's cleanup so savings can be totalled across every run of a
    tapeout. Append-only. Callers should treat this as best-effort telemetry
    and wrap it -- it must never fail a flow (the DB may be unreachable, or the
    caller may have no Postgres password configured).
    """
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {FQ_CACHE_EVENT} (
                    stage, outcome, saved_seconds, tool_seconds, restore_seconds,
                    store_seconds, saved_cpu_seconds, tool_cpu_seconds,
                    triggering_user, dag_id, dag_run_id, workspace, design,
                    project, make_would_rerun, module, sha256
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (stage, outcome, saved_seconds, tool_seconds, restore_seconds,
                 store_seconds, saved_cpu_seconds, tool_cpu_seconds,
                 triggering_user, dag_id, dag_run_id, workspace, design,
                 project, make_would_rerun, module, sha256),
            )
        conn.commit()
    finally:
        conn.close()


def fetch_cache_events(
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
    dag: Optional[str] = None,
    design: Optional[str] = None,
    stage: Optional[str] = None,
    user: Optional[str] = None,
    project: Optional[str] = None,
    module: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read events from the durable ledger as a list of dicts.

    Each dict uses the same keys as the JSONL events pd_cache writes (``ts`` as
    an epoch float, ``stage_tag``, ``outcome``, ``saved_seconds`` ...) so the
    aggregator in pd_cache can treat DB and JSONL rows identically.

    Filters (all optional, ANDed): ``since``/``until`` are epoch seconds;
    ``dag``/``design``/``stage``/``user`` match a substring (ILIKE); ``outcome``
    matches exactly. ``user`` checks both triggering_user and owner.
    """
    where, params, _ = _cache_event_where(
        since=since, until=until, dag=dag, design=design,
        stage=stage, user=user, project=project, module=module, outcome=outcome)
    # extract(epoch ...) so the caller gets a plain float ts like the JSONL events
    select_cols = "extract(epoch from ts) AS ts, " + ", ".join(_CACHE_EVENT_COLS[1:])
    sql = f"SELECT {select_cols} FROM {FQ_CACHE_EVENT}{where} ORDER BY ts"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(zip(_CACHE_EVENT_COLS, r))
        # match the JSONL event shape so pd_cache aggregation is source-agnostic
        d["stage_tag"] = d.pop("stage")
        d["run_id"] = d.get("dag_run_id")
        out.append(d)
    return out


def count_cache_events(**filters: Any) -> int:
    """Return the number of ledger rows matching the filters (no filter = all)."""
    where, params, _ = _cache_event_where(**filters)
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {FQ_CACHE_EVENT}{where}", params)
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def clear_cache_events(*, all_rows: bool = False, **filters: Any) -> int:
    """Delete ledger rows. Returns the number deleted.

    Refuses to run with no filter unless ``all_rows=True`` (the reset-everything
    case), so a stray clear can't silently wipe the whole tapeout history.
    """
    where, params, n = _cache_event_where(**filters)
    if n == 0 and not all_rows:
        raise ValueError(
            "refusing to clear the whole ledger without a filter; pass a filter "
            "(--dag/--design/--stage/--before/--after/--user) or all_rows=True.")
    conn = _connect()
    conn.autocommit = True
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {FQ_CACHE_EVENT}{where}", params)
            return cur.rowcount
    finally:
        conn.close()


def set_cache_event_project(project: str, *, all_rows: bool = False,
                            **filters: Any) -> int:
    """Categorize ledger rows under ``project`` (UPDATE the project column).

    Returns the number of rows updated. This is how you bucket one or more
    dags/designs into a named project after the fact, e.g.
    ``set_cache_event_project("ee290_tapeout", dag="RocketConfig")``. Refuses to
    relabel the whole ledger without ``all_rows=True``.
    """
    if not project:
        raise ValueError("project must be non-empty")
    where, params, n = _cache_event_where(**filters)
    if n == 0 and not all_rows:
        raise ValueError(
            "refusing to relabel the whole ledger without a filter; pass a "
            "filter (--dag/--design/--stage/--after/--before/--user) or all_rows=True.")
    conn = _connect()
    conn.autocommit = True
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {FQ_CACHE_EVENT} SET project = %s{where}",
                        [project] + params)
            return cur.rowcount
    finally:
        conn.close()


def list_stage_blobs(
    stage_tag: Optional[str] = None,
    limit: int = 20,
) -> List[Tuple[Any, ...]]:
    """
    List recent blobs. Returns rows of:
      (sha256, stage, size_bytes, duration_seconds, cpu_seconds, owner,
       triggering_user, dag_id, design, workspace, created_at)
    """
    cols = ("sha256, stage, size_bytes, duration_seconds, cpu_seconds, owner, "
            "triggering_user, dag_id, design, workspace, created_at")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if stage_tag is None:
                cur.execute(
                    f"SELECT {cols} FROM {FQ_BLOB} ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            else:
                cur.execute(
                    f"SELECT {cols} FROM {FQ_BLOB} WHERE stage = %s "
                    f"ORDER BY created_at DESC LIMIT %s",
                    (stage_tag, limit),
                )
            return list(cur.fetchall())
    finally:
        conn.close()


def store_checkpoint(stage_key: str, stage: str, step: str, path: Path,
                     saved_seconds: Optional[float] = None,
                     saved_cpu_seconds: Optional[float] = None,
                     **provenance: Any) -> int:
    """Upsert one sub-step checkpoint (file gzipped, directory tarred).

    One row per (stage_key, step); a newer attempt replaces the old data.
    Returns the stored size in bytes.
    """
    path = Path(path)
    # connect first, so an unreachable database fails before the tar
    conn = _connect()
    if path.is_dir():
        data = tar_directory(path, arcname=path.name)
        is_dir = True
    else:
        data = gzip.compress(path.read_bytes())
        is_dir = False
    if len(data) > CHECKPOINT_MAX_BYTES:
        raise RuntimeError(
            f"checkpoint tarball is {len(data) / 1e6:.0f} MB compressed, past "
            f"the {CHECKPOINT_MAX_BYTES // (1024 * 1024)} MB sanity ceiling; "
            "keeping the local checkpoint only")
    # Same overflow scheme as stage blobs: one bytea can't hold rocket-scale
    # innovus checkpoints, so anything past a chunk goes to
    # pd_checkpoint_chunks and the parent row keeps empty data.
    chunks = [data[i:i + BLOB_CHUNK_BYTES]
              for i in range(0, len(data), BLOB_CHUNK_BYTES)]
    inline = data if len(chunks) <= 1 else b""
    cols = ("triggering_user", "dag_id", "dag_run_id", "workspace",
            "design", "module", "project")
    vals = [provenance.get(c) for c in cols]
    with conn:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {FQ_CHECKPOINT}
                    (stage_key, stage, step, data, size_bytes, is_dir, saved_seconds,
                     saved_cpu_seconds, {", ".join(cols)})
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, {", ".join(["%s"] * len(cols))})
                    ON CONFLICT (stage_key, step) DO UPDATE SET
                        data = EXCLUDED.data,
                        size_bytes = EXCLUDED.size_bytes,
                        is_dir = EXCLUDED.is_dir,
                        stage = EXCLUDED.stage,
                        saved_seconds = EXCLUDED.saved_seconds,
                        saved_cpu_seconds = EXCLUDED.saved_cpu_seconds,
                        created_at = NOW()""",
                [stage_key, stage, step, psycopg2.Binary(inline), len(data), is_dir,
                 saved_seconds, saved_cpu_seconds] + vals)
            # Drop chunks from any previous write unconditionally so an inline
            # rewrite can't leave stale overflow rows behind.
            cur.execute(
                f"DELETE FROM {FQ_CHECKPOINT_CHUNK} WHERE stage_key = %s AND step = %s",
                (stage_key, step))
            if len(chunks) > 1:
                for seq, chunk in enumerate(chunks):
                    cur.execute(
                        f"INSERT INTO {FQ_CHECKPOINT_CHUNK} (stage_key, step, seq, data) "
                        f"VALUES (%s, %s, %s, %s)",
                        (stage_key, step, seq, psycopg2.Binary(chunk)))
        conn.commit()
    return len(data)


def find_checkpoints(stage_key: Optional[str] = None, design: Optional[str] = None,
                     stage: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Checkpoint metadata rows (no blob data), newest first."""
    where, params = [], []  # type: List[str], List[Any]
    for col, val in (("stage_key", stage_key), ("design", design), ("stage", stage)):
        if val is not None:
            where.append(f"{col} = %s")
            params.append(val)
    sql = (f"SELECT id, stage_key, stage, step, size_bytes, is_dir, owner, "
           f"design, module, project, dag_id, created_at FROM {FQ_CHECKPOINT}")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]


def fetch_checkpoint(stage_key: Optional[str] = None, step: Optional[str] = None,
                     ckpt_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The newest checkpoint row (with data) for a stage key (optionally an
    exact step), or the exact row by ``ckpt_id``. None if absent."""
    if stage_key is None and ckpt_id is None:
        raise ValueError("pass stage_key or ckpt_id")
    where, params = [], []  # type: List[str], List[Any]
    if stage_key is not None:
        where.append("stage_key = %s")
        params.append(stage_key)
    if step is not None:
        where.append("step = %s")
        params.append(step)
    if ckpt_id is not None:
        where.append("id = %s")
        params.append(ckpt_id)
    sql = (f"SELECT id, stage_key, stage, step, data, size_bytes, is_dir, saved_seconds, "
           f"saved_cpu_seconds "
           f"FROM {FQ_CHECKPOINT} WHERE " + " AND ".join(where))
    sql += " ORDER BY created_at DESC LIMIT 1"
    with _connect() as conn:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            names = [d[0] for d in cur.description]
            rec = dict(zip(names, row))
            rec["data"] = bytes(rec["data"])
            # An empty data column on a non-empty checkpoint means the payload
            # overflowed into pd_checkpoint_chunks; reassemble by seq.
            if not rec["data"] and rec.get("size_bytes"):
                cur.execute(
                    f"SELECT data FROM {FQ_CHECKPOINT_CHUNK} "
                    f"WHERE stage_key = %s AND step = %s ORDER BY seq",
                    (rec["stage_key"], rec["step"]))
                rec["data"] = b"".join(bytes(r[0]) for r in cur.fetchall())
                if len(rec["data"]) != rec["size_bytes"]:
                    raise RuntimeError(
                        f"checkpoint {rec['stage_key'][:16]}.../{rec['step']} "
                        f"reassembled to {len(rec['data'])} bytes, expected "
                        f"{rec['size_bytes']}; refusing the truncated payload")
            return rec


_STEP_NAME = re.compile(r"[A-Za-z0-9_]+")


def materialize_checkpoint(rec: Dict[str, Any], rundir: Path) -> Path:
    """Write a fetched checkpoint back into a rundir as pre_<step>.

    The row comes from a table every group member can write, so the step must
    be a plain tool step name before it becomes a path.
    """
    step = rec["step"]
    if not isinstance(step, str) or not _STEP_NAME.fullmatch(step):
        raise ValueError(f"refusing checkpoint with step name {step!r}")
    rundir = Path(rundir)
    rundir.mkdir(parents=True, exist_ok=True)
    dest = rundir / f"pre_{step}"
    if rec["is_dir"]:
        untar_to_directory(rec["data"], rundir, as_name=dest.name)
    else:
        dest.write_bytes(gzip.decompress(rec["data"]))
    return dest


def delete_checkpoints(stage_key: Optional[str] = None, design: Optional[str] = None,
                       stage: Optional[str] = None, module: Optional[str] = None,
                       ids: Optional[List[int]] = None,
                       older_than_days: Optional[float] = None,
                       step: Optional[str] = None) -> int:
    """Delete checkpoint rows by key, design/stage/module, step, ids, or age.
    Returns row count."""
    where, params = [], []  # type: List[str], List[Any]
    if stage_key is not None:
        where.append("stage_key = %s")
        params.append(stage_key)
    if step is not None:
        where.append("step = %s")
        params.append(step)
    if design is not None:
        where.append("design = %s")
        params.append(design)
    if stage is not None:
        where.append("stage = %s")
        params.append(stage)
    if module is not None:
        where.append("module = %s")
        params.append(module)
    if ids:
        where.append("id = ANY(%s)")
        params.append(list(ids))
    if older_than_days is not None:
        where.append("created_at < NOW() - (%s || ' days')::interval")
        params.append(str(older_than_days))
    if not where:
        raise ValueError("refusing to delete all checkpoints; pass a filter")
    with _connect() as conn:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            # Reap overflow chunks with their parents in the same statement so
            # a filtered delete can't strand multi-GB chunk rows.
            cur.execute(
                f"DELETE FROM {FQ_CHECKPOINT} WHERE " + " AND ".join(where) +
                f" RETURNING stage_key, step", params)
            gone = cur.fetchall()
            n = len(gone)
            for sk, st in gone:
                cur.execute(
                    f"DELETE FROM {FQ_CHECKPOINT_CHUNK} WHERE stage_key = %s AND step = %s",
                    (sk, st))
        conn.commit()
    return n


# --- Design sharing (studio share / studio pull) -------------------------------
# Every query below runs as the caller's own role, and the row-level security in _DDL decides what it
# can see: its own bundles and shares, plus bundles of designs shared with it. A design nobody shared
# with the caller is indistinguishable from one that does not exist.

def current_db_user() -> str:
    """The Postgres role this process connects as, which owns what it publishes and shares."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            return str(cur.fetchone()[0])
    finally:
        conn.close()


def publish_design_bundle(design: str, manifest: Dict[str, Any], data: bytes, bundle_sha: str) -> bool:
    """Store the caller's latest bundle of ``design``. False when the stored bundle already has
    ``bundle_sha``, so an unchanged design is not uploaded again."""
    chunks = [data[i:i + BLOB_CHUNK_BYTES] for i in range(0, len(data), BLOB_CHUNK_BYTES)]
    inline = data if len(chunks) <= 1 else b""
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(f"SELECT bundle_sha FROM {FQ_DESIGN_BUNDLE} WHERE owner = current_user AND design = %s",
                        (design,))
            row = cur.fetchone()
            if row is not None and row[0] == bundle_sha:
                conn.commit()
                return False
            cur.execute(
                f"""
                INSERT INTO {FQ_DESIGN_BUNDLE} (design, bundle_sha, manifest, data, size_bytes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (owner, design) DO UPDATE
                  SET bundle_sha = EXCLUDED.bundle_sha,
                      manifest   = EXCLUDED.manifest,
                      data       = EXCLUDED.data,
                      size_bytes = EXCLUDED.size_bytes,
                      updated_at = NOW()
                """,
                (design, bundle_sha, Json(manifest), psycopg2.Binary(inline), len(data)))
            cur.execute(f"DELETE FROM {FQ_DESIGN_BUNDLE_CHUNK} WHERE owner = current_user AND design = %s",
                        (design,))
            if len(chunks) > 1:
                for seq, chunk in enumerate(chunks):
                    cur.execute(f"INSERT INTO {FQ_DESIGN_BUNDLE_CHUNK} (design, seq, data) VALUES (%s, %s, %s)",
                                (design, seq, psycopg2.Binary(chunk)))
        conn.commit()
        return True
    finally:
        conn.close()


def fetch_design_bundle(owner: str, design: str) -> Optional[Dict[str, Any]]:
    """``owner``'s latest bundle of ``design`` as {manifest, data, bundle_sha, updated_at}, or None when it
    does not exist or was not shared with the caller."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT manifest, data, bundle_sha, updated_at FROM {FQ_DESIGN_BUNDLE} "
                f"WHERE owner = %s AND design = %s", (owner, design))
            row = cur.fetchone()
            if row is None:
                return None
            data = bytes(row[1]) if row[1] is not None else b""
            if not data:
                cur.execute(
                    f"SELECT data FROM {FQ_DESIGN_BUNDLE_CHUNK} WHERE owner = %s AND design = %s ORDER BY seq",
                    (owner, design))
                data = b"".join(bytes(c[0]) for c in cur.fetchall())
    finally:
        conn.close()
    return {"manifest": row[0], "data": data, "bundle_sha": row[2],
            "updated_at": row[3].isoformat() if hasattr(row[3], "isoformat") else row[3]}


def share_design(design: str, grantee: str) -> None:
    """Let ``grantee`` pull the caller's bundles of ``design``."""
    _valid_role(grantee)
    conn = _connect()
    try:
        _ensure_schema(conn, quiet=True)
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {FQ_DESIGN_SHARE} (design, grantee) VALUES (%s, %s) "
                        f"ON CONFLICT (owner, design, grantee) DO NOTHING", (design, grantee))
        conn.commit()
    finally:
        conn.close()


def unshare_design(design: str, grantee: str) -> int:
    """Stop ``grantee`` pulling the caller's ``design``. Returns the number of shares removed."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {FQ_DESIGN_SHARE} WHERE owner = current_user AND design = %s "
                        f"AND grantee = %s", (design, grantee))
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def design_has_shares(design: str) -> bool:
    """True when the caller has shared ``design`` with anyone."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {FQ_DESIGN_SHARE} WHERE owner = current_user AND design = %s LIMIT 1",
                        (design,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def list_design_shares() -> List[Dict[str, Any]]:
    """Every share the caller can see (its own, and those naming it as grantee), with the bundle's age and
    size."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT s.owner, s.design, s.grantee, s.created_at, b.updated_at, b.size_bytes "
                f"FROM {FQ_DESIGN_SHARE} s LEFT JOIN {FQ_DESIGN_BUNDLE} b "
                f"ON b.owner = s.owner AND b.design = s.design ORDER BY s.owner, s.design, s.grantee")
            rows = cur.fetchall()
    finally:
        conn.close()
    keys = ("owner", "design", "grantee", "shared_at", "bundle_updated_at", "size_bytes")
    return [dict(zip(keys, row)) for row in rows]


def tar_directory(path: Path, arcname: Optional[str] = None) -> bytes:
    """gzip-compressed tar of ``path``. ``arcname`` controls the root entry."""
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"Not a directory: {path}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(path), arcname=arcname or path.name, recursive=True)
    return buf.getvalue()


def _discard(path: Path) -> None:
    """Remove a file, symlink, or directory. Missing paths are fine."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _inside(root: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def _hard_link_escapes(member: tarfile.TarInfo, root: str) -> bool:
    """A hard link's target is archive-relative; it must stay under ``root``."""
    return member.islnk() and (os.path.isabs(member.linkname)
                               or not _inside(root, os.path.join(root, member.linkname)))


def _tar_filter_and_hard_links(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
    filtered = tarfile.tar_filter(member, path)  # type: ignore[attr-defined]
    if _hard_link_escapes(member, os.path.realpath(path)):
        # A FilterError subclass: extractall's default errorlevel swallows a
        # plain ExtractError raised here and would just skip the member.
        raise tarfile.LinkOutsideDestinationError(  # type: ignore[attr-defined]
            member, os.path.join(path, member.linkname))
    return filtered


def _trust_checked_member(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo:
    return member


def _not_a_plain_path(root: str, name: str) -> bool:
    """True if ``name`` has a ``..`` part or an existing part of it under ``root`` is a symlink."""
    parts = name.split("/")
    if ".." in parts:
        return True
    path = root
    for part in parts:
        path = os.path.join(path, part)
        # os.lstat, not os.path.islink, so an error such as ENAMETOOLONG aborts the restore.
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _extract_like_tar_filter(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract ``tar`` into ``dest`` with the checks of tarfile's "tar" filter.

    Each member is checked just before it is written, so a name that resolves
    through a symlink extracted earlier in the same archive is caught too.
    """
    root = os.path.realpath(dest)
    if hasattr(type(tar), "extraction_filter"):
        tar.extraction_filter = _trust_checked_member
    directories = []
    for member in tar.getmembers():
        if os.path.isabs(member.name) or not _inside(root, os.path.join(root, member.name)):
            raise tarfile.ExtractError(f"refusing to extract {member.name!r} outside {dest}")
        if _hard_link_escapes(member, root):
            raise tarfile.ExtractError(f"refusing hard link {member.name!r} -> {member.linkname!r}")
        if _not_a_plain_path(root, member.name):
            raise tarfile.ExtractError(f"refusing {member.name!r}: it has a '..' part or goes through a symlink")
        if member.islnk() and _not_a_plain_path(root, member.linkname):
            raise tarfile.ExtractError(f"refusing hard link {member.name!r} -> {member.linkname!r}")
        if member.ischr() or member.isblk() or member.isfifo():
            raise tarfile.ExtractError(f"refusing special file {member.name!r}")
        member.mode &= 0o755
        tar.extract(member, path=root, set_attrs=not member.isdir())
        if member.isdir():
            directories.append(member)
    for member in sorted(directories, key=lambda m: m.name, reverse=True):
        dirpath = os.path.join(root, member.name)
        try:
            lst = os.lstat(dirpath)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(lst.st_mode):
            continue
        os.utime(dirpath, (member.mtime, member.mtime))
        os.chmod(dirpath, member.mode)


def untar_to_directory(data: bytes, dest: Path, as_name: Optional[str] = None) -> Optional[str]:
    """Extract a gzip tar into ``dest``. ``dest`` is created if it doesn't exist.

    With ``as_name``, the archive must hold exactly one top-level entry, and it
    lands at ``dest/as_name`` whatever it was called when tarred. Restores pass
    the rundir they were asked for, so a blob named ``syn-rundir`` cannot
    replace a sibling of ``my-syn``, and a checkpoint cannot write anything
    beside its own ``pre_<step>``. It then returns the name the entry was
    tarred under; without ``as_name`` it returns None.

    Blobs come from teammates via the shared cache, so member names must not
    be able to write outside ``dest``: the "tar" filter strips absolute paths
    and refuses ``..`` traversal, and _hard_link_escapes adds the one check it
    lacks: a hard link to a file outside ``dest`` (``/home/me/.bashrc`` or
    ``../../x``) would let a later in-place write to the rundir rewrite that
    file, and a re-cache upload it. The stricter "data" filter would also reject
    symlinks with absolute targets, which rundirs legitimately contain (the
    innovus ``post_*`` links point at absolute paths), so it would break par
    restores.

    Extraction goes to a staging directory and only moves into ``dest`` once
    the whole archive has been read, so a rejected or truncated blob leaves
    the previous contents untouched instead of a half-written rundir. Staging
    also keeps the filter from resolving through symlinks left by an earlier
    restore: those point into the workspace that produced them, so extracting
    the same blob twice on top of itself would otherwise be refused as an
    escape from ``dest``.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".untar-", dir=str(dest)))
    stored_name: Optional[str] = None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            if hasattr(tarfile, "tar_filter"):
                tar.extractall(path=str(staging), filter=_tar_filter_and_hard_links)
            else:
                # This tarfile has no extraction filters, so members are checked and extracted by hand.
                _extract_like_tar_filter(tar, staging)

        entries = sorted(staging.iterdir())
        if as_name is not None:
            if len(entries) != 1:
                raise ValueError(f"expected one top-level entry to restore as {as_name!r}, "
                                 f"found {sorted(e.name for e in entries)}")
            stored_name = entries[0].name
            if as_name != stored_name:
                renamed = staging / f".as-{os.getpid()}" / as_name
                renamed.parent.mkdir()
                os.replace(entries[0], renamed)
                entries = [renamed]
        for entry in entries:
            target = dest / entry.name
            displaced = dest / f"{entry.name}.replaced-{os.getpid()}"
            _discard(displaced)
            if os.path.lexists(target):
                os.replace(target, displaced)
            try:
                os.replace(entry, target)
            except OSError:
                if os.path.lexists(displaced):
                    os.replace(displaced, target)
                raise
            _discard(displaced)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return stored_name
