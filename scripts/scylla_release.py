"""Resolve a stable Scylla tag, split its test suites, and fetch it with CCM."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


SCYLLA_REPOSITORY = 'https://github.com/scylladb/scylladb.git'
# These contain harness code, fixtures, manual/performance tests, or C++ tests.
EXCLUDED_DIRECTORIES = {
    'boost', 'unit', 'raft', 'vector_search', 'lib', 'pylib', 'resource',
    'manual', 'perf', 'pylib_test', '__pycache__',
}
SUITE_SHARDS = {'cluster': 8, 'cqlpy': 4, 'alternator': 2, 'rest_api': 2}


def latest_release(refs):
    """Select final numeric tags only, including dereferenced annotated tags."""
    releases = {}
    for line in refs.splitlines():
        sha, ref = line.split()
        match = re.fullmatch(r'refs/tags/scylla-(\d+)\.(\d+)\.(\d+)(\^\{\})?', ref)
        if match:
            version = tuple(map(int, match.group(1, 2, 3)))
            if version not in releases or match.group(4):
                releases[version] = sha
    if not releases:
        raise ValueError('No stable Scylla release tags found')
    version = max(releases)
    text = '.'.join(map(str, version))
    return {'version': text, 'tag': f'scylla-{text}', 'revision': releases[version]}


def test_plan(source):
    """Partition every Python/CQL test file, preserving module fixtures."""
    jobs = []
    for directory in sorted((source / 'test').iterdir()):
        if not directory.is_dir() or directory.name in EXCLUDED_DIRECTORIES:
            continue
        files = []
        for path in sorted(directory.rglob('*')):
            if any(part in EXCLUDED_DIRECTORIES for part in path.relative_to(directory).parts[:-1]):
                continue
            name = path.name
            if path.is_file() and (
                (path.suffix == '.py' and (
                    name.startswith('test_') or name.endswith(('_test.py', '_tests.py'))
                )) or name.endswith('_test.cql')
            ):
                files.append(str(path.relative_to(source)))
        if not files:
            continue
        shards = min(SUITE_SHARDS.get(directory.name, 1), len(files))
        for shard in range(shards):
            jobs.append({
                'name': f'{directory.name}-{shard + 1}',
                'suite': directory.name,
                'files': files[shard::shards],
            })
    if not jobs:
        raise ValueError('No Python/CQL tests found in the release checkout')
    return jobs


def write_outputs(values):
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream:
            for name, value in values.items():
                stream.write(f'{name}={value}\n')


def fetch_release(release, source, symbols=False):
    """Install the pinned package with CCM and expose it at upstream's path."""
    from ccmlib.common import get_scylla_full_version
    from ccmlib import scylla_repository

    install, actual = scylla_repository.setup(f'release:{release["version"]}')
    if actual != release['version']:
        raise ValueError(f'CCM installed {actual}, expected {release["version"]}')
    full_version = get_scylla_full_version(install)
    build_hash = full_version.rsplit('.', 1)[-1]
    if (
        not re.fullmatch('[0-9a-f]{7,40}', build_hash)
        or not release['revision'].startswith(build_hash)
    ):
        raise ValueError(f'Package {full_version} does not match tag commit {release["revision"]}')
    executable = Path(install) / 'bin' / 'scylla'

    if symbols:
        # GDB tests need DWARF data. The matching unstripped core package is
        # published alongside CCM's unified release package; let CCM fetch it.
        _, packages = scylla_repository.setup(f'release:{release["version"]}', skip_downloads=True)
        unified = packages.scylla_unified_package
        if not unified or '/scylla-unified-' not in unified:
            raise ValueError('Cannot locate the matching unstripped release package')
        url = unified.replace('/scylla-unified-', '/scylla-unstripped-')
        package = source / 'build' / 'gdb-package'
        runtime = source / 'build' / 'gdb-runtime'
        # CCM's extraction helper expects an empty target, including on retries.
        for directory in (package, runtime):
            if directory.exists():
                shutil.rmtree(directory)
        package.mkdir(parents=True, exist_ok=True)
        scylla_repository.download_version(
            version=release['version'], url=url, target_dir=str(package), verbose=True,
        )
        subprocess.run([
            str(package / 'install.sh'), '--nonroot', '--without-systemd',
            '--prefix', str(runtime),
        ], check=True)
        executable = runtime / 'libexec' / 'scylla'
        # Run the ELF directly so upstream's GDB fixture can use it with -se.
        subprocess.run([
            'patchelf', '--set-rpath', str(runtime / 'libreloc'), str(executable),
        ], check=True)

    # No build.ninja: upstream path_to('release', 'scylla') uses this CMake path.
    binary = source / 'build' / 'RelWithDebInfo' / 'scylla'
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.unlink(missing_ok=True)
    binary.symlink_to(executable)
    print(json.dumps({**release, 'ccm_version': full_version, 'executable': str(executable)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    resolve = commands.add_parser('resolve')
    resolve.add_argument('--output', type=Path, required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('source', type=Path)
    plan.add_argument('--output', type=Path, required=True)
    fetch = commands.add_parser('fetch')
    fetch.add_argument('--release', type=Path, required=True)
    fetch.add_argument('--source', type=Path, required=True)
    fetch.add_argument('--symbols', action='store_true')
    args = parser.parse_args()
    if args.command == 'resolve':
        refs = subprocess.check_output(['git', 'ls-remote', '--tags', SCYLLA_REPOSITORY], text=True)
        release = latest_release(refs)
        args.output.write_text(json.dumps(release, indent=2) + '\n')
        write_outputs(release)
        print(json.dumps(release))
        if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(summary, 'a') as stream:
                stream.write(f'Scylla release: **{release["tag"]}**\n\n')
                stream.write(f'Source commit: `{release["revision"]}`\n\n')
    elif args.command == 'plan':
        jobs = test_plan(args.source.resolve())
        args.output.write_text(json.dumps(jobs, indent=2) + '\n')
        image = (args.source / 'tools/toolchain/image').read_text().strip()
        if not re.fullmatch(r'docker.io/scylladb/scylla-toolchain:[A-Za-z0-9_.-]+', image):
            raise ValueError(f'Unexpected release toolchain image: {image}')
        matrix = {'include': [{'name': job['name'], 'suite': job['suite']} for job in jobs]}
        write_outputs({'matrix': json.dumps(matrix), 'toolchain': image})
        print(json.dumps(matrix, indent=2))
        if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(summary, 'a') as stream:
                stream.write(f'Test plan: **{len(jobs)} jobs**, ')
                stream.write(f'**{sum(len(job["files"]) for job in jobs)} files**\n\n')
                for job in jobs:
                    stream.write(f'- {job["name"]}: {len(job["files"])} files\n')
    else:
        fetch_release(json.loads(args.release.read_text()), args.source.resolve(), args.symbols)


if __name__ == '__main__':
    main()
