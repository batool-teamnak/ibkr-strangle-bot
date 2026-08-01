import contextlib
import io
import math
import unittest
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import bot

class TradingTests(unittest.TestCase):
    def setUp(self):
        self.ib = MagicMock()
        def qualify(*contracts):
            for index, contract in enumerate(contracts, start=1):
                if not contract.conId:
                    contract.conId = index
            return list(contracts)
        self.ib.qualifyContracts.side_effect = qualify
        self.quote = SimpleNamespace(ask=0.5, bid=0.4, last=0.5,
            impliedVolatility=0.3, marketPrice=lambda:100.0, midpoint=lambda:0.5)
        self.ib.reqMktData.return_value = self.quote
        self.ib.reqHistoricalData.return_value = [SimpleNamespace(close=0.3)] * 20
        self.trade = SimpleNamespace(orderStatus=SimpleNamespace(status='Submitted', filled=0),
                                     isDone=lambda:False)
        self.ib.placeOrder.return_value = self.trade
        with patch.object(bot, 'IB', return_value=self.ib), contextlib.redirect_stdout(io.StringIO()):
            self.trader = bot.HybridStrangleTrader()

    def quiet(self, fn, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args)


    def test_session_times_construct(self):
        self.assertEqual(self.trader.sell_time, time(15,45))
        self.assertEqual(self.trader.buy_time, time(9,35))

    def test_connection_failure_and_injected_client(self):
        broken = MagicMock()
        broken.connect.side_effect = OSError('offline')
        with self.assertRaises(ConnectionError):
            bot.HybridStrangleTrader(ib=broken)
        broken.disconnect.assert_called_once()
        with contextlib.redirect_stdout(io.StringIO()):
            bot.HybridStrangleTrader(ib=broken, connect=False)
        self.assertEqual(broken.connect.call_count, 1)

    def test_invalid_spot_blocks_orders(self):
        for price in (None, 0, -1, float('nan'), float('inf')):
            self.quote.marketPrice = lambda p=price:p
            self.quiet(self.trader.sell_strangle, 'F')
        self.ib.placeOrder.assert_not_called()

    def test_invalid_premiums_block_orders(self):
        for premium in (None, -1, float('nan'), float('inf')):
            self.quote.ask = premium
            self.quiet(self.trader.sell_strangle, 'F')
        self.ib.placeOrder.assert_not_called()

    def test_invalid_volatility_blocks_entry(self):
        for volatility in (None, float('nan'), float('inf'), 0):
            self.quote.impliedVolatility = volatility
            self.assertIsNone(self.quiet(self.trader.get_iv_rank, 'F'))
            self.quiet(self.trader.sell_strangle, 'F')
        self.ib.placeOrder.assert_not_called()
        self.quote.impliedVolatility = .3
        self.ib.reqHistoricalData.return_value = [SimpleNamespace(close=0)]
        self.assertIsNone(self.quiet(self.trader.get_iv_rank, 'F'))

    def test_unqualified_options_block_entry(self):
        self.ib.qualifyContracts.side_effect = None
        self.ib.qualifyContracts.return_value = []
        self.quiet(self.trader.sell_strangle, 'F')
        self.ib.placeOrder.assert_not_called()

    def test_position_limit_checked_per_entry(self):
        self.trader.max_strangles = 1
        self.quiet(self.trader.sell_strangle, 'F')
        self.assertEqual(len(self.trader.current_positions), 1)
        self.quiet(self.trader.sell_strangle, 'AMC')
        self.assertEqual(self.ib.placeOrder.call_count, 2)

    def test_duplicate_symbol_entry_is_blocked(self):
        self.quiet(self.trader.sell_strangle, 'F')
        self.quiet(self.trader.sell_strangle, 'F')
        self.assertEqual(self.ib.placeOrder.call_count, 2)

    def test_snapshot_subscription_cleanup(self):
        contract = bot.Stock('F','SMART','USD')
        self.assertIs(self.trader.market_snapshot(contract), self.quote)
        self.ib.cancelMktData.assert_called_once_with(contract)
        self.ib.sleep.side_effect = RuntimeError('poll failure')
        with self.assertRaises(RuntimeError):
            self.trader.market_snapshot(contract)
        self.assertEqual(self.ib.cancelMktData.call_count, 2)

    def test_exit_quote_fallback(self):
        self.quote.midpoint = lambda:float('nan')
        self.quote.last = .7
        self.assertEqual(self.trader.exit_price(self.quote), .7)
        self.quote.last = float('inf')
        self.assertIsNone(self.trader.exit_price(self.quote))

    def test_midrange_volatility_has_profit_exit(self):
        self.trader.current_positions[(1,2)] = (datetime.now(), 2.0, 40)
        self.quiet(self.trader.manage_positions)
        self.assertEqual(self.ib.placeOrder.call_count, 2)

    def test_run_waits_use_broker_event_loop(self):
        import inspect
        source = inspect.getsource(self.trader.run)
        self.assertIn('self.ib.sleep(60)', source)
        self.assertNotIn('time.sleep', source)

    def test_market_clock_uses_eastern_time(self):
        self.assertEqual(str(bot.market_now().tzinfo), 'America/New_York')
        with patch.object(bot, 'market_now', return_value=datetime(2026,7,3,16,tzinfo=bot.ET)):
            self.assertEqual(self.trader.next_expiry(), '20260710')
            self.trader.earnings_dates = {'F':['2026-07-04']}
            self.assertTrue(self.trader.is_earnings_soon('F'))

    def test_bad_entry_credit_does_not_submit_exit(self):
        for credit in (0, -1, float('nan'), float('inf')):
            self.trader.current_positions = {(1,2):(datetime.now(),credit,40)}
            self.quiet(self.trader.manage_positions)
        self.ib.placeOrder.assert_not_called()

    def test_unfilled_close_is_retained_without_duplicate_orders(self):
        self.trader.current_positions[(1,2)] = (datetime.now(),2.0,40)
        self.quiet(self.trader.manage_positions)
        self.assertIn((1,2), self.trader.current_positions)
        self.quiet(self.trader.manage_positions)
        self.assertEqual(self.ib.placeOrder.call_count, 2)
        self.trade.orderStatus.status = 'Filled'
        self.trade.orderStatus.filled = 1
        self.quiet(self.trader.manage_positions)
        self.assertNotIn((1,2), self.trader.current_positions)

    def test_second_exit_submission_failure_can_recover(self):
        self.trader.current_positions[(1,2)] = (datetime.now(),2.0,40)
        self.ib.placeOrder.side_effect = [self.trade, RuntimeError('lost connection'), self.trade]
        self.quiet(self.trader.manage_positions)
        self.assertEqual(len(self.trader.closing_orders[(1,2)]), 1)
        self.quiet(self.trader.manage_positions)
        self.assertEqual(len(self.trader.closing_orders[(1,2)]), 2)
        self.assertEqual(self.ib.placeOrder.call_count, 3)
