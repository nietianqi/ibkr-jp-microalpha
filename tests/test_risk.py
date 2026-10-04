import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from ibkr_microalpha.risk import AccountSnapshot, PortfolioRisk, RiskConfig

T = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)


def config(**changes):
    values = dict(capital=D('1000000'), trade_risk_fraction=D('.001'),
                  symbol_fraction=D('.1'), portfolio_fraction=D('.3'),
                  daily_loss_fraction=D('.005'), sector_fraction=D('.2'),
                  max_positions=3, max_entry_intents_per_day=100, max_quantity=1000)
    return RiskConfig(**(values | changes))


class RiskTests(unittest.TestCase):
    def ready(self, **changes):
        risk = PortfolioRisk(config(**changes))
        risk.verify_account(D('1000000'), {}, {}, True)
        return risk

    def allocate(self, risk, symbol='A', **kwargs):
        return risk.allocate(symbol, symbol, 'sector', D('1000'), D('2'), D('1'),
                             1000, 1000, kwargs.get('fees', lambda q, p: (D(80), D(80))))

    def test_T12_minimum_lot_cannot_fit(self):
        risk = self.ready(capital=D('100000'))
        result = self.allocate(risk)
        self.assertFalse(result.allowed)
        self.assertEqual(risk.reservations, {})

    def test_commission_reserve_can_block_apparently_affordable_lot(self):
        risk = self.ready()
        risk.verify_account(D('100000'), {}, {}, True)
        self.assertFalse(self.allocate(risk).allowed)

    def test_T15_atomic_shared_sector_reservations(self):
        risk = self.ready()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: self.allocate(risk, str(i)), range(8)))
        self.assertEqual(sum(r.allowed for r in results), 2)
        self.assertEqual(sum(r.notional for r in risk.reservations.values()), D('200000'))

    def test_T10_daily_lock_cannot_be_overridden_by_profit(self):
        risk = self.ready()
        risk.observe_daily_pnl(D('-5000'), T)
        risk.observe_daily_pnl(D('10000'), T)
        self.assertFalse(self.allocate(risk).allowed)
        with self.assertRaises(ValueError):
            risk.manual_unlock(account_reconciled=True, data_healthy=True, risk_approved=True)

    def test_unknown_loss_is_not_zero_but_soft_until_escalated(self):
        risk = self.ready()
        risk.observe_daily_pnl(None, T)
        self.assertFalse(risk.locked)
        self.assertTrue(risk.entries_blocked)
        self.assertEqual(self.allocate(risk).reason, 'entries_blocked')
        self.assertEqual(risk.escalate(T + timedelta(seconds=30), 30), [])
        self.assertEqual(risk.escalate(T + timedelta(seconds=31), 30), ['escalated:unvalued_daily_pnl'])
        self.assertTrue(risk.locked)
        risk.observe_daily_pnl(D('0'), T + timedelta(seconds=32))
        self.assertTrue(risk.locked)  # HARD survives recovery until an operator unlocks.

    def test_valuation_recovery_clears_soft_block(self):
        risk = self.ready()
        risk.observe_daily_pnl(None, T)
        risk.observe_daily_pnl(D('-1'), T + timedelta(seconds=1))
        self.assertFalse(risk.entries_blocked)
        self.assertTrue(self.allocate(risk).allowed)

    def test_portfolio_stress_budget_limits_total_open_stress(self):
        risk = self.ready(portfolio_stress_fraction=D('.0005'), symbol_fraction=D('.5'),
                          sector_fraction=D('.5'))
        risk.verify_account(D('1000000'), {}, {}, True, stress={'HELD': D('300')})
        # 100 shares stress 460 JPY; 300 already held leaves 200 of the 500 budget.
        self.assertEqual(self.allocate(risk).reason, 'less_than_one_legal_lot')
        risk.verify_account(D('1000000'), {}, {}, True, stress={})
        self.assertTrue(self.allocate(risk).allowed)

    def test_derived_default_stress_budget_is_finite(self):
        self.assertEqual(config().portfolio_stress_fraction, D('.003'))

    def test_entry_intent_limit_counts_only_intents(self):
        risk = self.ready(max_entry_intents_per_day=1)
        risk.note_entry_intent()
        self.assertEqual(self.allocate(risk).reason, 'entry_intent_limit')

    def test_account_snapshot_requires_jpy_and_nonnegative_funds(self):
        AccountSnapshot('U1', 'JPY', D('1'), D('1'), T, 'broker')
        for currency, funds in (('USD', D('1')), ('JPY', D('-1'))):
            with self.assertRaises(ValueError):
                AccountSnapshot('U1', currency, funds, D('1'), T, 'broker')

    def test_external_positions_consume_portfolio_and_sector_budget(self):
        risk = self.ready()
        risk.verify_account(D('800000'), {'MANUAL': D('200000')}, {'MANUAL': 'sector'}, True)
        self.assertFalse(self.allocate(risk).allowed)

    def test_minimum_fee_size_recalculated_and_no_addition(self):
        risk = self.ready(symbol_fraction=D('.5'), sector_fraction=D('.5'))
        risk.verify_account(D('1000000'), {}, {}, True)
        result = self.allocate(risk)
        self.assertEqual(result.reservation.quantity, 200)
        self.assertEqual(result.reservation.stress_loss, D('760'))
        self.assertFalse(risk.allocate('second', 'A', 'sector', D('1000'), D(2), D(1),
                                       1000, 1000, lambda q,p:(D(80),D(80))).allowed)

    def test_bad_parameters_rejected(self):
        for value in (D('NaN'), D('Infinity'), D('0')):
            with self.assertRaises(ValueError):
                config(capital=value)


if __name__ == '__main__':
    unittest.main()
