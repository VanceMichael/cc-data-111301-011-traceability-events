import copy
import unittest

from traceability_events.parsing import (
    KIND_EPC,
    KIND_QUANTITY,
    ROLE_CHILD,
    ROLE_INPUT,
    ROLE_OBJECT,
    ROLE_OUTPUT,
    ROLE_PARENT,
    EventValidationError,
    canonical_event_hash,
    parse_event,
)


BASE = {
    'type': 'ObjectEvent',
    'eventID': 'urn:uuid:x',
    'eventTime': '2026-11-01T10:00:00+08:00',
    'eventTimeZoneOffset': '+08:00',
    'recordTime': '2026-11-01T10:05:00+08:00',
    'action': 'OBSERVE',
    'epcList': ['urn:epc:id:sgtin:0614141.107346.1'],
}


class ParsingTests(unittest.TestCase):
    def test_times_kept_separately_with_offset(self):
        normalized = parse_event(BASE, 0)
        self.assertEqual(normalized['eventTime']['original'],
                         '2026-11-01T10:00:00+08:00')
        self.assertEqual(normalized['eventTime']['instant'],
                         '2026-11-01T02:00:00Z')
        self.assertEqual(normalized['eventTimeZoneOffset'], '+08:00')
        self.assertEqual(normalized['recordTime']['instant'],
                         '2026-11-01T02:05:00Z')
        self.assertIsNone(parse_event({k: v for k, v in BASE.items()
                                       if k != 'recordTime'}, 0)['recordTime'])

    def test_offset_mismatch_is_rejected(self):
        bad = copy.deepcopy(BASE)
        bad['eventTimeZoneOffset'] = '+09:00'
        with self.assertRaises(EventValidationError):
            parse_event(bad, 2)

    def test_object_roles_are_not_flattened(self):
        raw = {
            'type': 'TransformationEvent',
            'eventID': 'urn:uuid:t',
            'eventTime': '2026-11-01T11:00:00+08:00',
            'eventTimeZoneOffset': '+08:00',
            'inputEPCList': ['urn:epc:id:sgtin:0614141.107346.1'],
            'inputQuantityList': [
                {'epcClass': 'urn:epc:id:lgtin:0614141.107346.Lot9',
                 'quantity': 12.5, 'uom': 'EA'}],
            'outputEPCList': ['urn:epc:id:sgtin:0614141.107346.3'],
        }
        normalized = parse_event(raw, 0)
        roles = {(p['role'], p['kind']) for p in normalized['participations']}
        self.assertEqual(roles, {
            (ROLE_INPUT, KIND_EPC),
            (ROLE_INPUT, KIND_QUANTITY),
            (ROLE_OUTPUT, KIND_EPC),
        })
        self.assertIsNone(normalized['action'])

    def test_aggregation_parent_and_child_roles_distinct(self):
        raw = {
            'type': 'AggregationEvent',
            'eventID': 'urn:uuid:a',
            'eventTime': '2026-11-01T10:00:00+08:00',
            'eventTimeZoneOffset': '+08:00',
            'action': 'ADD',
            'parentID': 'urn:epc:id:sscc:0614141.1234567890',
            'childEPCs': ['urn:epc:id:sgtin:0614141.107346.1'],
        }
        normalized = parse_event(raw, 0)
        roles = {p['role'] for p in normalized['participations']}
        self.assertEqual(roles, {ROLE_PARENT, ROLE_CHILD})
        self.assertNotIn(ROLE_OBJECT, roles)

    def test_aggregation_requires_action_and_add_requires_parent(self):
        raw = {
            'type': 'AggregationEvent',
            'eventTime': '2026-11-01T10:00:00+08:00',
            'eventTimeZoneOffset': '+08:00',
            'childEPCs': ['urn:epc:id:sgtin:0614141.107346.1'],
        }
        with self.assertRaises(EventValidationError):
            parse_event(raw, 0)
        raw['action'] = 'ADD'
        with self.assertRaises(EventValidationError):
            parse_event(raw, 0)

    def test_empty_participation_is_rejected(self):
        raw = copy.deepcopy(BASE)
        del raw['epcList']
        with self.assertRaises(EventValidationError):
            parse_event(raw, 0)

    def test_quantity_without_measure_is_rejected(self):
        raw = {
            'type': 'ObjectEvent',
            'eventTime': '2026-11-01T10:00:00+08:00',
            'eventTimeZoneOffset': '+08:00',
            'quantityList': [{'epcClass': 'urn:epc:id:lgtin:0614141.1.Lot1'}],
        }
        with self.assertRaises(EventValidationError):
            parse_event(raw, 0)

    def test_error_declaration_parsed_but_not_deleted(self):
        raw = copy.deepcopy(BASE)
        raw['errorDeclaration'] = {
            'declarationTime': '2026-11-02T10:00:00+08:00',
            'reason': 'incorrect_data',
            'correctiveEventIDs': ['urn:uuid:event-3'],
        }
        normalized = parse_event(raw, 0)
        declaration = normalized['errorDeclaration']
        self.assertEqual(declaration['correctiveEventIDs'], ['urn:uuid:event-3'])
        self.assertEqual(declaration['declarationTime']['instant'],
                         '2026-11-02T02:00:00Z')

    def test_canonical_hash_is_stable_and_order_insensitive(self):
        first = copy.deepcopy(BASE)
        second = copy.deepcopy(BASE)
        second['epcList'] = list(reversed(second['epcList']))
        second = dict(reversed(list(second.items())))
        self.assertEqual(canonical_event_hash(first),
                         canonical_event_hash(second))


if __name__ == '__main__':
    unittest.main()
