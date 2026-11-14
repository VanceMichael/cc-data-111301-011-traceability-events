import copy
import json
from pathlib import Path
import tempfile
import unittest

from traceability_events.store import EventStore, event_identity


def document(*events):
    return {'type': 'EPCISDocument', 'schemaVersion': '2.0',
            'epcisBody': {'eventList': list(events)}}


def object_event(event_id='urn:uuid:e1', epc='urn:epc:id:sgtin:0614141.107346.1',
                 action='OBSERVE', **extra):
    event = {
        'type': 'ObjectEvent',
        'eventTime': '2026-11-01T10:00:00+08:00',
        'eventTimeZoneOffset': '+08:00',
        'action': action,
        'epcList': [epc],
    }
    if event_id is not None:
        event['eventID'] = event_id
    event.update(extra)
    return event


def aggregation_event(action='ADD', with_id='urn:uuid:agg-1'):
    event = {
        'type': 'AggregationEvent',
        'eventTime': '2026-11-01T10:00:00+08:00',
        'eventTimeZoneOffset': '+08:00',
        'action': action,
        'parentID': 'urn:epc:id:sscc:0614141.1234567890',
        'childEPCs': ['urn:epc:id:sgtin:0614141.107346.1',
                      'urn:epc:id:sgtin:0614141.107346.2'],
        'bizStep': 'packing',
    }
    if with_id is not None:
        event['eventID'] = with_id
    return event


class IdentityTests(unittest.TestCase):
    def test_eventid_identity_follows_source_identifier(self):
        first = object_event('urn:uuid:same', epc='urn:epc:a')
        second = object_event('urn:uuid:same', epc='urn:epc:b')
        key_first, id_first = event_identity(first)
        key_second, id_second = event_identity(second)
        self.assertEqual(key_first, key_second)
        self.assertTrue(key_first.startswith('id-'))
        self.assertEqual(id_first['basis'], 'eventID')

    def test_missing_eventid_uses_content_hash_not_randomness(self):
        first = object_event(None)
        second = copy.deepcopy(first)
        key_first, id_first = event_identity(first)
        key_second, _ = event_identity(second)
        self.assertTrue(key_first.startswith('ch-'))
        self.assertEqual(key_first, key_second)
        changed = copy.deepcopy(first)
        changed['bizStep'] = 'receiving'
        self.assertNotEqual(event_identity(changed)[0], key_first)


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = EventStore(self.root / 'store',
                                archive_root=self.root / 'raw')

    def tearDown(self):
        self._tmp.cleanup()

    def _receive(self, doc, name='doc.json'):
        raw = json.dumps(doc, ensure_ascii=False).encode('utf-8')
        return self.store.receive_document(json.loads(raw.decode('utf-8')),
                                           source_name=name, source_bytes=raw)

    def test_acceptance_persists_structured_and_raw_evidence(self):
        receipt = self._receive(document(object_event()))
        self.assertEqual(receipt['counts']['accepted'], 1)
        records = list(self.store.iter_records())
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['versions'][0]['rawEvent']['type'], 'ObjectEvent')
        self.assertEqual(record['normalized']['participations'][0]['epc'],
                         'urn:epc:id:sgtin:0614141.107346.1')
        # 原始字节已内容寻址归档，文件名即 sha256。
        archived = list((self.root / 'raw').iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(archived[0].name), 64)

    def test_single_bad_event_does_not_block_batch(self):
        good = object_event('urn:uuid:good')
        bad = {'type': 'ObjectEvent'}  # 缺时间与对象
        receipt = self._receive(document(good, bad))
        self.assertEqual(receipt['counts']['accepted'], 1)
        self.assertEqual(receipt['counts']['rejected'], 1)
        self.assertFalse(receipt['allAccepted'])
        # 查询可见性与回执一致：被拒事件不在事件库中。
        stored_keys = {record['key'] for record in self.store.iter_records()}
        self.assertEqual(len(stored_keys), 1)
        rejected = receipt['results'][1]
        self.assertEqual(rejected['outcome'], 'rejected')
        # 被拒事件即使能算出内容身份，也不得落盘可见。
        self.assertNotIn(rejected['identity'].get('key'), stored_keys)

    def test_identical_redelivery_is_idempotent(self):
        event = object_event()
        first = self._receive(document(event), 'a.json')
        second = self._receive(document(copy.deepcopy(event)), 'b.json')
        self.assertEqual(first['counts']['accepted'], 1)
        self.assertEqual(second['counts']['redelivery'], 1)
        records = list(self.store.iter_records())
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]['versions']), 2)
        self.assertEqual(records[0]['status'], 'accepted')
        self.assertEqual(len(list((self.root / 'store' / 'receipts').iterdir())), 2)

    def test_late_error_declaration_is_supplemented_not_replaced(self):
        event = aggregation_event()
        self._receive(document(event))
        declared = copy.deepcopy(event)
        declared['errorDeclaration'] = {
            'declarationTime': '2026-11-02T10:00:00+08:00',
            'reason': 'incorrect_data',
            'correctiveEventIDs': ['urn:uuid:event-3'],
        }
        receipt = self._receive(document(declared), 'declaration.json')
        self.assertEqual(receipt['counts']['supplemented'], 1)
        record = list(self.store.iter_records())[0]
        self.assertEqual(record['status'], 'accepted_with_error_declaration')
        self.assertEqual(len(record['versions']), 2)
        # 原始证据未改写。
        self.assertNotIn('errorDeclaration', record['versions'][0]['rawEvent'])
        self.assertEqual(
            record['normalized']['errorDeclaration']['correctiveEventIDs'],
            ['urn:uuid:event-3'])

    def test_disallowed_change_is_kept_as_conflict(self):
        event = object_event(action='OBSERVE')
        self._receive(document(event))
        changed = copy.deepcopy(event)
        changed['action'] = 'ADD'
        receipt = self._receive(document(changed))
        self.assertEqual(receipt['counts']['conflict'], 1)
        record = list(self.store.iter_records())[0]
        self.assertEqual(record['status'], 'conflict')
        self.assertEqual(len(record['conflicts']), 1)
        self.assertEqual(record['versions'][0]['rawEvent']['action'], 'OBSERVE')
        self.assertEqual(record['conflicts'][0]['rawEvent']['action'], 'ADD')

    def test_no_eventid_redelivery_stable_and_change_is_new_identity(self):
        event = object_event(None)
        first = self._receive(document(event))
        second = self._receive(document(copy.deepcopy(event)))
        self.assertEqual(second['counts']['redelivery'], 1)
        changed = copy.deepcopy(event)
        changed['bizStep'] = 'receiving'
        third = self._receive(document(changed))
        self.assertEqual(third['counts']['accepted'], 1)
        keys = {record['key'] for record in self.store.iter_records()}
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(key.startswith('ch-') for key in keys))


if __name__ == '__main__':
    unittest.main()
