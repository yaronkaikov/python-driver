"""Contracts for selecting one release and partitioning all upstream suites."""

import importlib.util
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'scylla_release', Path(__file__).resolve().parents[2] / 'scripts/scylla_release.py',
)
release_ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_ci)


class ReleaseSelectionTest(unittest.TestCase):
    def test_latest_numeric_final_tag_excludes_rc_and_candidate_tags(self):
        refs = '\n'.join([
            'old refs/tags/scylla-2026.3.9',
            'latest refs/tags/scylla-2026.3.10',
            'rc refs/tags/scylla-2026.4.0-rc1',
            'candidate refs/tags/scylla-2026.4.0-candidate-20261101',
            'legacy refs/tags/scylla-6.2.99',
        ])
        self.assertEqual(release_ci.latest_release(refs), {
            'version': '2026.3.10', 'tag': 'scylla-2026.3.10', 'revision': 'latest',
        })

    def test_annotated_tag_uses_commit_in_either_order(self):
        lines = ['tag-object refs/tags/scylla-2026.3.4',
                 'commit refs/tags/scylla-2026.3.4^{}']
        for refs in (lines, list(reversed(lines))):
            self.assertEqual(release_ci.latest_release('\n'.join(refs))['revision'], 'commit')

    def test_no_final_tags_fails(self):
        with self.assertRaisesRegex(ValueError, 'No stable'):
            release_ci.latest_release('rc refs/tags/scylla-2026.4.0-rc1')


class TestPlanTest(unittest.TestCase):
    def test_all_files_including_nested_suites_appear_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            expected = [f'test/cluster/test_case_{i}.py' for i in range(19)]
            expected += ['test/cqlpy/cassandra_tests/nested_test.py',
                         'test/cql/approval_test.cql', 'test/new_suite/new_tests.py']
            ignored = ['test/cluster/conftest.py', 'test/cluster/helper.py',
                       'test/cluster/manual/test_manual.py', 'test/pylib/test_helper.py',
                       'test/boost/test_compiled.py', 'test/vector_search/a_test.cc']
            for name in expected + ignored:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            jobs = release_ci.test_plan(source)
            assigned = [name for job in jobs for name in job['files']]
            self.assertCountEqual(assigned, expected)
            self.assertEqual(len(assigned), len(set(assigned)))
            self.assertEqual(len([job for job in jobs if job['suite'] == 'cluster']), 8)
            self.assertTrue(all(job['files'] for job in jobs))

    def test_empty_plan_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / 'test').mkdir()
            with self.assertRaisesRegex(ValueError, 'No Python/CQL'):
                release_ci.test_plan(source)


class PackageIdentityTest(unittest.TestCase):
    def setUp(self):
        self.package = ModuleType('ccmlib')
        self.common = ModuleType('ccmlib.common')
        self.repository = SimpleNamespace(
            setup=lambda *args, **kwargs: ('/ccm/release', '2026.3.4'),
        )
        self.package.scylla_repository = self.repository
        self.common.get_scylla_full_version = lambda path: '2026.3.4-0.20261018.abcdef123456'
        self.release = {'version': '2026.3.4', 'revision': 'abcdef1234567890'}
        self.modules = patch.dict('sys.modules', {
            'ccmlib': self.package, 'ccmlib.common': self.common,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_ccm_version_mismatch_fails_before_exposing_binary(self):
        self.repository.setup = lambda *args, **kwargs: ('/ccm/release', '2026.3.5')
        with self.assertRaisesRegex(ValueError, 'expected 2026.3.4'):
            release_ci.fetch_release(self.release, Path('/unused'))

    def test_build_commit_mismatch_fails_before_exposing_binary(self):
        self.common.get_scylla_full_version = lambda path: '2026.3.4-0.20261018.123456789abc'
        with self.assertRaisesRegex(ValueError, 'does not match tag commit'):
            release_ci.fetch_release(self.release, Path('/unused'))

    def test_matching_package_exposes_upstream_release_mode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            release_ci.fetch_release(self.release, source)
            self.assertEqual(
                (source / 'build/RelWithDebInfo/scylla').readlink(),
                Path('/ccm/release/bin/scylla'),
            )

    def test_debugger_uses_matching_unstripped_package_through_ccm(self):
        unified = 'https://example.test/scylla-unified-2026.3.4-build.x86_64.tar.gz'

        def setup(version, **kwargs):
            if kwargs.get('skip_downloads'):
                return '/unused', SimpleNamespace(scylla_unified_package=unified)
            return '/ccm/release', '2026.3.4'

        self.repository.setup = setup
        self.repository.download_version = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.object(release_ci.subprocess, 'run'):
            source = Path(directory)
            release_ci.fetch_release(self.release, source, symbols=True)
            self.assertEqual(
                self.repository.download_version.call_args.kwargs['url'],
                unified.replace('/scylla-unified-', '/scylla-unstripped-'),
            )
            self.assertEqual(
                (source / 'build/RelWithDebInfo/scylla').readlink(),
                source / 'build/gdb-runtime/libexec/scylla',
            )
