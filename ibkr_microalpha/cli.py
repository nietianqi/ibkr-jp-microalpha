import argparse
import json
import traceback
from pathlib import Path

from .config import build_engine, load_config
from .replay import Replay, encode


def _replay_file(config_path, events, output):
    """Stream raw input and audit into the output directory; write report only on success."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for stale in ('report.json', 'failure.json'):
        (output / stale).unlink(missing_ok=True)
    engine = build_engine(load_config(config_path))
    with (output / 'raw-input.jsonl').open('w', encoding='utf-8') as raw, \
            (output / 'audit.jsonl').open('w', encoding='utf-8') as audit:
        runner = Replay(engine, raw_sink=raw, audit_sink=audit)
        try:
            report = runner.run(events)
        except Exception as error:
            failure = {'status': 'FAILED', 'source': str(events), 'error': str(error),
                       'error_type': type(error).__name__,
                       'validated_events': runner.events_processed,
                       'runner_poisoned': runner.failed,
                       'manifest': runner.manifest(),
                       'traceback': traceback.format_exc(limit=5)}
            (output / 'failure.json').write_text(json.dumps(failure, indent=2, ensure_ascii=False),
                                                 encoding='utf-8')
            raise
        result = runner.save(output, report=report)
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
            events, runner = create_demo(args.config, args.events)
            report = runner.report()
            result = runner.save(args.output, report=report)
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
