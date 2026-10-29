import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from tempfile import TemporaryDirectory

from tests.test_round4_research import T,q,FEES
from ibkr_microalpha.research import (DecisionRecord,SimulationPolicy,scan_holding_periods,
    ExperimentRegistry,ResearchObservation,run_walk_forward,simulate_intent)


class HoldingResearchTests(unittest.TestCase):
    def scan(self, quotes, decisions, **settings):
        return scan_holding_periods(quotes,decisions,commissions=FEES,
            policy=SimulationPolicy(holding_seconds=2,participation_rate=D(1),
                                    submit_latency_seconds=0,ack_latency_seconds=0),
            holding_periods=(2,),fee_multipliers=(1,),slippage_ticks=(1,),
            min_days=2,min_samples=2,draws=400,**settings)

    def test_unknown_paths_are_not_dropped_from_all_intent_inference(self):
        quotes=[q(0),q(1)]
        report=self.scan(quotes,[DecisionRecord('d','S',T,1,100)])
        row=report['rows'][0]
        self.assertEqual(row['status'],'INCOMPLETE_COVERAGE')
        self.assertIsNone(row['all_intents_mean'])
        self.assertEqual(row['unknown_intents'],['d'])

    def test_complete_negative_paths_have_explicit_simulated_non_deployable_identity(self):
        quotes=[]
        decisions=[]
        for offset in (0,1):
            shift=timedelta(days=offset)
            for second in range(8):
                quote=q(second)
                quotes.append(replace(quote,at=quote.at+shift,bid_at=quote.bid_at+shift,
                                      ask_at=quote.ask_at+shift,event_id=f'{offset}:{second}'))
            decisions.append(DecisionRecord(str(offset),'S',T+shift,1,100))
        report=self.scan(quotes,decisions)
        self.assertFalse(report['deployable'])
        self.assertEqual(report['execution_source'],'SIMULATED')
        self.assertEqual(report['rows'][0]['status'],'NEGATIVE_EXPECTATION_EVIDENCE')
        self.assertLess(D(report['rows'][0]['upper']),0)

    def test_repeated_unchanged_book_does_not_replenish_visible_liquidity(self):
        policy=SimulationPolicy(holding_seconds=2,participation_rate=D(1))
        result=simulate_intent([q(i,size=40) for i in range(10)],DecisionRecord('d','S',T,1,100),
                               policy=policy,commissions=FEES)
        self.assertEqual(result.bought,40)

    def test_missing_symbol_is_unknown_and_volatility_requires_past_observation(self):
        report=self.scan([], [DecisionRecord('d','MISSING',T,1,100)])
        self.assertEqual(report['rows'][0]['status'],'INCOMPLETE_COVERAGE')
        result=simulate_intent([q(i) for i in range(10)],DecisionRecord('d','S',T,1,100),
            policy=SimulationPolicy(stop_volatility_multiple=2),commissions=FEES)
        self.assertEqual(result.status,'UNKNOWN')


class WalkForwardTests(unittest.TestCase):
    def observations(self):
        return [ResearchObservation(str(day),T+timedelta(days=day),T+timedelta(days=day,seconds=5),
                    {'x':float(day%3)},{'a':'10','b':'-10'}) for day in range(16)]

    def run_experiment(self,directory,observations,**kwargs):
        return run_walk_forward(observations,[{'variant_id':name,'weights':{'x':1.0},
                    'score_threshold':-100.0} for name in ('a','b')],epsilon={'x':.1},
                    registry=ExperimentRegistry(directory),dataset_id='UNIT_ONLY',train=4,validate=2,
                    test=2,holdout=3,embargo=1,draws=400,**kwargs)

    def test_outer_test_and_holdout_cannot_choose_the_winning_policy(self):
        with TemporaryDirectory() as directory:
            rows=self.observations()
            report=self.run_experiment(directory,rows)
            self.assertEqual(report['final_holdout']['status'],'SEALED_NOT_READ')
            self.assertTrue(all(f['selected_policy']=='a' for f in report['folds']))
            changed=[replace(o,outcomes={'a':'-99999','b':'99999'}) if index>=13 else o
                     for index,o in enumerate(rows)]
            second=self.run_experiment(directory,changed)
            self.assertEqual([f['selected_policy'] for f in report['folds']],
                             [f['selected_policy'] for f in second['folds']])

    def test_holdout_claim_is_one_use_across_reruns_and_trial_ids(self):
        with TemporaryDirectory() as directory:
            report=self.run_experiment(directory,self.observations(),use_final_holdout=True)
            self.assertEqual(report['final_holdout']['status'],'CONSUMED_ONCE')
            with self.assertRaises(FileExistsError):
                self.run_experiment(directory,self.observations(),use_final_holdout=True)

    def test_future_training_label_is_rejected_before_selection(self):
        rows=self.observations()
        rows[0]=replace(rows[0],completed_at=T+timedelta(days=100))
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'unavailable'):
                self.run_experiment(directory,rows)


if __name__=='__main__':unittest.main()
