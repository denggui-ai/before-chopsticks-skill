import unittest
import test_engine
from bc_core.engine import ProjectEngine
class SeriesMixedRecovery(unittest.TestCase):
    setUp=test_engine.EngineTests.setUp
    tearDown=test_engine.EngineTests.tearDown
    auth=test_engine.EngineTests.auth
    prepare=test_engine.EngineTests.prepare
    output=test_engine.EngineTests.output
    finish=test_engine.EngineTests.finish
    def test_success_unknown_prepared_resume_only_prepared(self):
        group=self.engine.create_group([self.work]*3,kind='visual_series');one,two,three=group['item_ids'];grant=self.auth(count=3)
        first=self.prepare(auth=grant,group_id=group['id'],item_id=one);success=self.finish(first)['added_version_ids'][0]
        pending=self.prepare(auth=grant,group_id=group['id'],item_id=two,begin_context=False);aid=pending['attempt']['id'];self.engine.dispatch(aid)
        third=self.prepare(auth=grant,group_id=group['id'],item_id=three,begin_context=False);thirdid=third['attempt']['id']
        self.engine=ProjectEngine(self.root/'project');recovered=self.engine.recover();attempts={a['id']:a for a in recovered['attempts']}
        self.assertEqual(attempts[aid]['state'],'unknown');self.assertEqual(attempts[thirdid]['state'],'prepared')
        self.assertIn(success,recovered['project']['items'][one]['version_ids']);self.assertIn(aid,recovered['project']['items'][two]['unresolved_attempt_ids'])
        self.assertEqual(self.engine.dispatch(thirdid)['status'],'dispatching')
        with self.assertRaisesRegex(ValueError,'never dispatch'):self.engine.dispatch(aid)
        self.assertEqual(len(self.engine.store.list_records('attempts')),3)
