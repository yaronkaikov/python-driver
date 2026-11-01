# Pre-release CI

Add the `pre-release-ci` label to a pull request to run the full wheel build and
test matrix, the driver integration matrix, and Scylla's `test/cqlpy` suite with
the candidate Linux CPython 3.14 wheel. Adding the label starts the workflow;
subsequent pushes and reopening the labeled PR run it again. Removing the label
cancels its current run. Unrelated label changes do not restart or cancel it.
The workflow can also be started manually from Actions.

This workflow validates a candidate without publishing packages. It complements
the regular PR checks, which still run independently. The existing
`build-pre-release.yml` workflow checks prerelease **Python** versions and serves
a different purpose.

The Scylla job checks out a pinned source revision and downloads the matching
development server build. Update `SCYLLA_REVISION` and `SCYLLA_EXE_URL` together
in `pre-release-ci.yml` when refreshing the server snapshot. The job builds the
Wasm fixtures required by CQL tests, installs Scylla's locked Python dependencies,
and replaces its pinned driver with the candidate wheel. It checks the wheel's
version, installation URL, and module paths before running `test.py` with
`DISABLE_VENV=1` so startup cannot restore the upstream driver.

The CQL suite runs with two workers and excludes tests marked `non_gating`,
matching Scylla's gating convention. This does not run Scylla's C++ suites or
the full multi-node `test/cluster` suite. The `scylla-testpy-logs` artifact contains
the candidate wheel identity, console output, JUnit reports, and server logs.

To run a targeted check locally after building a compatible wheel:

```sh
bash scripts/build_scylla_wasm.sh /path/to/matching/scylla
SCYLLA_SOURCE=/path/to/matching/scylla \
SCYLLA_EXE_URL=https://example.com/matching-scylla-dev.x86_64.tar.gz \
DRIVER_WHEEL_DIR=/path/to/candidate-wheels \
TESTPY_LOG_DIR=/tmp/scylla-testpy-logs \
    bash scripts/run_scylla_testpy.sh test/cqlpy/test_null.py
```

Install `uv`, Python 3.14, libev, libnuma, Clang/LLD, WABT, Binaryen, OpenLDAP,
SASL tools, Docker, Toxiproxy 2.12.0, and Rust 1.88.0 with the `wasm32-wasip1`
target first. On Ubuntu, adapt the LDAP fixture's schema path, load `back_mdb`,
and use a private copy of `slapd` for temporary test data, as shown in the workflow.
Omit the final test path to run the same CQL suite as CI. Keep only one compatible
candidate wheel in the directory; missing or ambiguous wheels fail instead of
falling back to a published driver.
