"""Reproduce the research contract with explicit unit fixtures, never edge evidence."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from tests.test_round3_research import NOW, recorded_intent
from ibkr_microalpha.economics import CalibrationTable
from ibkr_microalpha.research import build_calibration_rows, calibration_table_event, replay_intent_labels


def main():
    labels, branches = [], []
    for offset, filled in enumerate((0, 40, 100)):
        runner = recorded_intent(start=NOW+timedelta(days=offset), filled=filled)
        label = replay_intent_labels(runner, verified_manifest=runner.manifest())[0]
        labels.append(label)
        branches.append(dict(day=label.day.isoformat(), target=label.quantity, bought=label.bought,
                             sold=label.sold, final_fees=str(label.actual_fees), net=str(label.net_amount),
                             max_chase_ticks=label.max_chase_ticks, source=label.label_source,
                             manifest=runner.manifest()))
    c = runner.engine.config
    args = dict(policy_id=c.policy_id, version=c.model_version, holding_seconds=c.holding_seconds,
                quantity=100, max_chase_ticks=10, min_days=2, min_samples=2, draws=100)
    rows = build_calibration_rows(labels, [0], **args)
    known = NOW+timedelta(days=4)
    table = CalibrationTable(rows, known_at=known, version='unit-closed-loop')
    table.validate_for_profile('research', min_independent_days=2, policy_hash=labels[0].policy_hash,
                               fee_version=labels[0].fee_version, as_of=known)
    errors = {}
    for name, operation in (
            ('changed_chase_cap', lambda: build_calibration_rows(labels, [0], **{**args,'max_chase_ticks':11})),
            ('single_day', lambda: build_calibration_rows(labels[:1], [0], **{**args,'min_days':1})),
            ('tampered_cap_row', lambda: CalibrationTable([replace(rows[0],max_chase_ticks=11)],
                known_at=known, version='invalid').validate_for_profile('research'))):
        try:
            operation()
        except ValueError as error:
            errors[name] = str(error)
    missing = recorded_intent(final_fees=False)
    try:
        replay_intent_labels(missing, verified_manifest=missing.manifest())
    except ValueError as error:
        errors['missing_final_fee'] = str(error)
    result = dict(note='SYNTHETIC UNIT FIXTURES: verified Replay contract only; no real-data profitability evidence.',
                  branches=branches, row_event=calibration_table_event(rows, version='unit-closed-loop',known_at=known),
                  min_20_days_result=build_calibration_rows(labels,[0],**{**args,'min_days':20}),
                  rejected=errors)
    destination = Path(__file__).with_name('closed-loop-results.json')
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(branches=len(branches),rows=len(rows),rejected=errors,output=str(destination)),ensure_ascii=False))


if __name__ == '__main__':
    main()
