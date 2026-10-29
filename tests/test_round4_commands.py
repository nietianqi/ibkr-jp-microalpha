import io
import json
from contextlib import redirect_stdout,redirect_stderr
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ibkr_microalpha.cli import main
from ibkr_microalpha.recording import CaptureConfig
from tests.test_round4_recording import metadata,BASE
from tests.test_round4_research import T,q
from tests.test_round4_integration import CONFIG


class ResearchCommandTests(unittest.TestCase):
    def test_capture_cli_preserves_input_and_closes_evidence_before_success(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'capture-config.json'
            config.write_text(json.dumps(CaptureConfig((metadata(),),session_id='unit').to_dict()),encoding='utf-8')
            source=root/'callbacks.jsonl'
            original=json.dumps(dict(callback='marketDataType',received_at=BASE.isoformat(),request_id=1,
                                     data=dict(market_data_type=1)))+'\n'
            source.write_text(original,encoding='utf-8')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['capture-callbacks',str(source),'--capture-config',str(config),
                                      '--output',str(root/'out')]),0)
            self.assertEqual(source.read_text(encoding='utf-8'),original)
            self.assertFalse(json.loads((root/'out/capture.json').read_text())['execution_enabled'])
            self.assertEqual(len((root/'out/raw-callbacks.jsonl').read_text().splitlines()),1)

    def test_capture_source_collision_is_rejected_without_modification(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'config.json'
            config.write_text(json.dumps(CaptureConfig((metadata(),)).to_dict()),encoding='utf-8')
            source=root/'events.jsonl'
            source.write_text('UNCHANGED',encoding='utf-8')
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                main(['capture-callbacks',str(source),'--capture-config',str(config),'--output',str(root)])
            self.assertEqual(source.read_text(),'UNCHANGED')

    def test_scan_cli_is_registered_before_simulation_and_never_claims_deployment(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'quotes.jsonl'
            rows=[]
            decisions=[]
            for day in (0,1):
                shift=timedelta(days=day)
                for second in range(8):
                    quote=q(second)
                    data=asdict(quote)
                    data.pop('at');data.pop('event_id')
                    for field in ('bid','ask'):data[field]=str(data[field])
                    for field in ('bid_at','ask_at'):data[field]=(data[field]+shift).isoformat()
                    rows.append(dict(event_id=f'{day}:{second}',received_at=(quote.at+shift).isoformat(),
                                     sequence=second,type='quote',data=data))
                decisions.append(dict(decision_id=str(day),symbol='S',at=(T+shift).isoformat(),score=1,quantity=100))
            source.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
            (root/'decisions.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in decisions),encoding='utf-8')
            plan=dict(dataset_id='UNIT_ONLY',simulation=dict(participation_rate='1',submit_latency_seconds=0,
                ack_latency_seconds=0),scan=dict(holding_periods=[2],fee_multipliers=[1],slippage_ticks=[1],
                min_days=2,min_samples=2,draws=400))
            (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
            with redirect_stdout(io.StringIO()):
                result=main(['scan-holdings',str(source),'--decisions',str(root/'decisions.jsonl'),
                    '--config',str(CONFIG),'--plan',str(root/'plan.json'),'--registry',str(root/'registry'),
                    '--output',str(root/'report.json')])
            self.assertEqual(result,0)
            report=json.loads((root/'report.json').read_text())
            self.assertFalse(report['deployable'])
            self.assertEqual(report['execution_source'],'SIMULATED')
            self.assertTrue((root/'registry'/(report['experiment_id']+'.json')).exists())


if __name__=='__main__':unittest.main()
