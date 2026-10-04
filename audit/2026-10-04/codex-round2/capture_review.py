"""Freeze the source reviewed in round 2 and validate retained probe evidence."""
import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent


def read(relative):
    return json.loads((OUT/relative).read_text(encoding='utf-8'))


execution = read('execution/evidence.json')
entry = read('entry-evidence.json')
strategy = read('strategy/results.json')
collision = read('performance/source-collision.json')
persistence = read('performance/persistence-failure.json')
intent = read('performance/reopened-intent.json')
demo = read('demo/report.json')
verified = read('demo/verify/report.json')
checks = {
    'EXE-09': execution['snapshot_funds_reused']['second_allocation_allowed'],
    'EXE-10': execution['queued_entry_ignores_new_funds']['commands'][0]['kind'] == 'SUBMIT',
    'EXE-11': execution['late_sell_after_cover_cancel_crashes_valuation']['position_after_late_fill'] == -50,
    'EXE-12': execution['request_budget_lost_on_restore']['restored']['snapshot']['budget'] is None,
    'RPL-03': collision['after_bytes'] == 0 and collision['success_report_created'],
    'RPL-04': not persistence['failed'] and persistence['later_event_accepted'],
    'RPT-06': intent['position_final'] == 0 and all(i['status'] == 'OPEN' for i in intent['intents_final']),
    'FLOW-05': entry['temporary_price_cap']['candidate_after_same_time_poll'] is None,
    'FLOW-06': entry['quantity_specific_cap']['actual_plan_cap'] == '3011',
    'STR-09': strategy['labels']['partial_entry_label'] == '0',
    'STR-10': strategy['independent_day_gate']['zero_days_row_allowed'],
    'DATA-09': strategy['new_grid_tail_omission']['volatility_bps'] == 0,
    'demo_business_equal': {k:v for k,v in demo.items() if k not in ('metrics','manifest')} ==
                           {k:v for k,v in verified.items() if k not in ('metrics','manifest')},
    'demo_input_hash_equal': demo['manifest']['input_sha256'] == verified['manifest']['input_sha256'],
}
assert all(checks.values()), checks
files = sorted(set(ROOT.glob('ibkr_microalpha/**/*.py')) | set(ROOT.glob('tests/**/*.py')) |
               set(ROOT.glob('examples/*.json')) | {ROOT/'pyproject.toml', ROOT/'README.md',
                                                   ROOT/'IMPLEMENTATION_STATUS.md'})
manifest = {'captured_at_utc': datetime.now(timezone.utc).isoformat(),
            'git_commit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
            'python': platform.python_version(), 'platform': platform.platform(),
            'baseline_note': 'Single initial-import commit includes Claude fixes; this is a current-source re-review, not a historical git diff.',
            'files': [{'path':p.relative_to(ROOT).as_posix(), 'bytes':p.stat().st_size,
                       'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files],
            'demo_manifest': demo['manifest'], 'evidence_assertions': checks,
            'tests_observed': {'command':'python -m unittest discover -s tests',
                               'count':195, 'exit_code':0, 'reported_seconds':0.476,
                               'source':'Independent root tool run in this review; source was unchanged afterwards.'}}
(OUT/'review-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'source_files':len(files), 'all_evidence_assertions':True,
                  'code_sha256':demo['manifest']['code_sha256'], 'events':demo['events_processed']}, indent=2))
