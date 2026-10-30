"""Exercise all four specified adoption windows through the actual engine."""
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))
from bc_core.engine import ProjectEngine
from test_engine import caps


class AdoptionCrashWindowTests(unittest.TestCase):
    def test_four_adoption_windows_preserve_selection_and_never_resubmit(self):
        for boundary in ('asset_saved', 'attempt_outputs_saved', 'snapshot_committed', 'mirror_updated'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / 'source.png'
                output = root / 'output.png'
                Image.new('RGB', (4, 4), (40, 60, 80)).save(source)
                Image.new('RGB', (4, 4), (80, 60, 40)).save(output)
                engine = ProjectEngine(root / 'project')
                engine.initialize()
                work_id = engine.create_work('soup')['id']
                original = engine.import_image(work_id, source)['version']['id']
                auth = engine.authorize([work_id], ['generate'], 1, paid_channel_scope=['offline'],
                                        evidence={'fixture': 'authorized'})['id']
                prepared = engine.prepare(work_id, auth, 'offline', caps())
                attempt_id = prepared['attempt']['id']
                engine.dispatch(attempt_id)
                engine.select_version(original)

                def crash(stage):
                    if stage == boundary:
                        raise RuntimeError('injected crash at ' + stage)

                engine.store.failpoint = crash
                with self.assertRaises(RuntimeError):
                    engine.observe(attempt_id, 'succeeded', 'submitted', {'fixture': 'completed'}, outputs=[{
                        'output_index': 0, 'path': str(output),
                        'provenance': {'attempt_id': attempt_id, 'verified': True, 'source': 'offline_fixture'},
                    }])
                reopened = ProjectEngine(root / 'project')
                first = reopened.recover()
                second = reopened.recover()
                work = second['project']['works'][work_id]
                self.assertEqual(work['selected_version_id'], original)
                self.assertEqual(work['edit_target_version_id'], original)
                self.assertEqual(len(second['attempts']), 1)
                self.assertEqual(work['version_ids'], first['project']['works'][work_id]['version_ids'])
                self.assertEqual(len(work['version_ids']), 1 if boundary == 'asset_saved' else 2)
                self.assertEqual(second['attempts'][0]['state'],
                                 'unknown' if boundary == 'asset_saved' else 'succeeded')
                if boundary == 'asset_saved':
                    self.assertIn(attempt_id, second['unresolved_attempt_ids'])


if __name__ == '__main__':
    unittest.main()
