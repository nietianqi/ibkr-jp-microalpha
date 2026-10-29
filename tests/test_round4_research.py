"""Research contracts: no duplicate costs or promotion of simulated evidence."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

from ibkr_microalpha.domain import Quote
from ibkr_microalpha.economics import Prediction, CommissionSchedule, adjust_prediction_for_price, prediction_gate
from ibkr_microalpha.market import JST

T = datetime(2026, 10, 2, 10, tzinfo=JST)
FEES = CommissionSchedule(D('0'), D('1'), D('0'), 'unit-fees')


def q(second, bid='100', ask='100.1', size=100, **kwargs):
    at = T+timedelta(seconds=second)
    return Quote('S',at,D(bid),D(ask),size,size,f'q{second}',bid_at=at,ask_at=at,**kwargs)


def raw_prediction(filled=100):
    return Prediction('p','v',100,40,D(100),D(50),True,True,120,
        sample_days=20,label_source='ARTIFICIAL',provenance={'source':'ARTIFICIAL_FIXTURE'},
        value_basis='REPLAY_POLICY_NET',entry_cost_basis={'version':'ACTUAL_FILLED_PATHS_V1',
            'paths':[{'children':([] if not filled else [{'quantity':filled,
                'reference_price':'100','actual_notional':str(D('100.2')*filled)}])}]})


class Round4Economics(unittest.TestCase):
    def test_same_actual_fill_is_not_repriced_twice(self):
        p=raw_prediction()
        self.assertEqual(adjust_prediction_for_price(p,reference_price=D(100),limit_price=D('100.2'),
                         commissions=FEES).lower_net_amount,p.lower_net_amount)

    def test_partial_only_budgets_actual_filled_quantity(self):
        p=raw_prediction(40)
        adjusted=adjust_prediction_for_price(p,reference_price=D(100),limit_price=D('100.3'),commissions=FEES)
        self.assertEqual(adjusted.lower_net_amount,p.lower_net_amount-D(4))
        self.assertEqual(adjusted.mean_net_amount,p.mean_net_amount-D(4))

    def test_unfilled_and_improved_price_cannot_manufacture_value(self):
        for filled in (0,40,100):
            p=raw_prediction(filled)
            adjusted=adjust_prediction_for_price(p,reference_price=D(100),limit_price=D(99),commissions=FEES)
            self.assertEqual(adjusted,p)

    def test_unknown_raw_cost_basis_fails_closed(self):
        with self.assertRaises(ValueError):
            adjust_prediction_for_price(replace(raw_prediction(),entry_cost_basis=None),
                reference_price=D(100),limit_price=D('100.2'),commissions=FEES)

    def test_formal_factory_row_preserves_the_same_actual_path_cost(self):
        from tests.test_round3_research import recorded_intent,NOW
        from ibkr_microalpha.research import replay_intent_labels,build_calibration_rows
        labels=[]
        for day in (0,1):
            runner=recorded_intent(start=NOW+timedelta(days=day))
            labels.extend(replay_intent_labels(runner,verified_manifest=runner.manifest()))
        c=runner.engine.config
        row=build_calibration_rows(labels,[0],policy_id=c.policy_id,version=c.model_version,
            holding_seconds=c.holding_seconds,quantity=100,max_chase_ticks=10,min_days=2,min_samples=2,draws=20)[0]
        adjusted=adjust_prediction_for_price(row.prediction(),reference_price=D('960.1'),
                            limit_price=D('960.3'),commissions=runner.engine.commissions)
        self.assertEqual(row.mean_net_amount,D('-1070.00'))
        self.assertEqual(adjusted.mean_net_amount,row.mean_net_amount)

    def test_simulated_callback_cannot_be_promoted_to_verified_broker_labels(self):
        import json
        from tests.test_round3_research import recorded_intent
        from ibkr_microalpha.config import build_engine
        from ibkr_microalpha.replay import Replay
        from ibkr_microalpha.research import replay_intent_labels
        original=recorded_intent()
        rebuilt=Replay(build_engine(original.engine.frozen_config))
        for line in original.raw_lines:
            event=json.loads(line)
            if event['type']=='fill':event['execution_source']='SIMULATED'
            rebuilt.dispatch(event)
        rebuilt.finish()
        labels=replay_intent_labels(rebuilt,verified_manifest=rebuilt.manifest())
        self.assertEqual(labels[0].label_source,'SIMULATED')


class Round4Simulation(unittest.TestCase):
    def api(self):
        from ibkr_microalpha.research import SimulationPolicy, DecisionRecord, simulate_intent
        return SimulationPolicy, DecisionRecord, simulate_intent

    def simulate(self,quotes,**settings):
        Policy,Decision,simulate=self.api()
        policy=Policy(holding_seconds=2,submit_latency_seconds=1,ack_latency_seconds=1,
            cancel_latency_seconds=1,entry_ttl_seconds=2,exit_ttl_seconds=2,
            participation_rate=D('1'),**settings)
        return simulate(quotes,Decision('d','S',T,1.0,100),policy=policy,commissions=FEES)

    def test_fill_uses_arrival_quote_and_explicit_simulated_source(self):
        result=self.simulate([q(0),q(.5,'90','90.1'),q(1),q(2),q(3),q(4),q(5),q(6)])
        self.assertEqual(result.execution_source,'SIMULATED')
        self.assertTrue(all(fill.at >= T+timedelta(seconds=1) for fill in result.fills))
        self.assertEqual(result.bought,100)

    def test_cancel_latency_keeps_order_live_after_ttl(self):
        result=self.simulate([q(0),q(1,'101','101.1'),q(2.5),q(3),q(5),q(6),q(7)])
        self.assertEqual(result.bought,100)
        self.assertEqual(result.fills[0].at,T+timedelta(seconds=2.5))

    def test_passive_touch_is_not_queue_fill(self):
        result=self.simulate([q(0),q(1,'99.9','100'),q(2,'99.9','100'),q(4)],passive_entry=True)
        self.assertEqual(result.status,'UNFILLED')
        self.assertEqual(result.bought,0)

    def test_partial_residual_and_missing_coverage_are_unknown(self):
        result=self.simulate([q(0),q(1,size=40),q(2,'101','101.1'),q(4,'90','90.1',size=10)])
        self.assertEqual(result.status,'UNKNOWN')
        self.assertIsNone(result.net_amount)
        self.assertEqual(result.bought,40)

    def test_no_duplicate_quote_liquidity(self):
        Policy,Decision,simulate=self.api()
        from ibkr_microalpha.research.simulation import LiquidityBudget
        budget=LiquidityBudget()
        quotes=[q(0),q(1,size=40),q(4),q(5),q(6),q(7)]
        policy=Policy(holding_seconds=1,participation_rate=D(1))
        first=simulate(quotes,Decision('a','S',T,1,100),policy=policy,commissions=FEES,liquidity=budget)
        second=simulate(quotes,Decision('b','S',T,1,100),policy=policy,commissions=FEES,liquidity=budget)
        self.assertLessEqual(first.bought+second.bought,40)


if __name__=='__main__':
    unittest.main()
