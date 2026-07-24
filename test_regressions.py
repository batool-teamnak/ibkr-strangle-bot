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
