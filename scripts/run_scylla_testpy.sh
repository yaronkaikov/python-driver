#!/usr/bin/env bash
# Called inside the frozen toolchain from the selected release tag.
set -euo pipefail

: "${SCYLLA_SOURCE:?Set SCYLLA_SOURCE to the matching Scylla checkout}"
: "${DRIVER_WHEEL_DIR:?Set DRIVER_WHEEL_DIR to the candidate wheel directory}"
: "${TESTPY_LOG_DIR:?Set TESTPY_LOG_DIR to the output directory}"
: "${SCYLLA_RELEASE_FILE:?Set SCYLLA_RELEASE_FILE to the resolved release JSON}"
: "${SCYLLA_PLAN_FILE:?Set SCYLLA_PLAN_FILE to the suite/shard plan JSON}"
: "${SCYLLA_TEST_JOB:?Set SCYLLA_TEST_JOB to a name from the plan}"

driver_scripts=$(realpath "$(dirname "$0")")
SCYLLA_SOURCE=$(realpath "$SCYLLA_SOURCE")
DRIVER_WHEEL_DIR=$(realpath "$DRIVER_WHEEL_DIR")
mkdir -p "$TESTPY_LOG_DIR/pytest-tmp"
TESTPY_LOG_DIR=$(realpath "$TESTPY_LOG_DIR")
export PYTEST_DEBUG_TEMPROOT="$TESTPY_LOG_DIR/pytest-tmp"
export CCM_CONFIG_DIR="$SCYLLA_SOURCE/build/ccm"
unset PYTHONPATH PYTHONHOME
cd "$SCYLLA_SOURCE"

# Retain the release toolchain's test dependencies while overriding its driver.
python3 -m venv --system-site-packages build/driver-candidate-venv
test_python="$SCYLLA_SOURCE/build/driver-candidate-venv/bin/python"

candidate_wheel=$("$test_python" - "$DRIVER_WHEEL_DIR" <<'PY'
from pathlib import Path
import sys
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

supported = set(sys_tags())
candidates = []
for wheel in Path(sys.argv[1]).glob('*.whl'):
    name, version, build, tags = parse_wheel_filename(wheel.name)
    if canonicalize_name(name) == 'scylla-driver' and supported.intersection(tags):
        candidates.append(wheel)
if len(candidates) != 1:
    raise SystemExit(f'Expected one compatible candidate wheel, found {candidates}')
print(candidates[0])
PY
)
# Backports can add Python dependencies after the frozen image was published.
# Use the release's own dependency list, excluding the driver being replaced.
read -r -a upstream_packages <<< "$(bash install-dependencies.sh --print-pip-runtime-packages)"
test_packages=()
for package in "${upstream_packages[@]}"; do
    if [[ "$package" != scylla-driver ]]; then
        test_packages+=("$package")
    fi
done
"$test_python" -m pip install \
    'ccm @ git+https://github.com/scylladb/scylla-ccm.git@a9f43b03089fc8931bb9b542ae7c64273e8c64a9' \
    "${test_packages[@]}"
"$test_python" -m pip install --force-reinstall "$candidate_wheel"

"$test_python" - "$candidate_wheel" <<'PY' | tee "$TESTPY_LOG_DIR/driver.txt"
from importlib.metadata import distribution
from pathlib import Path
import hashlib
import json
import sys
import cassandra
import cassandra.cluster
import cassandra.protocol
from packaging.utils import parse_wheel_filename

wheel = Path(sys.argv[1])
dist = distribution('scylla-driver')
assert dist.version == str(parse_wheel_filename(wheel.name)[1]), dist.version
direct_url = json.loads(dist.read_text('direct_url.json'))
assert direct_url['url'] == wheel.as_uri(), direct_url
for module in (cassandra, cassandra.cluster, cassandra.protocol):
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(Path(sys.prefix).resolve()), path
    print(f'{module.__name__}: {path}')
print(f'Driver version: {dist.version}')
print(f'Candidate wheel: {wheel}')
with wheel.open('rb') as stream:
    print(f'SHA256: {hashlib.file_digest(stream, "sha256").hexdigest()}')
PY

suite=$("$test_python" - "$SCYLLA_PLAN_FILE" "$SCYLLA_TEST_JOB" <<'PY'
import json
import sys
jobs = json.load(open(sys.argv[1]))
print(next(job['suite'] for job in jobs if job['name'] == sys.argv[2]))
PY
)
fetch_args=(--release "$SCYLLA_RELEASE_FILE" --source "$SCYLLA_SOURCE")
if [[ "$suite" == scylla_gdb ]]; then
    fetch_args+=(--symbols)
fi
"$test_python" -u "$driver_scripts/scylla_release.py" fetch "${fetch_args[@]}" \
    2>&1 | tee "$TESTPY_LOG_DIR/ccm.log"

if [[ "$suite" == cqlpy ]]; then
    bash "$driver_scripts/build_scylla_wasm.sh" "$SCYLLA_SOURCE"
fi

mapfile -t test_paths < <("$test_python" - "$SCYLLA_PLAN_FILE" "$SCYLLA_TEST_JOB" <<'PY'
import json
import sys
jobs = json.load(open(sys.argv[1]))
print('\n'.join(next(job['files'] for job in jobs if job['name'] == sys.argv[2])))
PY
)
if [[ ${#test_paths[@]} == 0 ]]; then
    echo 'No test files assigned to this job' >&2
    exit 1
fi
printf '%s\n' "${test_paths[@]}" > "$TESTPY_LOG_DIR/test-files.txt"
# Explicit release mode preserves upstream run_in/skip_in and skip_mode rules.
# Invoke this interpreter directly so newer harnesses cannot restore their driver.
export DISABLE_VENV=1
"$test_python" test.py --mode release --no-gather-metrics --jobs 1 \
    --timeout 1800 --session-timeout 9000 --markers 'not non_gating' \
    --tmpdir "$TESTPY_LOG_DIR/tests" --save-log-on-success \
    "${test_paths[@]}" 2>&1 | tee "$TESTPY_LOG_DIR/console.log"
