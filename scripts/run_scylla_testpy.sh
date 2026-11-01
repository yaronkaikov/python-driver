#!/usr/bin/env bash
# Run Scylla's in-tree CQL suite with a wheel from this driver's build.
set -euo pipefail

: "${SCYLLA_SOURCE:?Set SCYLLA_SOURCE to the matching Scylla checkout}"
: "${SCYLLA_EXE_URL:?Set SCYLLA_EXE_URL to the matching relocatable server URL}"
: "${DRIVER_WHEEL_DIR:?Set DRIVER_WHEEL_DIR to the candidate wheel directory}"
: "${TESTPY_LOG_DIR:?Set TESTPY_LOG_DIR to an absolute output directory}"

SCYLLA_SOURCE=$(realpath "$SCYLLA_SOURCE")
DRIVER_WHEEL_DIR=$(realpath "$DRIVER_WHEEL_DIR")
mkdir -p "$TESTPY_LOG_DIR"
TESTPY_LOG_DIR=$(realpath "$TESTPY_LOG_DIR")
mkdir -p "$TESTPY_LOG_DIR/pytest-tmp"
export PYTEST_DEBUG_TEMPROOT="$TESTPY_LOG_DIR/pytest-tmp"
export UV_PROJECT_ENVIRONMENT="$SCYLLA_SOURCE/build/driver-candidate-venv"
test_python="$UV_PROJECT_ENVIRONMENT/bin/python"

# Prevent the driver checkout or a caller's environment from shadowing the wheel.
unset PYTHONPATH PYTHONHOME
cd "$SCYLLA_SOURCE"

uv sync --project test --locked --no-dev --python 3.14

# Select by Python ABI and platform; never let pip fall back to a PyPI driver.
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
uv pip install --python "$test_python" --reinstall "$candidate_wheel"

# Record proof that the candidate wheel, including its compiled modules, is used.
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

# test.py normally re-syncs the pinned upstream driver. Bypass that sync after
# installing the candidate, and invoke this interpreter directly (not uv run).
export DISABLE_VENV=1
test_paths=("$@")
if [[ ${#test_paths[@]} == 0 ]]; then
    test_paths=(test/cqlpy)
fi
"$test_python" test.py --exe-url "$SCYLLA_EXE_URL" \
    --no-gather-metrics --jobs 2 --timeout 600 --session-timeout 5400 \
    --markers 'not non_gating' --tmpdir "$TESTPY_LOG_DIR/tests" \
    --save-log-on-success "${test_paths[@]}" 2>&1 | tee "$TESTPY_LOG_DIR/console.log"
