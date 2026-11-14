import copy
import json
from pathlib import Path
import tempfile
import unittest

from traceability_events.history import object_history
from traceability_events.store import EventStore

EPC_1 = 'urn:epc:id:sgtin:0614141.107346.1'
EPC_3 = 'urn:epc:id:sgtin:0614141.107346.3'
PARENT = 'urn:epc:id:sscc:0614141.1234567890'


def document(*events):
    return {'type': 'EPCISDocument', 'schemaVersion': '2.0',
            'epcisBody': {'eventList': list(events)}}


def packing(action='ADD', children=None, event_id='urn:uuid:event-1'):
    return {
        'type': 'AggregationEvent',
        'eventID': event_id,
        'eventTime': '2026-11-01T10:00:00+08:00',
        'eventTimeZoneOffset': '+08:00',
        'action': action,
        'parentID': PARENT,
        'childEPCs': children if children is not None else [
            EPC_1, 'urn:epc:id:sgtin:0614141.107346.2'],
        'bizStep': 'packing',
    }


def transformation():
    return {
        'type': 'TransformationEvent',
        'eventID': 'urn:uuid:event-2',
        'eventTime': '2026-11-01T11:00:00+08:00',
        'eventTimeZoneOffset': '+08:00',
        'inputEPCList': [EPC_1],
        'outputEPCList': [EPC_3],
        'transformationID': 'urn:lab:transform-1',
    }


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = EventStore(self.root / 'store')

    def tearDown(self):
        self._tmp.cleanup()

    def _receive(self, doc):
        raw = json.dumps(doc, ensure_ascii=False).encode('utf-8')
        return self.store.receive_document(json.loads(raw.decode('utf-8')),
                                           source_bytes=raw)

    def test_history_includes_object_parent_child_and_input_roles(self):
        self._receive(document(packing(), transformation()))
        view = object_history(self.store, EPC_1)
        self.assertEqual(view['eventCount'], 2)
        first, second = view['history']
        self.assertEqual(first['roles'], ['child'])
        self.assertEqual(first['action'], 'ADD')
        self.assertIn('关系建立', first['actionRelationshipMeaning'])
        self.assertEqual(second['roles'], ['input'])
        self.assertIsNone(second['action'])
        # 容器关系单独呈现，不与货号摊平。
        self.assertEqual(view['upstreamDeclaredContainers'][0]['parentIDs'],
                         [PARENT])

    def test_add_and_delete_carry_different_relationship_meanings(self):
        self._receive(document(
            packing('ADD'),
            packing('DELETE', event_id='urn:uuid:event-1b')))
        view = object_history(self.store, EPC_1)
        meanings = {entry['action']: entry['actionRelationshipMeaning']
                    for entry in view['history']}
        self.assertIn('关系建立', meanings['ADD'])
        self.assertIn('关系解除', meanings['DELETE'])

    def test_unresolved_corrective_reference_resolves_after_late_event(self):
        declared = packing()
        declared['errorDeclaration'] = {
            'declarationTime': '2026-11-02T10:00:00+08:00',
            'reason': 'incorrect_data',
            'correctiveEventIDs': ['urn:uuid:event-3'],
        }
        # 先收原文，纠错声明后到。
        self._receive(document(packing()))
        self._receive(document(declared))
        before = object_history(self.store, EPC_1)
        self.assertEqual(
            [item['correctiveEventID']
             for item in before['unresolvedCorrectiveReferences']],
            ['urn:uuid:event-3'])
        self.assertTrue(
            before['history'][0]['errorDeclaration']
            ['hasUnresolvedCorrectiveReference'])

        corrective = packing('ADD', event_id='urn:uuid:event-3',
                             children=[EPC_1])
        corrective['parentID'] = 'urn:epc:id:sscc:0614141.1234567899'
        self._receive(document(corrective))
        after = object_history(self.store, EPC_1)
        self.assertEqual(after['unresolvedCorrectiveReferences'], [])
        declaration = after['history'][0]['errorDeclaration']
        self.assertFalse(declaration['hasUnresolvedCorrectiveReference'])
        self.assertTrue(declaration['correctiveEvents'][0]['resolved'])
        # 原事件仍在，未被纠正事件改写。
        record = self.store.index_by_event_id()['urn:uuid:event-1']
        self.assertEqual(len(record['versions']), 2)
        self.assertNotIn('errorDeclaration',
                         record['versions'][0]['rawEvent'])

    def test_missing_prior_history_is_flagged_never_invented(self):
        # 直接来一条 DELETE，没有任何 ADD 前史。
        self._receive(document(packing('DELETE', event_id='urn:uuid:only')))
        view = object_history(self.store, EPC_1)
        self.assertEqual(view['eventCount'], 1)
        self.assertEqual(len(view['historyGaps']), 1)
        self.assertEqual(view['historyGaps'][0]['action'], 'DELETE')
        self.assertIn('不猜测入库', view['historyGaps'][0]['note'])

    def test_conflict_is_visible_in_history(self):
        self._receive(document(packing()))
        changed = packing()
        changed['bizStep'] = 'unpacking'
        self._receive(document(changed))
        view = object_history(self.store, EPC_1)
        self.assertEqual(len(view['conflicts']), 1)
        self.assertEqual(view['history'][0]['recordStatus'], 'conflict')


if __name__ == '__main__':
    unittest.main()
