import argparse
import json
import os
import shutil
import tempfile
import traceback
import uuid
from pathlib import Path

from .config import build_engine, load_config
from .replay import RUN_FILES, Replay, encode, publish_run


def _protect_sources(sources, output, extra_targets=()):
    """Check resolved paths and filesystem identities before any output write."""
    output = Path(output).resolve()
    targets = [output/name for name in (*RUN_FILES, 'failure.json')]
    targets.extend(Path(target).resolve() for target in extra_targets)
    for raw_source in sources:
        source = Path(raw_source).resolve()
        for target in targets:
            if source == target.resolve() or (source.exists() and target.exists() and source.samefile(target)):
                raise ValueError(f'input/output path conflict: {source} and {target}')
    return output


def _record_failure(staging, output, runner, events, error):
    """Keep an older successful run intact; archive a failed attempt separately."""
    output.mkdir(parents=True, exist_ok=True)
    destination = output if not any(output.iterdir()) else output/'.failed'/uuid.uuid4().hex
    destination.mkdir(parents=True, exist_ok=True)
    (staging/'report.json').unlink(missing_ok=True)
    for name in RUN_FILES:
        if name != 'report.json' and (staging/name).is_file():
            shutil.copy2(staging/name, destination/name)
    failure = {'status': 'FAILED', 'source': str(events), 'error': str(error),
               'error_type': type(error).__name__, 'validated_events': runner.events_processed,
               'applied_events': runner.events_applied, 'runner_poisoned': runner.failed,
               'manifest': runner.manifest(), 'traceback': traceback.format_exc(limit=5)}
    temporary = destination/'.failure.tmp'
    temporary.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, destination/'failure.json')


def _replay_file(config_path, events, output):
    """Stage closed evidence, then publish success with rollback of any older run."""
    output = _protect_sources((config_path, events), output)
    engine = build_engine(load_config(config_path))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{output.name}-replay-', dir=output.parent) as directory:
        staging = Path(directory)
        runner = Replay(engine)
        try:
            with (staging/'raw-input.jsonl').open('w', encoding='utf-8') as raw, \
                    (staging/'audit.jsonl').open('w', encoding='utf-8') as audit:
                runner.raw_sink, runner.audit_sink = raw, audit
                report = runner.run(events)
            # Context exits above must succeed before any report is written.
            runner.save(staging, report=report)
            # Check again at commit in case a destination acquired a source link.
            _protect_sources((config_path, events), output)
            result = publish_run(staging, output)
        except Exception as error:
            runner.mark_failed()
            try:
                _record_failure(staging, output, runner, events, error)
            except OSError as evidence_error:
                error.add_note(f'failure artifact could not be recorded: {evidence_error}')
            raise
    return report, result


def main(argv=None):
    parser = argparse.ArgumentParser(description='IBKR JP multi-horizon offline research core')
    sub = parser.add_subparsers(dest='command', required=True)
    validate = sub.add_parser('validate-config')
    validate.add_argument('config')
    replay = sub.add_parser('replay')
    replay.add_argument('events')
    replay.add_argument('--config', required=True)
    replay.add_argument('--output', default='runs/latest')
    demo = sub.add_parser('demo')
    demo.add_argument('--config', default='examples/research.json')
    demo.add_argument('--events', default='runs/demo/events.jsonl')
    demo.add_argument('--output', default='runs/demo')
    demo.add_argument('--verify-replay', action='store_true',
                      help='replay the generated file with a fresh engine and require an identical report')
    args = parser.parse_args(argv)
    try:
        build_engine(load_config(args.config))
        if args.command == 'validate-config':
            print('Configuration valid. Mode: offline replay; live trading unavailable.')
            return 0
        if args.command == 'demo':
            from .demo import create_demo
            _protect_sources((args.config,), args.output, (args.events,))
            _protect_sources((args.events,), args.output)
            events, runner = create_demo(args.config, args.events)
            report = runner.report()
            output = _protect_sources((args.config, events), args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f'.{output.name}-demo-', dir=output.parent) as directory:
                staging = Path(directory)
                try:
                    runner.save(staging, report=report)
                    _protect_sources((args.config, events), output)
                    result = publish_run(staging, output)
                except Exception as error:
                    runner.mark_failed()
                    try:
                        _record_failure(staging, output, runner, events, error)
                    except OSError as evidence_error:
                        error.add_note(f'failure artifact could not be recorded: {evidence_error}')
                    raise
            if args.verify_replay:
                replayed, _ = _replay_file(args.config, events, Path(args.output) / 'verify')
                comparable = lambda r: {k: v for k, v in r.items() if k not in ('metrics', 'manifest')}
                if comparable(replayed) != comparable(report):
                    parser.exit(3, 'error: replay of the generated input produced a different report\n')
                print('Replay verification: identical report from the generated input.')
        else:
            report, result = _replay_file(args.config, args.events, args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=encode))
        print(f'Report saved: {result}')
        return 0
    except (ValueError, KeyError, TypeError, OSError, ArithmeticError, RuntimeError) as error:
        parser.exit(2, f'error: {error}\n')


if __name__ == '__main__':
    main()
