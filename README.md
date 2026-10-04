# ROS 2 Humble on MX Linux / Debian Bookworm

Local scripts to capture an existing ROS installation, build a separate candidate,
validate it, and print manual activation instructions. The pipeline preserves
`~/ros2_humble`; it never updates that workspace in place.

**Target:** MX Linux 23.x / Debian 12 Bookworm, amd64, system Python 3.11.
Humble officially targets Ubuntu 22.04 and Debian 11; Bookworm needs maintained
compatibility adjustments. Humble support ends in **May 2027**. See
[ROS target platforms](https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-2000.rst)
and the [official source-build guide](https://docs.ros.org/en/humble/Installation/Alternatives/Ubuntu-Development-Setup.html).
The kernel is recorded, not constrained. No systemd service is needed.

## Quick start

Run from this repository as your normal user. Scripts create clean subprocess
environments; do not source the scripts themselves. `sudo` is used only for apt
and first-time rosdep initialization. Run package-installing stages from an
interactive terminal; sudo requests your password there before logging commands.

Capture the working installation **before** installing or updating dependencies:

```bash
./scripts/ros2_humble.sh baseline --workspace "$HOME/ros2_humble"
```

Reproduce its captured source revisions and available build settings in a new
candidate:

```bash
./scripts/ros2_humble.sh run --source baseline
```

Or explicitly select an upstream update:

```bash
./scripts/ros2_humble.sh run --source upstream
```

On a fresh Bookworm machine with no ROS installation, use the upstream command
directly. The entry point needs Bash and `/usr/bin/python3`; bootstrap installs
the remaining tools, including Python YAML. Baseline capture needs Git and
Python YAML already installed.

`run` performs bootstrap → prepare → dependencies → build → validate. It prints
the candidate path and retains failed candidates. It does **not** activate ROS.
Inspect candidates with:

```bash
./scripts/ros2_humble.sh status
```

Copy the printed candidate path into `CANDIDATE`, then request activation
instructions:

```bash
CANDIDATE="$HOME/ros2_humble_candidates/candidates/REPLACE_WITH_CANDIDATE_ID"
./scripts/ros2_humble.sh activate --candidate "$CANDIDATE"
```

Open a **fresh terminal** and run the printed `source …/install/local_setup.bash`
command. Activation writes no shell configuration or current-installation
symlink. Switching workspace in an already sourced shell can leave old paths.

## Six stages

| Command | Purpose |
| --- | --- |
| `baseline --workspace PATH` | Read-only source/build/system snapshot of an existing installation |
| `bootstrap` | Install and verify Bookworm build tools and initialize rosdep if necessary |
| `prepare --source baseline\|upstream\|manifest` | Import into a fresh workspace and freeze exact source revisions |
| `deps --candidate PATH` | Refresh rosdep, show the dependency plan, and install Debian packages |
| `build --candidate PATH` | Check inputs/dependencies and build that candidate |
| `validate --candidate PATH` | Full package tests plus communication checks on both DDS implementations |
| `activate --candidate PATH` | Check current readiness and print manual sourcing/rollback commands |
| `run --source …` | All preparation/build/validation stages, without activation |
| `status [--candidate PATH]` | Inventory or detailed stage/readiness report |

### Capture the baseline

Snapshots preserve the original manifest, a commit-pinned `exact.repos`, checksums,
origins/branches/revisions, tracked diffs and untracked-file inventories, Debian
package versions, tool versions, rosdep evidence, available build-log arguments,
CMake settings, installed package names, and `COLCON_IGNORE` paths.

Capture does not source, test, fetch, or modify the original installation.
Generated Python bytecode is recorded separately. Substantive edits produce a
retained diagnostic snapshot but block reproduction; preserve/review those
changes yourself. Different per-package CMake build types are preserved in
candidate-local colcon metadata (vendor defaults can legitimately differ).
Unsupported build selections, merged installation layouts, or missing settings
still require an explicit compatibility adjustment; the error names the cause.

A baseline is **observed and user-reported working**, not independently validated.
The pipeline always enables tests when rebuilding it. Source revisions alone
cannot freeze apt packages, vendor downloads, or the operating system.

`manifests/humble-bookworm-known-good.repos` remains the historical manifest used
for the existing installation. Its branches move; use captured `exact.repos` for
reproduction. No scripts overwrite this checked-in historical file.

### Bootstrap and dependency installation

```bash
./scripts/bootstrap_ros2_humble.sh
```

Bootstrap uses Debian packages for compilers, Python, EmPy 3.3.x, Git, vcstool,
rosdep, colcon extensions, and test tools. It verifies the required colcon verbs.
There is no global pip installation, Ubuntu apt repository, distribution upgrade,
or automatic package removal.

Dependency installation refreshes Humble rosdep rules with
`ROS_OS_OVERRIDE=debian:bookworm` and explicit `--os=debian:bookworm`. It displays
rosdep's apt plan and apt's simulation, installs with `--no-remove`, then runs
`rosdep check`. Non-apt installers and alternative-package selections require a
reviewed local mapping; they stop the pipeline instead of being chosen implicitly.
Each candidate has its own rosdep cache; build checks reject changed cache contents.

**Apt changes affect the host, including your original installation.** Before/after
package snapshots and logs help diagnose them; workspace rollback cannot undo
package upgrades. For dependency isolation, trial the commands on a disposable
Bookworm machine first. Native build success is still required on the target.

### Select sources and build separately

Use the latest reproducible snapshot, or select an explicit one:

```bash
./scripts/ros2_humble.sh prepare --source baseline \
  --baseline "$HOME/ros2_humble_candidates/baselines/REPLACE_WITH_BASELINE_ID"
```

Use a local manifest, including the historical branch-based input:

```bash
./scripts/ros2_humble.sh prepare --source manifest \
  --manifest "$PWD/manifests/humble-bookworm-known-good.repos"
```

`prepare` needs bootstrap tools already installed. Upstream mode downloads Humble's
manifest; additions/removals, URL changes, and revision changes are reported against
the latest baseline when available. Every candidate freezes actual commits after
import. No later stage pulls sources.

```bash
./scripts/ros2_humble.sh deps --candidate "$CANDIDATE"
./scripts/ros2_humble.sh build --candidate "$CANDIDATE"
./scripts/ros2_humble.sh validate --candidate "$CANDIDATE"
```

New upstream/custom candidates default to Release, tests enabled, system Python,
isolated package installations, and `--symlink-install`. Baseline candidates reuse
evidenced per-package build types/CMake arguments and package exclusions, with system Python
and testing explicitly enforced. The default is two package workers and two
compiler jobs per package. Set concurrency **when preparing**:

```bash
./scripts/ros2_humble.sh run --source upstream --workers 2 --jobs 2
```

Candidates keep independent sources, build/install trees, logs, and metadata:

```text
~/ros2_humble_candidates/
├── baselines/<id>/       original.repos, exact.repos, baseline.json
├── candidates/<id>/      src/, build/, install/, logs/, metadata/
└── logs/                 bootstrap logs and package snapshots
```

Use `--root "$PWD/runtime"` on each command to store runtime data elsewhere.
Root and candidate paths cannot overlap protected source workspaces or existing
candidate roots. Keep each candidate at its original path and retain its sources
and build tree: a symlink installation is not a standalone relocatable artifact.

### Validate

Validation requires successful colcon test tasks, fresh CTest/JUnit reports,
nonzero executed tests, and `colcon test-result --verbose`. It rejects missing
reports, aborted jobs, infrastructure errors, and unapproved failures. Detailed
ament reports written below `build/` are copied into the fresh result collection.
See [colcon test options](https://colcon.readthedocs.io/en/released/reference/verb/test.html).

It also requires ROS CLI, demos, both RMW implementations, RViz, and rqt packages.
Communication checks exercise C++ → Python and Python → C++ with both
`rmw_fastrtps_cpp` and `rmw_cyclonedds_cpp`, using isolated topics/domains,
localhost discovery, no ROS daemon, and a 30-second limit per check.
Only processes started by the validator are terminated.

The full test stage defaults to two hours:

```bash
./scripts/ros2_humble.sh validate --candidate "$CANDIDATE" --test-timeout 7200
```

Logs, test summaries, raw results, and exceptions used are retained. After sourcing
an activated candidate, optionally check `ros2 run rviz2 rviz2`, `ros2 run rqt_gui rqt_gui`,
and your hardware/application workflows on the MX desktop. These manual checks
are not automatic activation gates.

### Activate and roll back

Activation is allowed only after validation passes and the recorded source,
configuration, and installed-package fingerprints still match. Package checking
is conservative: **any installed Debian package version change** requests a rebuild
and revalidation. There is no force-activation option.

Roll back workspace selection in a fresh terminal:

```bash
source "$HOME/ros2_humble/install/local_setup.bash"
```

Keep previous candidates and the original installation. Switching ROS setup paths
does not restore prior apt packages. There is no automatic pruning.

## Bookworm compatibility configuration

`config/bookworm.json` is copied into each candidate. Make changes before preparing
a new one, or select a separate profile with `--profile PATH`.

- `skip_keys` maps rosdep keys to justifications. Defaults cover the official
  Humble source-build skips and Debian's verified `vcstool` naming adjustment.
  An explicitly empty `{}` means no skips; old Python/OpenCV/PCL version skips
  are not retained.
- `packages_ignore` lists deliberate package exclusions for build and tests.
- `local_rosdep_mappings` lists YAML files relative to the profile directory.
  Candidate-local rosdep source lists take precedence over system rules.
- `patches` lists objects with `repository` (manifest key), `file` (profile-relative
  patch), and `reason`. Patches are copied, checksummed, checked, and applied only
  to candidate sources.

Do not add skip keys merely to hide resolution failures. Supply justified Debian
mappings or exclude a genuinely optional package instead. Treat compatibility
patches as versioned inputs.

`config/test-exceptions.json` starts empty. A reviewed exception needs `package`,
`classname`, `name`, the exact repository `revision` (40-character commit SHA),
and `reason`. Use the identifiers in `metadata/tests.json`; CTest-only test names
use classname `CTest`. Choose the file with `--exceptions PATH` during preparation.
Exceptions apply only to exact reported failures. Missing results, timeouts,
infrastructure errors, and communication checks cannot be waived. Changed source
revisions need new review; never use a package-wide waiver.

## Failures and retries

```bash
./scripts/ros2_humble.sh status --candidate "$CANDIDATE"
```

Inspect the stage log named in the report and `logs/`. Interrupted/failed stages
never establish readiness. Retry `deps`, `build`, or `validate` explicitly after
resolving the cause. Build retries require unchanged source/configuration inputs;
new source, profile, patch, or exception inputs require a fresh candidate.
If system packages changed after build, rebuild before validating again.

A failed dependency install still records the after-package snapshot. A failed
prepare retains its diagnostic workspace; start a new prepare rather than importing
into that partial source tree. Root/candidate locks prevent simultaneous operations.

The old updater delegates to `run`:

```bash
./scripts/update_ros2_humble.sh --source baseline
```

`WS` and `SKIP_KEYS` environment variables are retired and rejected with migration
instructions. Scripts discard ROS, Conda, venv, user Python, and compiler environment
settings. Use CLI arguments and compatibility profiles for recorded changes.

## Local development checks

No CI or scheduled jobs are configured. Offline tests create temporary Git
repositories and mock apt/rosdep/build commands; they never build ROS or touch the
working installation.

```bash
bash -n scripts/ros2_humble.sh scripts/bootstrap_ros2_humble.sh scripts/update_ros2_humble.sh
shellcheck scripts/*.sh
/usr/bin/python3 -B -m unittest discover -s tests -v
./scripts/ros2_humble.sh --help
```

Install ShellCheck separately if it is unavailable; it is a development tool, not
a ROS runtime dependency. Python tests require Debian's `python3-yaml`.
