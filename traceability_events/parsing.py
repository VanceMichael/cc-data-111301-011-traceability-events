"""EPCIS 2.0 JSON 业务子集的解析与规范化。

只依赖标准库，不做 JSON-LD 远端上下文解析。规范化结果遵守两条硬边界：

* 事件时间、记录时间、时区偏移分别保留，不合并成单一时间字段；
* 不同集合中的对象保持各自角色（对象 / 数量对象 / 父容器 / 子级 /
  转换输入 / 转换输出），绝不摊平成同一种货号。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

DOCUMENT_TYPE = 'EPCISDocument'
OBJECT_EVENT = 'ObjectEvent'
AGGREGATION_EVENT = 'AggregationEvent'
TRANSFORMATION_EVENT = 'TransformationEvent'
EVENT_TYPES = frozenset((OBJECT_EVENT, AGGREGATION_EVENT, TRANSFORMATION_EVENT))
ACTIONS = frozenset(('ADD', 'DELETE', 'OBSERVE'))

_OFFSET_RE = re.compile(r'^[+-]\d{2}:\d{2}$')

ACTION_MEANINGS = {
    'ADD': '上游声明：关系建立或对象加入',
    'DELETE': '上游声明：关系解除或对象移除',
    'OBSERVE': '上游观察：仅记录现状，不声明关系变化',
    None: '上游声明：转换事件的输入/输出事实（无 action）',
}

ROLE_OBJECT = 'object'
ROLE_PARENT = 'parent'
ROLE_CHILD = 'child'
ROLE_INPUT = 'input'
ROLE_OUTPUT = 'output'

KIND_EPC = 'epc'
KIND_QUANTITY = 'quantity'


class EventValidationError(ValueError):
    """单个事件不符合子集约定；index 为其在 eventList 中的位置。"""

    def __init__(self, index, message):
        self.index = index
        super().__init__(f'事件 {index}: {message}')


def canonical_event_hash(raw_event):
    """对事件原文做规范化哈希，用于无 eventID 事件的稳定内容身份。"""
    encoded = json.dumps(raw_event, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def without_error_declaration(raw_event):
    """去掉 errorDeclaration 后的事件副本，用于判断差异是否仅限纠错声明。"""
    clone = dict(raw_event)
    clone.pop('errorDeclaration', None)
    return clone


def parse_document(payload):
    """解析整份 EPCISDocument，返回原始事件列表（字典原样保留）。"""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode('utf-8')
    if isinstance(payload, str):
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f'来源不是合法 JSON: {exc}') from exc
    else:
        value = payload
    if not isinstance(value, dict) or value.get('type') != DOCUMENT_TYPE:
        raise ValueError('不是 EPCIS 文档')
    body = value.get('epcisBody')
    if not isinstance(body, dict) or not isinstance(body.get('eventList'), list):
        raise ValueError('EPCIS 文档缺少 epcisBody.eventList')
    return list(body['eventList'])


def _parse_timestamp(index, value, field):
    if not isinstance(value, str) or not value:
        raise EventValidationError(index, f'{field} 必须是带时区的时间字符串')
    text = value[:-1] + '+00:00' if value.endswith('Z') else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EventValidationError(index, f'{field} 时间无法解析: {value}') from exc
    if moment.tzinfo is None:
        raise EventValidationError(index, f'{field} 必须携带时区信息: {value}')
    instant = moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'original': value, 'instant': instant}, moment


def _check_offset(index, moment, offset):
    if not isinstance(offset, str) or not _OFFSET_RE.match(offset):
        raise EventValidationError(index, f'eventTimeZoneOffset 格式不正确: {offset!r}')
    sign = 1 if offset[0] == '+' else -1
    offset_minutes = sign * (int(offset[1:3]) * 60 + int(offset[4:6]))
    actual_minutes = int(moment.utcoffset().total_seconds() // 60)
    if offset_minutes != actual_minutes:
        raise EventValidationError(
            index, f'eventTimeZoneOffset {offset} 与 eventTime 自带偏移 {actual_minutes} 分钟不一致')


def _epc_participations(index, raw_event, field, role):
    items = raw_event.get(field)
    if items is None:
        return []
    if not isinstance(items, list):
        raise EventValidationError(index, f'{field} 必须是数组')
    participations = []
    for position, item in enumerate(items):
        if not isinstance(item, str) or not item:
            raise EventValidationError(index, f'{field}[{position}] 必须是非空标识字符串')
        participations.append({'role': role, 'kind': KIND_EPC, 'epc': item})
    return participations


def _quantity_participations(index, raw_event, field, role):
    items = raw_event.get(field)
    if items is None:
        return []
    if not isinstance(items, list):
        raise EventValidationError(index, f'{field} 必须是数组')
    participations = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise EventValidationError(index, f'{field}[{position}] 必须是数量对象')
        epc_class = item.get('epcClass')
        if not isinstance(epc_class, str) or not epc_class:
            raise EventValidationError(index, f'{field}[{position}].epcClass 必须是非空字符串')
        quantity = item.get('quantity')
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            raise EventValidationError(index, f'{field}[{position}].quantity 必须是数值')
        uom = item.get('uom')
        if uom is not None and not isinstance(uom, str):
            raise EventValidationError(index, f'{field}[{position}].uom 必须是字符串')
        entry = {'role': role, 'kind': KIND_QUANTITY,
                 'epcClass': epc_class, 'quantity': quantity}
        if uom is not None:
            entry['uom'] = uom
        participations.append(entry)
    return participations


def _parse_error_declaration(index, raw_event):
    declaration = raw_event.get('errorDeclaration')
    if declaration is None:
        return None
    if not isinstance(declaration, dict):
        raise EventValidationError(index, 'errorDeclaration 必须是对象')
    if 'declarationTime' not in declaration:
        raise EventValidationError(index, 'errorDeclaration.declarationTime 缺失')
    declaration_time, _ = _parse_timestamp(
        index, declaration['declarationTime'], 'errorDeclaration.declarationTime')
    reason = declaration.get('reason')
    if not isinstance(reason, str) or not reason:
        raise EventValidationError(index, 'errorDeclaration.reason 必须是非空字符串')
    corrective_ids = declaration.get('correctiveEventIDs', [])
    if not isinstance(corrective_ids, list) or any(
            not isinstance(item, str) or not item for item in corrective_ids):
        raise EventValidationError(index, 'errorDeclaration.correctiveEventIDs 必须是标识字符串数组')
    return {
        'declarationTime': declaration_time,
        'reason': reason,
        'correctiveEventIDs': list(corrective_ids),
    }


def _optional_string(index, raw_event, field):
    value = raw_event.get(field)
    if value is not None and (not isinstance(value, str) or not value):
        raise EventValidationError(index, f'{field} 必须是非空字符串')
    return value


def parse_event(raw_event, index):
    """把单个原始事件规范化为结构化视图；非法时抛出 EventValidationError。"""
    if not isinstance(raw_event, dict):
        raise EventValidationError(index, '事件必须是 JSON 对象')

    event_type = raw_event.get('type')
    if event_type not in EVENT_TYPES:
        raise EventValidationError(index, f'不支持的事件类型: {event_type!r}')

    event_id = raw_event.get('eventID')
    if event_id is not None and (not isinstance(event_id, str) or not event_id):
        raise EventValidationError(index, 'eventID 必须是非空字符串')

    if 'eventTime' not in raw_event:
        raise EventValidationError(index, '缺少 eventTime')
    event_time, moment = _parse_timestamp(index, raw_event['eventTime'], 'eventTime')
    offset = raw_event.get('eventTimeZoneOffset')
    if offset is None:
        raise EventValidationError(index, '缺少 eventTimeZoneOffset')
    _check_offset(index, moment, offset)

    record_time = None
    if 'recordTime' in raw_event:
        record_time, _ = _parse_timestamp(index, raw_event['recordTime'], 'recordTime')

    participations = []
    action = None

    if event_type == OBJECT_EVENT:
        action = raw_event.get('action', 'OBSERVE')
        if action not in ACTIONS:
            raise EventValidationError(index, f'ObjectEvent action 非法: {action!r}')
        participations.extend(_epc_participations(index, raw_event, 'epcList', ROLE_OBJECT))
        participations.extend(_quantity_participations(index, raw_event, 'quantityList', ROLE_OBJECT))

    elif event_type == AGGREGATION_EVENT:
        action = raw_event.get('action')
        if action not in ACTIONS:
            raise EventValidationError(
                index, f'AggregationEvent 必须声明 action（ADD/DELETE/OBSERVE）: {action!r}')
        parent_id = raw_event.get('parentID')
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise EventValidationError(index, 'parentID 必须是非空字符串')
            participations.append({'role': ROLE_PARENT, 'kind': KIND_EPC, 'epc': parent_id})
        elif action == 'ADD':
            raise EventValidationError(index, 'AggregationEvent ADD 必须携带 parentID')
        participations.extend(_epc_participations(index, raw_event, 'childEPCs', ROLE_CHILD))
        participations.extend(
            _quantity_participations(index, raw_event, 'childQuantityList', ROLE_CHILD))

    else:  # TransformationEvent 没有 action，输入/输出集合严格分列
        participations.extend(_epc_participations(index, raw_event, 'inputEPCList', ROLE_INPUT))
        participations.extend(
            _quantity_participations(index, raw_event, 'inputQuantityList', ROLE_INPUT))
        participations.extend(_epc_participations(index, raw_event, 'outputEPCList', ROLE_OUTPUT))
        participations.extend(
            _quantity_participations(index, raw_event, 'outputQuantityList', ROLE_OUTPUT))

    if not participations:
        raise EventValidationError(index, '事件未携带任何可追溯对象，拒绝接收空事件')

    normalized = {
        'type': event_type,
        'eventID': event_id,
        'eventTime': event_time,
        'recordTime': record_time,
        'eventTimeZoneOffset': offset,
        'action': action,
        'actionMeaning': ACTION_MEANINGS[action],
        'bizStep': _optional_string(index, raw_event, 'bizStep'),
        'disposition': _optional_string(index, raw_event, 'disposition'),
        'transformationID': _optional_string(index, raw_event, 'transformationID'),
        'participations': participations,
        'errorDeclaration': _parse_error_declaration(index, raw_event),
    }
    return normalized
