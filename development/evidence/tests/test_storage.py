import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'
sys.path.insert(0, str(SCRIPTS))
from bc_core.storage import ProjectStore, StateError


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project'
        self.store = ProjectStore(self.root)
        self.store.initialize('Food')

    def tearDown(self):
        self.tmp.cleanup()

    def test_committed_revision_is_authority_if_mirror_is_lost(self):
        with self.store.lock():
            p = self.store.load()
            p['selected_example'] = 'v0'
            self.store.commit(p, expected_revision=0)
        (self.root / 'project.json').write_text('{broken')
        recovered = ProjectStore(self.root).load()
        self.assertEqual(recovered['selected_example'], 'v0')
        self.assertEqual(recovered['revision'], 1)

    def test_snapshot_committed_before_mirror_crash_is_recoverable(self):
        def crash(stage):
            if stage == 'snapshot_committed':
                raise RuntimeError('simulated interruption')
        self.store.failpoint = crash
        with self.assertRaises(RuntimeError), self.store.lock():
            p = self.store.load()
            p['chosen'] = 'original'
            self.store.commit(p, expected_revision=0)
        self.assertEqual(ProjectStore(self.root).load()['chosen'], 'original')

    def test_corrupt_latest_snapshot_reports_degradation_and_blocks_write(self):
        with self.store.lock():
            p = self.store.load()
            p['chosen'] = 'v1'
            self.store.commit(p, expected_revision=0)
        (self.root / 'state/revisions/0000000001.json').write_text('{}')
        p = self.store.load()
        self.assertEqual(p['revision'], 0)
        self.assertTrue(self.store.recovery_report['degraded'])
        with self.assertRaisesRegex(StateError, 'degraded'), self.store.lock():
            self.store.commit(p, expected_revision=0)

    def test_stale_commit_is_rejected(self):
        old = self.store.load()
        with self.store.lock():
            self.store.commit(old, expected_revision=0)
            with self.assertRaisesRegex(StateError, 'revision'):
                self.store.commit(old, expected_revision=0)

    def test_missing_formal_revision_is_detected_from_newer_mirror(self):
        with self.store.lock():
            self.store.commit(self.store.load(), expected_revision=0)
        (self.root / 'state/revisions/0000000001.json').unlink()
        recovered = self.store.load()
        self.assertEqual(recovered['revision'], 0)
        self.assertTrue(self.store.recovery_report['degraded'])

    def test_second_writer_is_rejected_without_deleting_attempts(self):
        other = ProjectStore(self.root)
        with self.store.lock():
            with self.assertRaisesRegex(StateError, 'locked'), other.lock():
                pass
        with other.lock():
            self.assertEqual(other.load()['revision'], 0)

    def test_immutable_request_cannot_be_overwritten(self):
        self.store.write_record('requests', 'req_one', {'id': 'req_one'}, immutable=True)
        with self.assertRaisesRegex(StateError, 'exists'):
            self.store.write_record('requests', 'req_one', {'id': 'req_one', 'changed': True}, immutable=True)
        self.assertEqual(self.store.read_record('requests', 'req_one'), {'id': 'req_one'})

    def test_request_tampering_is_not_silently_used(self):
        self.store.write_record('requests', 'req_one', {'id': 'req_one'}, immutable=True)
        p = self.root / 'requests/req_one.json'
        data = json.loads(p.read_text())
        data['record']['id'] = 'req_other'
        p.write_text(json.dumps(data))
        with self.assertRaises(StateError):
            self.store.read_record('requests', 'req_one')

    def test_record_ids_and_asset_paths_cannot_escape(self):
        with self.assertRaises(StateError):
            self.store.record_path('requests', '../escape')
        with self.assertRaises(StateError):
            self.store.project_path('../outside.png')
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (self.root / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StateError):
            self.store.project_path('escape/out.png')

    def test_initialize_does_not_overwrite_existing_project(self):
        with self.assertRaises(StateError):
            self.store.initialize('Again')


if __name__ == '__main__':
    unittest.main()
