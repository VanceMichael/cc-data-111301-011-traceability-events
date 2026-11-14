"""事件库：忠实采集、稳定身份、重交与冲突处理。

磁盘布局（全部为 UTF-8 JSON，写入均经临时文件原子替换）::

    <root>/events/<key>.json       一条事件身份对应的完整记录
    <root>/receipts/<n>.json       每次批量接收的回执

身份边界（不使用随机 UUID）：

* 有 eventID：key = 'id-' + sha256(eventID)，身份跟随来源标识；
* 无 eventID：key = 'ch-' + sha256(规范化事件原文)，同内容重交稳定命中同一
  条记录，内容变化即产生新身份，旧证据不被覆盖。

errorDeclaration 是协议允许补充的部分：原文重交计为重交；仅新增/等价纠错
声明计为补充；其他任何差异都记为冲突，原文与冲突副本各自保留。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from . import archive
from .parsing import (
    EventValidationError,
    canonical_event_hash,
    parse_document,
    parse_event,
    without_error_declaration,
)


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent, delete=False) as writer:
        temporary = Path(writer.name)
        json.dump(payload, writer, ensure_ascii=False, indent=2, sort_keys=True)
        writer.write('\n')
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(temporary, path)


def event_identity(raw_event):
    """返回 (key, 身份依据说明)；无 eventID 时明确标记为内容哈希。"""
    event_id = raw_event.get('eventID') if isinstance(raw_event, dict) else None
    if isinstance(event_id, str) and event_id:
        import hashlib
        digest = hashlib.sha256(event_id.encode('utf-8')).hexdigest()
        return 'id-' + digest, {'basis': 'eventID', 'eventID': event_id}
    content_hash = canonical_event_hash(raw_event)
    return 'ch-' + content_hash, {'basis': 'canonical_content_hash',
                                  'canonicalHash': content_hash,
                                  'reason': '来源事件未提供 eventID'}


class EventStore:
    def __init__(self, root, archive_root=None):
        self.root = Path(root)
        self.events_dir = self.root / 'events'
        self.receipts_dir = self.root / 'receipts'
        self.archive_root = Path(archive_root) if archive_root is not None else None
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def iter_records(self):
        for path in sorted(self.events_dir.glob('*.json')):
            yield json.loads(path.read_text(encoding='utf-8'))

    def get_record(self, key):
        path = self.events_dir / f'{key}.json'
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding='utf-8'))

    def index_by_event_id(self):
        index = {}
        for record in self.iter_records():
            event_id = record.get('eventID')
            if event_id:
                index[event_id] = record
        return index

    # ------------------------------------------------------------------
    def _ingest(self, raw_event, index, source_ref):
        key, identity = event_identity(raw_event)
        outcome = {'key': key, 'identity': identity}
        existing = self.get_record(key)

        if existing is None:
            normalized = parse_event(raw_event, index)
            record = {
                'key': key,
                'eventID': normalized['eventID'],
                'status': 'accepted',
                'normalized': normalized,
                'sources': [source_ref],
                'versions': [{
                    'receivedOrder': 0,
                    'source': source_ref,
                    'rawEvent': raw_event,
                    'canonicalHash': canonical_event_hash(raw_event),
                    'change': 'initial',
                }],
                'conflicts': [],
            }
            _atomic_write_json(self.events_dir / f'{key}.json', record)
            outcome.update({'outcome': 'accepted', 'status': record['status']})
            return outcome, record

        full_hash = canonical_event_hash(raw_event)
        initial_hash = existing['versions'][0]['canonicalHash']
        known_hashes = {version['canonicalHash'] for version in existing['versions']}
        if full_hash == initial_hash or full_hash in known_hashes:
            if source_ref not in existing['sources']:
                existing['sources'].append(source_ref)
            existing['versions'].append({
                'receivedOrder': len(existing['versions']),
                'source': source_ref,
                'rawEvent': raw_event,
                'canonicalHash': full_hash,
                'change': 'redelivery_identical',
            })
            _atomic_write_json(self.events_dir / f'{key}.json', existing)
            outcome.update({'outcome': 'redelivery', 'status': existing['status']})
            return outcome, existing

        base_hash = canonical_event_hash(
            without_error_declaration(existing['versions'][0]['rawEvent']))
        new_base_hash = canonical_event_hash(without_error_declaration(raw_event))
        if base_hash == new_base_hash:
            # 差异仅限 errorDeclaration：允许补充；声明等价仍算重交。
            existing_declaration = existing['versions'][0]['rawEvent'].get('errorDeclaration')
            new_declaration = raw_event.get('errorDeclaration')
            if existing_declaration is None and new_declaration is not None:
                normalized = parse_event(raw_event, index)
                existing['normalized'] = normalized
                existing['status'] = 'accepted_with_error_declaration'
                change = 'error_declaration_supplemented'
            else:
                change = 'error_declaration_redelivery'
            existing['versions'].append({
                'receivedOrder': len(existing['versions']),
                'source': source_ref,
                'rawEvent': raw_event,
                'canonicalHash': full_hash,
                'change': change,
            })
            if source_ref not in existing['sources']:
                existing['sources'].append(source_ref)
            _atomic_write_json(self.events_dir / f'{key}.json', existing)
            outcome.update({'outcome': 'supplemented', 'status': existing['status']})
            return outcome, existing

        # 协议不允许的内容变化：保留冲突，不改写原始证据。
        conflict = {
            'receivedOrder': len(existing['versions']),
            'source': source_ref,
            'rawEvent': raw_event,
            'canonicalHash': full_hash,
            'change': 'conflicting_redelivery',
        }
        existing['conflicts'].append(conflict)
        existing['versions'].append(dict(conflict))
        existing['status'] = 'conflict'
        if source_ref not in existing['sources']:
            existing['sources'].append(source_ref)
        _atomic_write_json(self.events_dir / f'{key}.json', existing)
        outcome.update({'outcome': 'conflict', 'status': existing['status']})
        return outcome, existing

    def receive_document(self, payload, source_name=None, source_bytes=None):
        """批量接收一份文档，单事件失败不影响其余事件。

        回执与查询可见性保持一致：只有成功落盘的事件才出现在结果与事件库中。
        """
        raw_events = parse_document(payload)
        if source_bytes is None:
            source_bytes = json.dumps(
                payload, ensure_ascii=False, sort_keys=True).encode('utf-8')

        source_ref = None
        if self.archive_root is not None:
            archive_dir = Path(self.archive_root)
            archive_dir.mkdir(parents=True, exist_ok=True)
            import hashlib
            digest = hashlib.sha256(source_bytes).hexdigest()
            target = archive_dir / digest
            if not target.exists():
                import tempfile as _tempfile
                with _tempfile.NamedTemporaryFile(
                        dir=archive_dir, delete=False) as writer:
                    temporary = Path(writer.name)
                    writer.write(source_bytes)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.replace(temporary, target)
            source_ref = {'kind': 'archive', 'sha256': digest,
                          'name': source_name, 'bytes': len(source_bytes)}
        else:
            source_ref = {'kind': 'inline', 'name': source_name}

        results = []
        counts = {'accepted': 0, 'redelivery': 0, 'supplemented': 0,
                  'conflict': 0, 'rejected': 0}
        for index, raw_event in enumerate(raw_events):
            try:
                outcome, _record = self._ingest(raw_event, index, source_ref)
            except EventValidationError as exc:
                outcome = {'index': index, 'outcome': 'rejected',
                           'identity': _safe_identity(raw_event),
                           'error': str(exc)}
                counts['rejected'] += 1
            else:
                outcome.setdefault('index', index)
                counts[outcome['outcome']] += 1
            results.append(outcome)

        receipt = {
            'source': source_ref,
            'total': len(raw_events),
            'counts': counts,
            'allAccepted': counts['rejected'] == 0,
            'results': results,
        }
        receipt_number = 1 + len(list(self.receipts_dir.glob('*.json')))
        receipt_path = self.receipts_dir / f'{receipt_number:06d}.json'
        _atomic_write_json(receipt_path, receipt)
        receipt['receipt'] = str(receipt_path)
        return receipt


def _safe_identity(raw_event):
    """被拒事件也尽量报告身份；无 eventID 时不生成任何随机标识。"""
    try:
        key, identity = event_identity(raw_event)
        identity['key'] = key
        return identity
    except (TypeError, ValueError):
        return {'basis': 'unidentifiable',
                'reason': '事件结构损坏，无法计算稳定身份'}
