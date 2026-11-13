import json
from pathlib import Path


def inspect_file(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if value['type'] != 'EPCISDocument':
        raise ValueError('不是 EPCIS 文档')
    events = value['epcisBody']['eventList']
    return {'version':value['schemaVersion'],'events':len(events),'types':[event['type'] for event in events]}
