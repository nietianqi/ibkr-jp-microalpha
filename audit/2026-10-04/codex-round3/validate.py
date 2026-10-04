"""Reproducible local acceptance; never connects to a broker."""
import compileall
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from collections import Counter
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ibkr_microalpha.replay import code_hash


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def command(arguments, log):
    with (OUTPUT / log).open('w', encoding='utf-8') as stream:
        result = subprocess.run([sys.executable, *arguments], cwd=ROOT,
                                stdout=stream, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'acceptance command failed; see {log}')


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    counts = Counter(case.id().split('.')[0] for case in cases(suite))
    with (OUTPUT / 'test-results.txt').open('w', encoding='utf-8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    summary = {'at': datetime.now(timezone.utc).isoformat(), 'python': sys.version,
               'tests_run': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped),
               'tests_by_module': dict(sorted(counts.items())),
               'new_round3_tests': sum(count for name, count in counts.items() if name.startswith('test_round3_')),
               'successful': result.wasSuccessful(), 'live_readiness': 'NOT_VERIFIED'}
    if not result.wasSuccessful():
        (OUTPUT / 'regression-results.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        raise RuntimeError('regressions failed; see test-results.txt')
    if not compileall.compile_dir(str(ROOT / 'ibkr_microalpha'), quiet=1):
        raise RuntimeError('source compilation failed')
    if not compileall.compile_dir(str(ROOT / 'tests'), quiet=1):
        raise RuntimeError('test compilation failed')
    command(['-m', 'ibkr_microalpha', 'validate-config', 'examples/research.json'], 'config-validation.log')
    command(['-m', 'ibkr_microalpha', 'demo', '--events', str(OUTPUT / 'demo/events.jsonl'),
             '--output', str(OUTPUT / 'demo'), '--verify-replay'], 'demo-validation.log')
    demo = json.loads((OUTPUT / 'demo/report.json').read_text(encoding='utf-8'))
    verify = json.loads((OUTPUT / 'demo/verify/report.json').read_text(encoding='utf-8'))
    business = lambda report: {k: v for k, v in report.items() if k not in ('metrics', 'manifest')}
    assert business(demo) == business(verify)
    assert demo['events_processed'] == 3797 and demo['funnel']['intents'] == 1
    assert demo['active_orders'] == 0 and all(p['quantity'] == 0 for p in demo['positions'].values())
    assert demo['intents'][0]['status'] == 'CLOSED'
    assert demo['manifest']['code_sha256'] == code_hash() == verify['manifest']['code_sha256']
    summary.update(compileall='PASSED', config='PASSED',
                   demo={'events': demo['events_processed'], 'funnel': demo['funnel'],
                         'active_orders': demo['active_orders'], 'intents': demo['intents'],
                         'daily_net_pnl_estimate': demo['daily_net_pnl_estimate'],
                         'identical_business_report': True, 'metrics': verify['metrics']},
                   fixes={key: 'IMPLEMENTED_LOCAL_REGRESSIONS_PASS_PENDING_CLAUDE_REVIEW' for key in
                          ('EXE-09', 'EXE-10', 'EXE-11', 'EXE-12', 'RPL-03', 'RPL-04', 'RPT-06',
                           'FLOW-05', 'FLOW-06', 'STR-09', 'STR-10', 'DATA-09')})
    files = [*sorted((ROOT / 'ibkr_microalpha').rglob('*.py')),
             *sorted((ROOT / 'tests').glob('*.py')), ROOT / 'examples/research.json']
    manifest = {'at': summary['at'],
                'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'code_sha256': code_hash(), 'demo_manifest': demo['manifest'],
                'files': {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in files},
                'scope': 'Final source, existing/new tests and example configuration; round2 preserved.'}
    (OUTPUT / 'source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (OUTPUT / 'regression-results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'tests_run': result.testsRun, 'new_round3_tests': summary['new_round3_tests'],
                      'successful': True, 'demo_events': demo['events_processed'],
                      'demo_intents': demo['funnel']['intents'], 'identical_business_report': True}))


if __name__ == '__main__':
    main()
