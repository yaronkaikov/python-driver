# Pre-release CI

Add the `pre-release-ci` label to a PR to run full wheel builds/tests, the driver
integration matrix, and Scylla's Python/CQL tests in GitHub Actions. Adding the
label starts the workflow; pushes and reopening the labeled PR run it again.
Removing the label cancels its run. Unrelated label changes do not restart it.
Manual runs are also available. This workflow does not publish packages.

## Stable release selection

Preparation resolves the highest numeric final `scylla-X.Y.Z` tag once, excluding
RC and candidate tags, and pins its commit for the entire run. Every test job
checks out that commit and uses its own `tools/toolchain/image` through the
upstream `tools/toolchain/dbuild` launcher.

CCM downloads and installs `release:X.Y.Z` inside the job. Both the package version
and its embedded source hash must match the resolved tag. Tests run in `release`
mode against this binary, preserving upstream's mode exclusions. The candidate
wheel replaces the toolchain's driver in a venv; its version, file URL, imported
module paths, and SHA256 are recorded. No PyPI driver is used as a fallback.
The driver integration matrix uses this same CCM release selector. Scylla test
jobs wait only for Linux wheels; other platform builds run independently.

## Test jobs

Preparation discovers Python/CQL files from every eligible suite in the release
checkout. It splits whole modules to preserve their fixtures:

- `cluster`: eight shards.
- `cqlpy`: four shards, with Wasm fixtures built in the release toolchain.
- `alternator` and `rest_api`: two shards each.
- Other suites: one job each, including CQL approval tests, nodetool, debugger
  tests, and distribution Python tests when present.

For `scylla-2026.3.4`, this produces 21 jobs covering 516 files. Future releases
are discovered again rather than using a fixed file list. The `scylla-test-plan`
artifact records the chosen release and each job's complete file list.

Jobs use Linux x86_64 runners, with up to four test jobs running at once and one
test worker per job to leave memory for multi-node tests. Each shard has a
150-minute test session limit and a 180-minute job limit. Tests marked
`non_gating` are excluded, matching upstream gating. GDB jobs additionally fetch
the matching unstripped release package through CCM for debugger symbols.

Scope is the Python/CQL trial with published release binaries. C++ suites need
compiled test executables, which CCM's release packages do not provide.
Harness/fixture directories and upstream's manual/performance test directories
are excluded. This does not reproduce Jenkins' dev/debug or ARM build matrices.

## Results and local runs

Each `scylla-testpy-<suite>-<shard>-logs` artifact contains a tarball with candidate
identity, CCM package identity, selected test paths, console output, JUnit/Allure
reports, and test/server logs. The tarball preserves pytest parameter names that
artifact upload cannot accept as individual filenames. Server data directories
are not uploaded.

Prepare the same inputs locally:

```sh
python3 scripts/scylla_release.py resolve --output release.json
# Check out scylladb/scylladb at release.json's revision, including the
# seastar and tools/cqlsh helper submodules, into /path/to/scylla.
python3 scripts/scylla_release.py plan /path/to/scylla --output jobs.json
```

Run `scripts/run_scylla_testpy.sh` through that checkout's
`tools/toolchain/dbuild`, using the same mounts as the workflow, and set:

- `SCYLLA_SOURCE`: absolute path to that checkout.
- `SCYLLA_RELEASE_FILE` / `SCYLLA_PLAN_FILE`: absolute JSON file paths.
- `SCYLLA_TEST_JOB`: one job name from the plan, such as `cluster-1`.
- `DRIVER_WHEEL_DIR`: absolute directory containing exactly one compatible
  candidate wheel for the toolchain's Python ABI.
- `TESTPY_LOG_DIR`: absolute output directory mounted into the container.

The existing `build-pre-release.yml` checks prerelease Python versions and serves
a different purpose. Regular PR checks continue to run independently.
