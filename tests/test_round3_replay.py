"""RPL-03/04: immutable sources, transactional publication and I/O fail-closed."""
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ibkr_microalpha.cli import _replay_file, main
from ibkr_microalpha.config import build_engine, load_config
from ibkr_microalpha.replay import Replay, ReplayFailed
from tests.test_replay import EXAMPLE_CONFIG, event


ARTIFACTS = ('raw-input.jsonl', 'audit.jsonl', 'frozen-config.json', 'execution.json',
             'execution-journal.jsonl', 'report.json')


class FaultSink(io.StringIO):
    def __init__(self, *, write=False, flush=False):
        super().__init__()
        self.fail_write, self.fail_flush = write, flush

    def write(self, value):
        if self.fail_write:
            raise OSError('injected write failure')
        return super().write(value)

    def flush(self):
        if self.fail_flush:
            raise OSError('injected flush failure')
        return super().flush()


class ReplayPersistenceTests(unittest.TestCase):
    def runner(self, **kwargs):
        return Replay(build_engine(load_config(EXAMPLE_CONFIG)), **kwargs)

    def assert_poisoned(self, runner):
        self.assertTrue(runner.failed)
        with self.assertRaises(ReplayFailed):
            runner.dispatch(event(sequence=2))
        with self.assertRaises(ReplayFailed):
            runner.finish()
        with TemporaryDirectory() as directory:
            with self.assertRaises(ReplayFailed):
                runner.save(Path(directory)/'rejected')
            self.assertFalse((Path(directory)/'rejected').exists())

    def test_raw_write_failure_poisons_applied_runner_and_forbids_retry(self):
        runner=self.runner(raw_sink=FaultSink(write=True))
        with self.assertRaises(OSError):
            runner.dispatch(event())
        self.assert_poisoned(runner)
        with self.assertRaises(ReplayFailed):
            runner.dispatch(event())

    def test_audit_partial_write_failure_poisons_runner(self):
        class PartialAuditSink(FaultSink):
            def write(self,value):
                if self.getvalue():
                    raise OSError('injected failure after first audit record')
                return super().write(value)
        sink=PartialAuditSink()
        runner=self.runner(raw_sink=io.StringIO(),audit_sink=sink)
        runner.engine.audit.extend([{'kind':'FIRST'},{'kind':'SECOND'}])
        with self.assertRaises(OSError):
            runner.dispatch(event())
        self.assertIn('FIRST',sink.getvalue())
        self.assertNotIn('SECOND',sink.getvalue())
        self.assert_poisoned(runner)

    def test_short_sink_write_is_not_committed_as_success(self):
        class ShortSink:
            def write(self,text): return len(text)-1
        runner=self.runner(raw_sink=ShortSink())
        with self.assertRaises(OSError):
            runner.dispatch(event())
        self.assertEqual(runner.events_processed,0)
        self.assertEqual(runner.events_applied,1)
        self.assert_poisoned(runner)

    def test_finish_audit_write_failure_poisons_runner(self):
        sink=FaultSink()
        runner=self.runner(raw_sink=io.StringIO(),audit_sink=sink)
        runner.dispatch(event())
        runner.engine.audit.append({'kind':'FINAL_BATCH'})
        sink.fail_write=True
        with self.assertRaises(OSError):
            runner.finish()
        self.assert_poisoned(runner)

    def test_finish_checks_both_sink_flushes(self):
        for role in ('raw_sink','audit_sink'):
            with self.subTest(role=role):
                sink=FaultSink()
                runner=self.runner(**{role:sink})
                runner.dispatch(event())
                sink.fail_flush=True
                with self.assertRaises(OSError):
                    runner.finish()
                self.assert_poisoned(runner)

    def test_save_flush_failure_poisons_and_does_not_publish_report(self):
        sink=FaultSink()
        runner=self.runner(raw_sink=sink)
        runner.dispatch(event())
        sink.fail_flush=True
        with TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                runner.save(directory)
            self.assertFalse((Path(directory)/'report.json').exists())
        self.assert_poisoned(runner)

    def test_duration_statistics_have_bounded_storage_and_exact_counts(self):
        runner=self.runner(raw_sink=io.StringIO(),audit_sink=io.StringIO())
        for n in range(1,10001):
            runner.dispatch(event(sequence=n))
        metrics=runner.metrics()
        self.assertEqual(metrics['by_event_type']['timer']['events'],10000)
        self.assertLess(metrics['retained']['timing_bins'],200)
        self.assertIn('approximate',metrics['note'])
        self.assertIn('wall',metrics['note'])


class ReplayPublicationTests(unittest.TestCase):
    def source(self, parent):
        source=parent/'events.jsonl'
        source.write_text(json.dumps(event())+'\n',encoding='utf-8')
        return source

    def existing(self, output):
        output.mkdir()
        for name in ARTIFACTS:
            (output/name).write_text('OLD:'+name,encoding='utf-8')
        return {name:(output/name).read_bytes() for name in ARTIFACTS}

    def assert_old(self, output, old):
        self.assertEqual({name:(output/name).read_bytes() for name in ARTIFACTS},old)

    def test_same_raw_source_is_rejected_without_touching_bytes(self):
        with TemporaryDirectory() as directory:
            output=Path(directory)
            source=output/'raw-input.jsonl'
            source.write_text(json.dumps(event())+'\n',encoding='utf-8')
            before=source.read_bytes()
            with self.assertRaisesRegex(ValueError,'conflict'):
                _replay_file(EXAMPLE_CONFIG,source,output)
            self.assertEqual(source.read_bytes(),before)
            self.assertFalse((output/'report.json').exists())

    def test_demo_rejects_generated_input_artifact_before_generation(self):
        with TemporaryDirectory() as directory:
            output=Path(directory)/'out'
            old=self.existing(output)
            with patch('ibkr_microalpha.demo.create_demo') as create, patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(['demo','--config',str(EXAMPLE_CONFIG),
                          '--events',str(output/'raw-input.jsonl'),'--output',str(output)])
            self.assertEqual(error.exception.code,2)
            create.assert_not_called()
            self.assert_old(output,old)

    def test_hardlinked_event_or_config_artifact_is_rejected(self):
        for source_kind in ('events','config'):
            with self.subTest(source_kind=source_kind), TemporaryDirectory() as directory:
                root=Path(directory)
                source=self.source(root)
                config=root/'config.json'
                config.write_bytes(EXAMPLE_CONFIG.read_bytes())
                output=root/'out'
                output.mkdir()
                target=output/('audit.jsonl' if source_kind=='events' else 'execution.json')
                original=source if source_kind=='events' else config
                os.link(original,target)
                before=original.read_bytes()
                with self.assertRaisesRegex(ValueError,'conflict'):
                    _replay_file(config,source,output)
                self.assertEqual(original.read_bytes(),before)
                self.assertEqual(target.read_bytes(),before)

    def test_config_in_output_frozen_config_is_rejected_before_writes(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            output.mkdir()
            config=output/'frozen-config.json'
            config.write_bytes(EXAMPLE_CONFIG.read_bytes())
            before=config.read_bytes()
            with self.assertRaisesRegex(ValueError,'conflict'):
                _replay_file(config,self.source(root),output)
            self.assertEqual(config.read_bytes(),before)

    def test_build_failure_preserves_existing_success(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            old=self.existing(output)
            config=root/'invalid.json'
            config.write_text('{}',encoding='utf-8')
            with self.assertRaises(ValueError):
                _replay_file(config,self.source(root),output)
            self.assert_old(output,old)

    def test_parse_failure_preserves_old_run_and_records_separate_failure(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            old=self.existing(output)
            source=root/'bad.jsonl'
            source.write_text(json.dumps(event(sequence='bad'))+'\n',encoding='utf-8')
            with self.assertRaises(ValueError):
                _replay_file(EXAMPLE_CONFIG,source,output)
            self.assert_old(output,old)
            self.assertTrue(list((output/'.failed').glob('*/failure.json')))

    def test_report_is_written_only_after_input_and_audit_streams_close(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            streams=[]
            report_stream_states=[]
            original_open,original_write=Path.open,Path.write_text
            def opened(path,*args,**kwargs):
                stream=original_open(path,*args,**kwargs)
                if path.name in ('raw-input.jsonl','audit.jsonl') and args and args[0]=='w':
                    streams.append(stream)
                return stream
            def written(path,*args,**kwargs):
                if path.name=='report.json':
                    report_stream_states.append(all(stream.closed for stream in streams))
                return original_write(path,*args,**kwargs)
            with patch.object(Path,'open',opened),patch.object(Path,'write_text',written):
                report,result=_replay_file(EXAMPLE_CONFIG,self.source(root),root/'out')
            self.assertEqual(report['events_processed'],1)
            self.assertTrue(result.exists())
            self.assertTrue(report_stream_states)
            self.assertTrue(all(report_stream_states))

    def test_close_failure_preserves_old_run_and_poison_is_recorded(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            old=self.existing(output)
            source=self.source(root)
            original_open=Path.open
            class CloseFault:
                def __init__(self,stream): self.stream=stream
                def __enter__(self): return self.stream
                def __exit__(self,*args):
                    self.stream.close()
                    raise OSError('injected close failure')
            def opened(path,*args,**kwargs):
                stream=original_open(path,*args,**kwargs)
                if path.name=='raw-input.jsonl' and args and args[0]=='w':
                    return CloseFault(stream)
                return stream
            with patch.object(Path,'open',opened):
                with self.assertRaises(OSError):
                    _replay_file(EXAMPLE_CONFIG,source,output)
            self.assert_old(output,old)
            failures=list((output/'.failed').glob('*/failure.json'))
            self.assertTrue(failures)
            self.assertTrue(json.loads(failures[0].read_text())['runner_poisoned'])

    def test_middle_publication_failure_rolls_back_complete_previous_run(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            old=self.existing(output)
            source=self.source(root)
            replace=os.replace
            fired=[False]
            def fault(src,dst):
                if Path(dst).resolve()==(output/'execution.json').resolve() and not fired[0]:
                    fired[0]=True
                    raise OSError('injected publication failure')
                return replace(src,dst)
            with patch('ibkr_microalpha.cli.os.replace',side_effect=fault):
                with self.assertRaises(OSError):
                    _replay_file(EXAMPLE_CONFIG,source,output)
            self.assertTrue(fired[0])
            self.assert_old(output,old)
            self.assertEqual(source.read_text(),json.dumps(event())+'\n')

    def test_success_replaces_older_artifacts_and_preserves_nonartifact_source(self):
        with TemporaryDirectory() as directory:
            output=Path(directory)/'out'
            old=self.existing(output)
            source=self.source(output)
            before=source.read_bytes()
            (output/'failure.json').write_text('OLD FAILURE',encoding='utf-8')
            report,result=_replay_file(EXAMPLE_CONFIG,source,output)
            self.assertEqual(report['events_processed'],1)
            self.assertEqual(source.read_bytes(),before)
            self.assertFalse((output/'failure.json').exists())
            self.assertEqual(result.read_bytes(),(output/'report.json').read_bytes())
            self.assertNotEqual((output/'raw-input.jsonl').read_bytes(),old['raw-input.jsonl'])

    def test_report_commit_failure_restores_old_run_and_source(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            old=self.existing(output)
            source=self.source(root)
            replace=os.replace
            fired=[False]
            def fault(src,dst):
                if Path(dst).resolve()==(output/'report.json').resolve() and not fired[0]:
                    fired[0]=True
                    raise OSError('injected report commit failure')
                return replace(src,dst)
            with patch('ibkr_microalpha.cli.os.replace',side_effect=fault):
                with self.assertRaises(OSError):
                    _replay_file(EXAMPLE_CONFIG,source,output)
            self.assert_old(output,old)

    def test_rollback_failure_retains_backup_and_removes_success_marker(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            output=root/'out'
            old=self.existing(output)
            source=self.source(root)
            replace=os.replace
            publishing_failed=[False]
            def fault(src,dst):
                source_path,target_path=Path(src).resolve(),Path(dst).resolve()
                if target_path==(output/'execution.json').resolve() and not publishing_failed[0]:
                    publishing_failed[0]=True
                    raise OSError('injected publication failure')
                if publishing_failed[0] and '-previous-' in source_path.parent.name and \
                        target_path==(output/'raw-input.jsonl').resolve():
                    raise OSError('injected persistent rollback failure')
                return replace(src,dst)
            with patch('ibkr_microalpha.cli.os.replace',side_effect=fault):
                with self.assertRaises(OSError) as error:
                    _replay_file(EXAMPLE_CONFIG,source,output)
            self.assertFalse((output/'report.json').exists())
            backups=list(root.glob('.out-previous-*'))
            self.assertEqual(len(backups),1)
            self.assertEqual((backups[0]/'report.json').read_bytes(),old['report.json'])
            self.assertEqual((backups[0]/'raw-input.jsonl').read_bytes(),old['raw-input.jsonl'])
            self.assertIn('rollback incomplete',' '.join(error.exception.__notes__))

    def test_fresh_failure_has_no_success_report_and_can_rebuild_from_source(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            source=self.source(root)
            before=source.read_bytes()
            original_open=Path.open
            class CloseFault:
                def __init__(self,stream): self.stream=stream
                def __enter__(self): return self.stream
                def __exit__(self,*args):
                    self.stream.close()
                    raise OSError('injected first-run close failure')
            def opened(path,*args,**kwargs):
                stream=original_open(path,*args,**kwargs)
                if path.name=='raw-input.jsonl' and args and args[0]=='w':
                    return CloseFault(stream)
                return stream
            output=root/'out'
            with patch.object(Path,'open',opened):
                with self.assertRaises(OSError):
                    _replay_file(EXAMPLE_CONFIG,source,output)
            self.assertFalse((output/'report.json').exists())
            self.assertTrue((output/'failure.json').exists())
            self.assertEqual(source.read_bytes(),before)
            report,_=_replay_file(EXAMPLE_CONFIG,source,output)
            self.assertEqual(report['events_processed'],1)
            self.assertFalse((output/'failure.json').exists())


if __name__=='__main__':
    unittest.main()
