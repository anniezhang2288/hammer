"""vlsi.core.build_system: hammer, plain legacy Hammer with every SledgeHammer feature off.

The other build systems only choose what `buildfile` writes. "hammer" writes the same hammer.d
as "make" and also makes every hammer-vlsi action behave like upstream Hammer: make, or whoever
calls the action, decides that it runs, and it runs the tool. There is no dependency check or
master_database.json, PD cache, sub-step resume or checkpoint streaming, error scan, LVS report
verdict, time-saved ledger, RTL/collateral/code fingerprinting, or Postgres connection. It never
creates master_database.json; if an earlier run under another build_system left one, each stage this
mode reruns is marked needs-rerun in it (forget_stage_records), so switching back cannot skip to this
mode's outputs.

CLIDriver.run_main_parsed calls `apply` as soon as the config is loaded. It forces the SledgeHammer
switches below to their legacy values in os.environ, over anything the user exported, so children
and env-reading code see them too, and it overrides the config-level enables in the driver's
database. `restored_after` gives an in-process caller its own values back once the action returns.
cli_driver and the feature switches also check `is_legacy_hammer` directly, so the legacy path does
not depend on the environment.

The mode is read from the action's merged config. Stages after syn get it from the previous
stage's output (par-input.json, par-output-full.json, ...) plus whatever configs make passes after
it, so after switching vlsi.core.build_system, rerun from syn.
"""
import contextlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

BUILD_SYSTEM_KEY = "vlsi.core.build_system"
LEGACY_HAMMER = "hammer"

# Every SledgeHammer environment switch, at the value that turns its feature off.
LEGACY_ENV: Dict[str, str] = {
    "HAMMER_PD_CACHE": "0",
    "HAMMER_PD_CACHE_LEDGER": "0",
    "HAMMER_SUBSTEP_RESUME": "0",
    "HAMMER_DB_CHECKPOINTS": "0",
    "HAMMER_CHECKPOINT_STREAM": "0",
    "HAMMER_ERROR_SCAN": "0",
    "HAMMER_RTL_FP_CACHE": "off",
    "HAMMER_CODE_FP_CACHE": "off",
}

# The config-level enables, at their legacy values.
LEGACY_SETTINGS: Dict[str, Any] = {
    "vlsi.pd_cache.enabled": False,
    "vlsi.pd_cache.ledger_enabled": False,
    "vlsi.substep_resume.enabled": False,
    "vlsi.substep_resume.db_checkpoints": False,
    "vlsi.error_scan.enabled": False,
    "vlsi.error_scan.fail_on": [],
}

_OFF = ("", "0", "false", "no", "off")

# How many times `apply` has forced the switches in this process.
_applied = 0


def is_legacy_hammer_project(project_configs: Sequence[str]) -> bool:
    """True when these project config files, read and merged as HammerDriver does, set build_system to "hammer"."""
    import hammer.config as hammer_config
    configs = []
    for config in project_configs:
        if os.path.exists(config):
            configs.append(hammer_config.load_config_from_string(Path(config).read_text(), config.endswith(".yml"),
                                                                 str(Path(config).resolve().parent)))
    return hammer_config.combine_configs(configs).get(BUILD_SYSTEM_KEY) == LEGACY_HAMMER


def is_legacy_hammer(driver: Any) -> bool:
    """True when the driver's merged config sets vlsi.core.build_system to "hammer". Never raises."""
    try:
        value = driver.database.get_setting(BUILD_SYSTEM_KEY, nullvalue=None, check_type=False)
    except Exception:
        return False
    return isinstance(value, str) and value == LEGACY_HAMMER


def apply(driver: Any) -> bool:
    """Switch this process to legacy Hammer if the config asks for it. Returns whether it did."""
    global _applied
    if not is_legacy_hammer(driver):
        return False
    overridden: List[str] = []
    for name, value in LEGACY_ENV.items():
        old = os.environ.get(name)
        if old is not None and old.strip().lower() not in _OFF:
            overridden.append(f"{name}={old}")
        os.environ[name] = value
    _applied += 1
    for key, value in LEGACY_SETTINGS.items():
        driver.database.set_setting(key, value)
    forced = " ".join(f"{name}={value}" for name, value in LEGACY_ENV.items())
    settings = ", ".join(f"{key}={str(value).lower()}" for key, value in LEGACY_SETTINGS.items())
    driver.log.info(f"vlsi.core.build_system is hammer: running plain legacy Hammer. Forced {forced} "
                    f"and {settings}; no dependency check, PD cache, sub-step resume, checkpoint streaming, "
                    f"error scan, LVS report verdict, time-saved ledger, fingerprints or Postgres. Stages "
                    f"after syn read build_system from the stage before them, so rerun from syn after "
                    f"switching it.")
    if overridden:
        driver.log.warning(f"vlsi.core.build_system is hammer, so these exported SledgeHammer switches "
                           f"are ignored: {', '.join(overridden)}.")
    return True


def forget_stage_records(driver: Any, stage: str, run_dir: str) -> None:
    """Stop an earlier SledgeHammer run's records from vouching for a rundir this run is about to rewrite.

    hammer mode never creates master_database.json or a resume marker, but a build dir that ran under
    another build_system first still has them. Once build_system is switched back they would describe
    this run's rundir as the earlier one: the dependency check skips the stage and returns this run's
    outputs for the earlier inputs, and sub-step resume can start from this run's checkpoints. So mark
    the stage needs-rerun in an existing master database and remove the rundir's resume marker.
    Creates nothing and never raises.
    """
    from hammer.vlsi.substep_resume import MARKER_NAME
    master = Path(driver.obj_dir) / "master_database.json"
    try:
        if master.is_file():
            contents = json.loads(master.read_text())
            if isinstance(contents, dict) and contents.get(f"{stage}.needsToRerun") is not True:
                contents[f"{stage}.needsToRerun"] = True
                tmp = master.with_name(f"{master.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(contents, sort_keys=True, indent=4, separators=(',', ': ')))
                os.replace(tmp, master)
    except (OSError, ValueError) as e:
        driver.log.warning(f"Could not mark {stage} needs-rerun in {master} ({e}). Rerun {stage} with "
                           f"--force if you switch vlsi.core.build_system back from hammer.")
    try:
        os.unlink(os.path.join(run_dir, MARKER_NAME))
    except FileNotFoundError:
        pass
    except OSError as e:
        driver.log.warning(f"Could not remove the resume marker in {run_dir} ({e}).")


@contextlib.contextmanager
def restored_after() -> Iterator[None]:
    """Around one action: if `apply` forced the switches inside it, put back the values from before."""
    saved: Dict[str, Optional[str]] = {name: os.environ.get(name) for name in LEGACY_ENV}
    applied = _applied
    try:
        yield
    finally:
        if _applied != applied:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def airflow_run_id() -> str:
    """The Airflow DAG run this process is a task of, or "" outside Airflow."""
    return os.environ.get("HAMMER_AIRFLOW_RUN_ID") or os.environ.get("AIRFLOW_CTX_DAG_RUN_ID") or ""
