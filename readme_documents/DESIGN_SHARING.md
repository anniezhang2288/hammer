# Running a partner's design

Your partner shares a design through the database. You pull it into a directory of your own and run it
on your own DAG. Since your partner already built it, syn and par restore their results from the PD
cache in the time it takes to download them, instead of running Genus and Innovus again.

```bash
# your partner, once (their syn runs keep it up to date after that)
studio share ChipTop bob

# you
studio pull alice ChipTop          # files land in ./ChipTop-from-alice, plus your own DAG
cd ChipTop-from-alice
sledgehammer run syn par           # cache hits: alice's latest syn and par, restored
```

Run `studio pull alice ChipTop` again whenever you want your partner's latest. It updates the directory
in place and removes files they no longer use.

## Who can pull what

`studio share <design> <partner>` lets one Postgres role pull one design. The database enforces it
with row-level security on the bundle and share tables. A teammate in `sledgehammer_users` still can't
read a design nobody shared with them; to them it looks like it doesn't exist. Only the owner can change
or remove their shares:

```bash
studio shares                      # what you shared, and what others shared with you
studio unshare ChipTop bob
```

A database admin runs `studio init` once after upgrading, to create the tables and policies.

## What gets copied

`studio share` reads the DAG `make buildfile` registered for the design and copies:

- the env and project configs the DAG runs with
- your driver script (e.g. `example-vlsi`), and any `.py` files beside it
- every file and directory those configs name under your working directory (where you run `make`),
  such as the RTL, include dirs, SDCs and macro collateral
- generated inputs at the top level of the build dir, such as `sram_generator-output.json`

All of it is gzipped into one bundle. Stage outputs are not copied: the cache restores those.

Paths outside your working directory are not copied: the PDK, tool installs and site-wide configs. Your
partner needs them at the same absolute path, which is true on BWRC machines. `studio pull` lists any
that are missing.

## When you get cache hits

A stage hits your partner's cache when everything its result depends on matches what they built. In
practice that means you need:

- **The same Hammer commit.** Every cache key includes Hammer's own code. `studio pull` warns when
  yours differs and names your partner's commit.
- **The same tech plugin version**, installed the same way.
- **The same env config.** `studio pull` uses your partner's by default. `--env <file>` swaps in yours,
  but any setting that differs changes the keys.

Only syn and par are cached. DRC, LVS, sim, power, formal and timing run on your machine as usual.

## Things to know

- **Your partner's driver script runs on your machine.** `studio pull` runs it to generate your DAG, and
  every stage runs it again. Only pull designs from people you trust.
- **Your pulled DAG is called `sledgehammer_<design>_from_<owner>_<you>`**, so it never replaces your
  own DAG of a design with the same name. Run `sledgehammer` from the pulled directory and it finds the
  pulled DAG on its own.
- **The pulled DAG uses your partner's default Tools choice only.**
- **Upgrading rebuilds every cache entry once.** Files under the working directory are now keyed by
  where they sit inside it, not by absolute path, so that a pulled copy matches. Stages already built
  rerun once after the upgrade, and cache entries stored before it are not reused.
