import tempfile,unittest
from pathlib import Path
from PIL import Image
import test_engine
from bc_core.storage import StateError

class SeriesTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp
    tearDown = test_engine.EngineTests.tearDown
    auth = test_engine.EngineTests.auth
    prepare = test_engine.EngineTests.prepare
    output = test_engine.EngineTests.output
    finish = test_engine.EngineTests.finish
    def test_dependency_binding_and_changed_anchor_reprepare(self):
        other=self.engine.create_work('豆腐')['id']; group=self.engine.create_group([self.work,other],kind='visual_series')
        first,second=group['item_ids']; grant=self.engine.authorize([self.work,other],['generate'],3,paid_channel_scope=['offline'],evidence={'fixture':'series'})['id']
        self.engine.set_item_dependencies(group['id'],second,[{'item_id':first,'version_id':None}])
        blocked=self.engine.prepare(other,grant,'offline',test_engine.caps(),group_id=group['id'],item_id=second)
        self.assertEqual(blocked['status'],'blocked')
        p=self.prepare(auth=grant,group_id=group['id'],item_id=first)
        v=self.finish(p)['added_version_ids'][0]
        self.engine.set_item_dependencies(group['id'],second,[{'item_id':first,'version_id':v}])
        prepared=self.engine.prepare(other,grant,'offline',test_engine.caps(),group_id=group['id'],item_id=second)
        self.engine.set_series_anchor(group['id'],v,['lighting','table'])
        self.engine.set_item_anchor(group['id'],second,[])
        outcome=self.engine.dispatch(prepared['attempt']['id'])
        self.assertEqual(outcome['status'],'reprepared')
        self.assertEqual(outcome['request']['prepared_from']['anchor']['version_id'],v)
        self.assertEqual(self.engine.dispatch(outcome['attempt']['id'])['status'],'dispatching')
        with self.assertRaises(StateError): self.engine.set_item_dependencies(group['id'],first,[{'item_id':second,'version_id':v}])

    def test_failed_attempt_output_cannot_be_anchor_or_dependency(self):
        group=self.engine.create_group([self.work,self.work],kind='narrative_series'); first,second=group['item_ids'];grant=self.auth(count=2)
        p=self.prepare(auth=grant,group_id=group['id'],item_id=first);aid=p['attempt']['id'];self.engine.dispatch(aid)
        result=self.engine.observe(aid,'failed','submitted',{'fixture':'failure with candidate'},outputs=[self.output(aid)])
        v=result['added_version_ids'][0]
        with self.assertRaisesRegex(StateError,'succeeded'): self.engine.set_series_anchor(group['id'],v,['lighting'])
        with self.assertRaisesRegex(StateError,'succeeded'): self.engine.set_item_dependencies(group['id'],second,[{'item_id':first,'version_id':v}])
