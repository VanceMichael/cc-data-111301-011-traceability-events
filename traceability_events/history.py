"""对象历程查询：只汇总来源已经声明的事实，不补造、不改写。

查询在读取事件库时实时计算，因此补交遗漏事件后历程立即更新，而历史记录
本身只追加、永不重写。
"""
from __future__ import annotations

from .parsing import (
    KIND_EPC,
    KIND_QUANTITY,
    ROLE_CHILD,
    ROLE_PARENT,
)

RELATIONSHIP_ACTIONS = {
    'ADD': '关系建立/加入（上游声明）',
    'DELETE': '关系解除/移除（上游声明）',
    'OBSERVE': '仅观察现状，未声明关系变化',
    None: '转换输入/输出事实（无 action 语义）',
}


def _matches(participation, epc):
    if participation['kind'] == KIND_EPC:
        return participation['epc'] == epc, 'epc'
    if participation['kind'] == KIND_QUANTITY:
        return participation['epcClass'] == epc, 'epcClass'
    return False, None


def object_history(store, epc):
    """返回一件货在所有已接收来源事件中的历程。

    覆盖角色：ObjectEvent 对象、AggregationEvent 父容器/子级、
    TransformationEvent 输入/输出，并附上 action 对关系的不同含义。
    """
    records = list(store.iter_records())
    event_id_index = {
        record['eventID']: record['key']
        for record in records if record.get('eventID')
    }

    participations_out = []
    seen_add = False
    for record in records:
        normalized = record['normalized']
        matched_roles = []
        matched_by = set()
        for participation in normalized['participations']:
            hit, basis = _matches(participation, epc)
            if hit:
                matched_roles.append(participation)
                matched_by.add(basis)
        if not matched_roles:
            continue

        action = normalized['action']
        if action == 'ADD':
            seen_add = True

        declaration = normalized.get('errorDeclaration')
        declaration_view = None
        if declaration is not None:
            corrective = []
            for corrective_id in declaration['correctiveEventIDs']:
                corrective.append({
                    'correctiveEventID': corrective_id,
                    'resolved': corrective_id in event_id_index,
                    'resolvedByKey': event_id_index.get(corrective_id),
                })
            declaration_view = {
                'declarationTime': declaration['declarationTime'],
                'reason': declaration['reason'],
                'correctiveEvents': corrective,
                'hasUnresolvedCorrectiveReference': any(
                    item['resolved'] is False for item in corrective),
                # 原文仍在 record.versions 中保留，这里只做可见性标记。
                'evidenceStatus': '原事件保留且被标记错误',
            }

        participations_out.append({
            'key': record['key'],
            'eventID': record['eventID'],
            'eventType': normalized['type'],
            'eventTime': normalized['eventTime'],
            'recordTime': normalized['recordTime'],
            'eventTimeZoneOffset': normalized['eventTimeZoneOffset'],
            'action': action,
            'actionRelationshipMeaning': RELATIONSHIP_ACTIONS[action],
            'roles': sorted({item['role'] for item in matched_roles}),
            'matchedBy': sorted(matched_by),
            'matchedParticipations': matched_roles,
            'bizStep': normalized.get('bizStep'),
            'transformationID': normalized.get('transformationID'),
            'errorDeclaration': declaration_view,
            'recordStatus': record['status'],
            'conflictCount': len(record.get('conflicts', [])),
        })

    participations_out.sort(key=lambda item: (
        item['eventTime']['instant'], item['key']))

    # 机械推导的前史缺口：只提示，不合成任何入库/出库事件。
    gaps = []
    add_seen = False
    for entry in participations_out:
        action = entry['action']
        if action == 'ADD':
            add_seen = True
        elif action in ('DELETE', 'OBSERVE') and not add_seen:
            gaps.append({
                'key': entry['key'],
                'eventID': entry['eventID'],
                'action': action,
                'note': '事件库中不存在更早的 ADD 前史；按约定不猜测入库，'
                        '仅标记来源前史缺口，等待补交事件',
            })

    unresolved_references = []
    for record in records:
        declaration = record['normalized'].get('errorDeclaration')
        if declaration is None:
            continue
        for corrective_id in declaration['correctiveEventIDs']:
            if corrective_id not in event_id_index:
                unresolved_references.append({
                    'declaringEventID': record['eventID'],
                    'declaringKey': record['key'],
                    'correctiveEventID': corrective_id,
                    'reason': declaration['reason'],
                    'declarationTime': declaration['declarationTime'],
                })

    conflicts = []
    for record in records:
        for conflict in record.get('conflicts', []):
            conflicts.append({
                'key': record['key'],
                'eventID': record['eventID'],
                'canonicalHash': conflict['canonicalHash'],
                'source': conflict['source'],
                'note': '同身份事件存在协议不允许的内容变化，冲突副本已保留，原文未改写',
            })

    containers = _container_views(records, epc)

    return {
        'epc': epc,
        'eventCount': len(participations_out),
        'history': participations_out,
        'upstreamDeclaredContainers': containers,
        'historyGaps': gaps,
        'unresolvedCorrectiveReferences': unresolved_references,
        'conflicts': conflicts,
    }


def _container_views(records, epc):
    """汇总该货作为子级出现时，上游声明的父容器关系及其 ADD/DELETE 语义。"""
    views = []
    for record in records:
        normalized = record['normalized']
        roles = normalized['participations']
        as_child = any(
            item['role'] == ROLE_CHILD and _matches(item, epc)[0]
            for item in roles)
        if not as_child:
            continue
        parents = [item['epc'] for item in roles if item['role'] == ROLE_PARENT]
        views.append({
            'key': record['key'],
            'eventID': record['eventID'],
            'eventTime': normalized['eventTime']['instant'],
            'action': normalized['action'],
            'actionRelationshipMeaning': RELATIONSHIP_ACTIONS[normalized['action']],
            'parentIDs': parents,
            'note': '容器关系为上游 AggregationEvent 声明，不与普通货号摊平',
        })
    views.sort(key=lambda item: (item['eventTime'], item['key']))
    return views
