DESCRIPTION = 'EPCIS 追溯事件的对象集合与纠错声明采集'
SAMPLE = 'fixtures/events.json'
DEFAULT_STORE = '.work/store'
DEFAULT_ARCHIVE = '.work/raw'

import argparse
import json
from pathlib import Path
import sys
import tempfile

from .archive import archive_file
from .core import inspect_file
from .history import object_history
from .store import EventStore


def _load_bytes(path):
    return Path(path).read_bytes()


def _cmd_receive(args):
    payload_bytes = _load_bytes(args.path)
    store = EventStore(args.store, archive_root=args.archive)
    receipt = store.receive_document(
        json.loads(payload_bytes.decode('utf-8')),
        source_name=Path(args.path).name,
        source_bytes=payload_bytes)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 2 if receipt['counts']['rejected'] else 0


def _cmd_history(args):
    # history 的第一个位置参数是 EPC（path 槽位复用）。
    target = args.epc or (None if args.path == SAMPLE else args.path)
    if not target:
        print('history 命令需要提供要查询的 EPC，例如: history urn:epc:id:sgtin:...',
              file=sys.stderr)
        return 2
    store = EventStore(args.store)
    result = object_history(store, target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _demo_corrective_document():
    return {
        'type': 'EPCISDocument',
        'schemaVersion': '2.0',
        'epcisBody': {
            'eventList': [
                {
                    'type': 'AggregationEvent',
                    'eventID': 'urn:uuid:event-3',
                    'eventTime': '2026-11-02T09:00:00+08:00',
                    'eventTimeZoneOffset': '+08:00',
                    'action': 'ADD',
                    'parentID': 'urn:epc:id:sscc:0614141.1234567899',
                    'childEPCs': [
                        'urn:epc:id:sgtin:0614141.107346.1',
                        'urn:epc:id:sgtin:0614141.107346.2',
                    ],
                    'bizStep': 'repacking',
                }
            ]
        },
    }


def _cmd_demo(args):
    with tempfile.TemporaryDirectory() as directory:
        store_root = Path(directory) / 'store'
        archive_root = Path(directory) / 'raw'
        store = EventStore(store_root, archive_root=archive_root)

        first_bytes = _load_bytes(SAMPLE)
        first = store.receive_document(
            json.loads(first_bytes.decode('utf-8')),
            source_name=Path(SAMPLE).name, source_bytes=first_bytes)
        # 同内容重交：应识别为 redelivery，不产生新记录。
        redelivery = store.receive_document(
            json.loads(first_bytes.decode('utf-8')),
            source_name=Path(SAMPLE).name, source_bytes=first_bytes)

        declaration_bytes = _load_bytes('fixtures/declaration.json')
        declaration = store.receive_document(
            json.loads(declaration_bytes.decode('utf-8')),
            source_name='declaration.json', source_bytes=declaration_bytes)

        target_epc = 'urn:epc:id:sgtin:0614141.107346.1'
        before_correction = object_history(store, target_epc)

        corrective_doc = _demo_corrective_document()
        corrective_bytes = json.dumps(
            corrective_doc, ensure_ascii=False, sort_keys=True).encode('utf-8')
        corrective = store.receive_document(
            corrective_doc, source_name='corrective.json',
            source_bytes=corrective_bytes)
        after_correction = object_history(store, target_epc)

        result = {
            '原始资料': archive_file(SAMPLE, archive_root),
            '资料预览': inspect_file(SAMPLE),
            '首次接收回执': first['counts'],
            '同内容重交回执': redelivery['counts'],
            '错误声明补交回执': declaration['counts'],
            '纠正事件补交回执': corrective['counts'],
            '补交前未解决纠错引用': [
                item['correctiveEventID']
                for item in before_correction['unresolvedCorrectiveReferences']],
            '补交后未解决纠错引用': after_correction['unresolvedCorrectiveReferences'],
            '对象历程事件数': after_correction['eventCount'],
            '上游声明的容器关系': after_correction['upstreamDeclaredContainers'],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command',
                        choices=['inspect', 'capture', 'receive', 'history', 'demo'])
    parser.add_argument('path', nargs='?', default=SAMPLE)
    parser.add_argument('epc', nargs='?', default=None)
    parser.add_argument('--archive', default=DEFAULT_ARCHIVE)
    parser.add_argument('--store', default=DEFAULT_STORE)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            print(json.dumps(inspect_file(Path(args.path)),
                             ensure_ascii=False, indent=2))
            code = 0
        elif args.command == 'capture':
            print(json.dumps(archive_file(Path(args.path), Path(args.archive)),
                             ensure_ascii=False, indent=2))
            code = 0
        elif args.command == 'receive':
            code = _cmd_receive(args)
        elif args.command == 'history':
            code = _cmd_history(args)
        else:
            code = _cmd_demo(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f'命令失败: {exc}', file=sys.stderr)
        code = 1
    sys.exit(code)


if __name__ == '__main__':
    main()
