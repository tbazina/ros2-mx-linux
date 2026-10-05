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

Open a **fresh terminal** and run the printed sourcing command and
`export ROS_LOCALHOST_ONLY=1`. Certification covers local use; LAN communication
is unverified. Activation writes no shell configuration or current-installation
symlink. Switching workspace in an already sourced shell can leave old paths.

## Six stages

| Command | Purpose |
| --- | --- |
| `baseline --workspace PATH` | Read-only source/build/system snapshot of an existing installation |
| `bootstrap` | Install and verify Bookworm build tools and initialize rosdep if necessary |
| `prepare --source baseline\|upstream\|manifest` | Import into a fresh workspace and freeze exact source revisions |
| `deps --candidate PATH` | Refresh rosdep, show the dependency plan, and install Debian packages |
| `build --candidate PATH` | Check inputs/dependencies and build that candidate |
| `validate --candidate PATH` | Sequential package tests and local pub/sub, discovery, service, and action checks on both DDS implementations |
| `activate --candidate PATH` | Check current readiness and print manual sourcing/rollback commands |
| `run --source …` | All preparation/build/validation stages, without activation |
| `status [--candidate PATH]` | Inventory or stage/readiness report, certification scope, and warnings |
| `diagnose --candidate PATH [--check-network]` | Summarize saved failures and run bounded communication checks; never grant readiness |

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
and testing explicitly enforced. Fresh upstream/custom builds default to two package workers and three
compiler jobs per package. Baseline reproduction retains recorded concurrency unless
you explicitly override it. Validation runs one package and one CTest job at a time
to prevent test interference. Set concurrency **when preparing**:

```bash
./scripts/ros2_humble.sh run --source upstream --workers 2 --jobs 3
```

Compiler caching is optional and helps repeated builds after the cache is populated:

```bash
./scripts/bootstrap_ros2_humble.sh --ccache
./scripts/ros2_humble.sh run --source upstream --ccache
# Or prepare --ccache after bootstrap, then deps/build/validate separately.
```

Only an explicit `--ccache` request installs/enables ccache. It uses CMake compiler
launchers, a shared `<root>/cache/ccache` directory, content-based compiler checking,
and candidate-local configuration without unsafe cache settings. Launcher/compiler
identity and paths are recorded build inputs. Changing build settings requires a
fresh candidate; existing two-job candidates remain unchanged. Monitor available
memory, swapping, and thermal behavior when increasing concurrency. `metadata/build-timing.json`
records resource observations, package timings, and cache statistics; no particular
speedup is guaranteed. See [ccache documentation](https://ccache.dev/manual/4.8.html).

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

Validation certifies **localhost use only**. The default `local` mode runs selected
core ROS, graph, QoS, services, actions, logging, TF2, and rosbag2 tests. Required
runtime tests run under Fast DDS and Cyclone DDS where applicable. The fixed,
anchored allowlist is in `config/validation.json`; missing required tests block
validation instead of silently expanding or shrinking coverage.

```bash
./scripts/ros2_humble.sh validate --candidate "$CANDIDATE"
./scripts/ros2_humble.sh validate --candidate "$CANDIDATE" --validation-mode full
```

Full mode runs all discovered package tests, including native DDS tests. Neither
mode certifies LAN connectivity. Passing local mode does not resolve failures in
omitted upstream tests; previous full-suite reports remain in the candidate logs.
RViz/rqt and hardware behavior require optional manual checks.

| Setting | Local default | Full default |
|---|---|---|
| Lint/type-check policy | `skip` | `warn` |
| Package-test budget | 1,800 seconds | 7,200 seconds |
| Per-invocation limit | 300 seconds | 900 seconds |
| Package/CTest concurrency | One | One |

`--lint-policy skip` omits only exact recognized tests or configured lint labels;
a pytest subprocess plugin records exact deselected items. `warn` still runs lint
and converts identifiable reported failures to warnings; `strict` makes those
failures blocking. Runtime tests in lint packages, collection errors, crashes,
missing results, and timeouts remain blocking.

```bash
./scripts/ros2_humble.sh validate --candidate "$CANDIDATE" \
  --validation-mode full --lint-policy strict --test-timeout 7200 --package-timeout 900
```

The eight bounded DDS smoke checks run **before** expensive package tests: C++ →
Python and Python → C++ pub/sub, daemon-free discovery, correct service response,
and action result for both implementations. Each check has a 30-second limit.
ROS CLI, demos, both DDS implementations, RViz, rqt, and action tutorials must be
installed. Full-mode preflight also checks native Cyclone's hard-coded domain-0
ports; occupied ports and identifiable owners are reported without stopping them.

Package invocations use reserved domains **20–100**, private logs/cache directories,
and `ROS_LOCALHOST_ONLY=1`. Logging tests instead get a temporary `HOME` with
`ROS_HOME` and `ROS_LOG_DIR` unset, preserving their environment-manipulation tests.
Native Cyclone tests use the recorded loopback configuration, without changing
host interfaces or firewall rules. Only validator-owned processes and daemons
carrying the attempt token are cleaned up. Validation runs under a shared root lock.

Local mode stops after the first blocking package result; full mode continues
collecting failures. Readiness requires fresh reports, nonzero executed coverage,
correct stage outcomes, and no blocking results. Legitimate benchmark skips and
configured disabled tests are recorded separately from intentionally unselected
tests. The exact revision-bound default-interface `ros2multicast` assertion remains
a visible network-only warning; general communication failures are never waived.

Each `logs/validation-<id>/` retains the execution plan, preflight, policies,
exceptions, CTest inventory, invocation commands/environments, middleware-separated
report snapshots, pytest item evidence, `tests.json`, `smoke.json`, and timings.
Latest summaries also appear in `metadata/`, including on failure. Use `status`
for stage durations and the slowest build/test invocations. GUI checks are manual:

```bash
ros2 run rviz2 rviz2
ros2 run rqt_gui rqt_gui
```

See [colcon test options](https://colcon.readthedocs.io/en/main/reference/verb/test.html).

### Activate and roll back

Activation is allowed only after validation passes and the recorded source,
configuration, and installed-package fingerprints still match. Package checking
is conservative: **any installed Debian package version change** requests a rebuild
and revalidation. Validation code, policy, exceptions, or network configuration
changes require revalidation without a ROS rebuild. Activation reports the validation mode, local
certification scope, coverage, omitted-test entries, and warnings. There is no force-activation option.

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
patches as versioned inputs. The default profile patches Humble launch-testing
retry handling for Python 3.11 while preserving earlier test failures. It is based
on the [upstream guarded implementation](https://github.com/ros2/launch/blob/rolling/launch_testing/launch_testing/markers.py).
Patches apply only during fresh candidate preparation; an already matching
postimage is recorded, while unknown drift stops preparation.

`config/test-exceptions.json` starts empty. A reviewed exception needs `package`,
`classname`, `name`, the exact repository `revision` (40-character commit SHA),
and `reason`. Use the identifiers in `metadata/tests.json`; CTest-only test names
use classname `CTest`. Choose the file with `--exceptions PATH` during preparation
or validation. Validation stores a per-attempt snapshot; changing exceptions
invalidates readiness without changing build inputs.
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
new source, build profile, or patch inputs require a fresh candidate. Validation
policy/exception/code changes require only another validation run.
If system packages changed after build, rebuild before validating again.

Inspect saved failures and run quick checks without rebuilding or running the full
suite:

```bash
./scripts/ros2_humble.sh diagnose --candidate "$CANDIDATE"
./scripts/ros2_humble.sh diagnose --candidate "$CANDIDATE" --check-network
```

`--check-network` also probes default-interface communication; it is not proof of
communication between two machines. Diagnostics save results in `logs/diagnose-<id>/`
and never grant or replace readiness. Target selected failures without rerunning
the complete suite:

```bash
./scripts/ros2_humble.sh diagnose --candidate "$CANDIDATE" \
  --packages demo_nodes_cpp test_rclcpp test_communication \
  --ctest-regex '^test_(tutorial_talker_listener|node_name|services_cpp|publisher_subscriber__rclcpp__rclpy|requester_replier__rclcpp__rclpy|action_client_server__rclcpp__rclpy)__rmw_cyclonedds_cpp$' \
  --compare-cyclone --package-timeout 300

./scripts/ros2_humble.sh diagnose --candidate "$CANDIDATE" \
  --packages ros2topic --pytest-expression 'test_cli and rmw_fastrtps_dynamic_cpp' \
  --rmw rmw_fastrtps_dynamic_cpp --package-timeout 300

./scripts/ros2_humble.sh diagnose --candidate "$CANDIDATE" \
  --packages rcl_logging_spdlog --ctest-regex '^test_logging_interface$'
```

`--compare-cyclone` compares the usual localhost environment with explicit loopback
multicast. The second diagnostic variant disables RMW's automatic localhost interface
injection to avoid duplicate interface definitions, but requires loopback-only XML
with no remote peers. It does **not** change certification settings. No new general
Cyclone workaround is enabled without a successful live reproduction.

Known old metadata is migrated only after verifying its complete original
fingerprint; old validation becomes stale. Unknown or changed provenance requires
a fresh candidate, without an adoption bypass.

Validator-only fixes allow reuse of an unchanged built candidate:

```bash
./scripts/ros2_humble.sh validate --candidate "$CANDIDATE" --lint-policy warn
./scripts/ros2_humble.sh activate --candidate "$CANDIDATE"
```

To include the Python 3.11 source patch, create a **new** candidate from the existing
candidate's pinned manifest. This retains the selected revisions and applies the
current compatibility profile. These commands build and test ROS when you run them:

```bash
ROOT="$HOME/ros2_humble_updated"
OLD="$ROOT/candidates/REPLACE_WITH_EXISTING_CANDIDATE_ID"
./scripts/ros2_humble.sh prepare --root "$ROOT" --source manifest \
  --manifest "$OLD/metadata/exact.repos"
NEW="$ROOT/candidates/REPLACE_WITH_NEW_CANDIDATE_ID"
./scripts/ros2_humble.sh deps --root "$ROOT" --candidate "$NEW"
./scripts/ros2_humble.sh build --root "$ROOT" --candidate "$NEW"
./scripts/ros2_humble.sh validate --root "$ROOT" --candidate "$NEW" --lint-policy warn
./scripts/ros2_humble.sh activate --root "$ROOT" --candidate "$NEW"
```

Always supply the same `--root` when using a nondefault runtime directory.
The previous candidate and `~/ros2_humble` remain at their existing paths.

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
a ROS runtime dependency. Python tests require Debian's `python3-yaml`, `python3-pytest`, colcon, and CMake.
They also build tiny non-ROS CMake fixtures in temporary directories to exercise
actual colcon commands and CTest reporting. No apt commands or ROS builds run.
