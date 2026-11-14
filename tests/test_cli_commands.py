import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliReceiveTests(unittest.TestCase):
    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'traceability_events', *arguments],
            capture_output=True, text=True, timeout=20)

    def test_receive_and_history_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / 'store'
            archive = root / 'raw'
            source = root / 'events.json'
            source.write_bytes(Path('fixtures/events.json').read_bytes())

            received = self._run('receive', str(source),
                                 '--store', str(store), '--archive', str(archive))
            self.assertEqual(received.returncode, 0, received.stderr)
            receipt = json.loads(received.stdout)
            self.assertEqual(receipt['counts']['accepted'], 2)
            self.assertTrue(archive.iterdir())

            epc = 'urn:epc:id:sgtin:0614141.107346.1'
            queried = self._run('history', epc, '--store', str(store))
            self.assertEqual(queried.returncode, 0, queried.stderr)
            view = json.loads(queried.stdout)
            self.assertEqual(view['epc'], epc)
            self.assertEqual(view['eventCount'], 2)
            self.assertEqual(view['history'][0]['roles'], ['child'])

            # 同内容再次接收：回执显示重交，事件库记录数不变。
            again = self._run('receive', str(source),
                              '--store', str(store), '--archive', str(archive))
            self.assertEqual(json.loads(again.stdout)['counts']['redelivery'], 2)

    def test_rejected_batch_returns_nonzero_but_partial_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'mixed.json'
            source.write_text(json.dumps({
                'type': 'EPCISDocument',
                'schemaVersion': '2.0',
                'epcisBody': {'eventList': [
                    {
                        'type': 'ObjectEvent',
                        'eventID': 'urn:uuid:ok',
                        'eventTime': '2026-11-01T10:00:00+08:00',
                        'eventTimeZoneOffset': '+08:00',
                        'epcList': ['urn:epc:id:sgtin:1.1.1'],
                    },
                    {'type': 'ObjectEvent'},
                ]},
            }), encoding='utf-8')
            process = self._run('receive', str(source),
                                '--store', str(root / 'store'))
            self.assertEqual(process.returncode, 2)
            receipt = json.loads(process.stdout)
            self.assertEqual(receipt['counts']['accepted'], 1)
            self.assertEqual(receipt['counts']['rejected'], 1)

    def test_unknown_document_fails_without_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bad.json'
            source.write_text('{"type": "something-else"}', encoding='utf-8')
            process = self._run('receive', str(source),
                                '--store', str(Path(directory) / 'store'))
            self.assertEqual(process.returncode, 1)
            self.assertIn('命令失败', process.stderr)


if __name__ == '__main__':
    unittest.main()
