from ib_insync import *
import numpy as np
import math
from datetime import datetime, timedelta, time
import time as clock

class HybridStrangleTrader:
    def __init__(self, ib=None, connect=True, host="127.0.0.1", port=7497, client_id=1):
        self.ib = ib if ib is not None else IB()
        try:
            if connect:
                self.ib.connect(host, port, clientId=client_id)
            print("✅ Connected to IBKR")
        except Exception as e:
            self.ib.disconnect()
            raise ConnectionError(f"Unable to connect to IBKR: {e}") from e

        # Parameters
        self.account_value = 4900
        self.max_strangles = 2
        self.min_premium = 0.30
        self.profit_target_high_iv = 0.50
        self.profit_target_low_iv = 0.25
        self.stop_loss = 2.0
        self.sell_time = time(15, 45)
        self.buy_time = time(9, 35)
        self.blacklist = set()
        self.current_positions = {}
        self.position_symbols = {}
        self.earnings_dates = self.load_earnings_dates()
        self.stocks = ['AMC', 'PLTR', 'F', 'SNAP']

    def load_earnings_dates(self):
        return {
            'AMC': ['2023-11-28', '2024-02-27'],
            'PLTR': ['2023-11-07', '2024-02-20'],
            'F': ['2023-10-26', '2024-01-23'],
            'SNAP': ['2023-10-24', '2024-01-30']
        }

    def get_iv_rank(self, ticker):
        try:
            stock = Stock(ticker, 'SMART', 'USD')
            self.ib.qualifyContracts(stock)
            hv = self.ib.reqHistoricalData(
                stock, endDateTime='', durationStr='1 Y',
                barSizeSetting='1 day', whatToShow='HISTORICAL_VOLATILITY', useRTH=True
            )
            ticker_data = self.ib.reqMktData(stock, '', False, False)
            self.ib.sleep(1)
            iv = ticker_data.impliedVolatility
            if not hv or not isinstance(iv, (int, float)) or not math.isfinite(iv) or iv <= 0:
                return None
            closes = [h.close for h in hv[-20:]]
            if any(not math.isfinite(v) or v <= 0 for v in closes):
                return None
            hv_20 = np.mean(closes)
            iv_rank = min(100, max(0, (iv / hv_20 - 0.8) * 100))
            return iv_rank
        except Exception as e:
            print(f"⚠️ Error calculating IV rank for {ticker}: {e}")
            return 50

    def is_earnings_soon(self, ticker, days=5):
        today = datetime.now().strftime('%Y-%m-%d')
        future = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
        return any(today <= d <= future for d in self.earnings_dates.get(ticker, []))

    def sell_strangle(self, ticker):
        if len(self.current_positions) >= self.max_strangles:
            return
        if ticker in self.position_symbols.values():
            return
        if ticker in self.blacklist or self.is_earnings_soon(ticker):
            print(f"⚠️ Skipping {ticker} (blacklist/earnings soon)")
            return

        stock = Stock(ticker, 'SMART', 'USD')
        try:
            self.ib.qualifyContracts(stock)
            ticker_data = self.ib.reqMktData(stock, '', False, False)
            self.ib.sleep(1)
            price = ticker_data.marketPrice()
            if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
                print(f"❌ No valid market price for {ticker}")
                return
            print(f"📈 {ticker} market price: {price:.2f}")
        except Exception as e:
            print(f"❌ Error getting {ticker} market price: {e}")
            return

        iv_rank = self.get_iv_rank(ticker)
        if iv_rank is None:
            return
        expiry = self.next_expiry()

        if iv_rank > 70:
            put_strike = round(price * 0.90, 1)
            call_strike = round(price * 1.10, 1)
        else:
            put_strike = round(price * 0.94, 1)
            call_strike = round(price * 1.06, 1)

        put = Option(ticker, expiry, put_strike, 'P', 'SMART')
        call = Option(ticker, expiry, call_strike, 'C', 'SMART')

        try:
            qualified = self.ib.qualifyContracts(put, call)
            if len(qualified) != 2 or not put.conId or not call.conId or put.conId == call.conId:
                return
            put_data = self.ib.reqMktData(put, '', False, False)
            call_data = self.ib.reqMktData(call, '', False, False)
            self.ib.sleep(1)

            if any(not isinstance(p, (int, float)) or not math.isfinite(p) or p <= 0
                   for p in (put_data.ask, call_data.ask)):
                print(f"❌ Missing option prices for {ticker}")
                return
            if put_data.ask < self.min_premium or call_data.ask < self.min_premium:
                print(f"⚠️ Premium too low for {ticker}: P={put_data.ask}, C={call_data.ask}")
                return

            self.ib.placeOrder(put, LimitOrder('SELL', 1, put_data.ask))
            self.ib.placeOrder(call, LimitOrder('SELL', 1, call_data.ask))
            credit = put_data.ask + call_data.ask
            self.position_symbols[(put.conId, call.conId)] = ticker
            self.current_positions[(put.conId, call.conId)] = (datetime.now(), credit, iv_rank)
            print(f"✅ Sold {ticker} {put_strike}P/{call_strike}C @ ${credit:.2f} (IV Rank: {iv_rank:.0f})")
        except Exception as e:
            print(f"❌ Error placing order for {ticker}: {e}")

    def manage_positions(self):
        to_remove = []
        for key, (open_time, credit, iv_rank) in self.current_positions.items():
            try:
                put_id, call_id = key
                put = Contract(conId=put_id, exchange='SMART')
                call = Contract(conId=call_id, exchange='SMART')
                self.ib.qualifyContracts(put, call)
                put_data = self.ib.reqMktData(put, '', False, False)
                call_data = self.ib.reqMktData(call, '', False, False)
                self.ib.sleep(1)

                put_price = put_data.midpoint() or put_data.last
                call_price = call_data.midpoint() or call_data.last
                if put_price is None or call_price is None:
                    continue

                value = put_price + call_price
                profit_pct = (credit - value) / credit
                reason = None
                if iv_rank > 50 and profit_pct >= self.profit_target_high_iv:
                    reason = "🔓 Hit 50% target"
                elif iv_rank < 30 and profit_pct >= self.profit_target_low_iv:
                    reason = "🔓 Hit 25% target (low IV)"
                elif value >= credit * self.stop_loss:
                    reason = "🛑 Stop loss hit"
                    self.blacklist.add(put.symbol)

                if reason:
                    self.ib.placeOrder(put, MarketOrder('BUY', 1))
                    self.ib.placeOrder(call, MarketOrder('BUY', 1))
                    to_remove.append(key)
                    print(f"🔁 Closing {put.symbol} strangle: {reason}")
            except Exception as e:
                print(f"❌ Error managing position: {e}")

        for key in to_remove:
            self.current_positions.pop(key, None)
            self.position_symbols.pop(key, None)

    def next_expiry(self):
        today = datetime.now()
        days_until_friday = (4 - today.weekday()) % 7
        friday = today + timedelta(days=days_until_friday or 7)
        return friday.strftime('%Y%m%d')

    def run(self):
        print(f"🚀 Hybrid Strangle Trader Running | Account: ${self.account_value}")
        try:
            while True:
                now = datetime.now()
                if now.weekday() >= 5:
                    time.sleep(600)
                    continue

                if not time(9, 30) <= now.time() <= time(16, 0):
                    time.sleep(60)
                    continue

                if now.time() >= self.sell_time:
                    if len(self.current_positions) < self.max_strangles:
                        for ticker in self.stocks:
                            self.sell_strangle(ticker)
                            time.sleep(1)

                self.manage_positions()
                time.sleep(60)
        except KeyboardInterrupt:
            print("🛑 Trader manually stopped.")
        finally:
            self.ib.disconnect()
            print("🔌 Disconnected from IBKR")

if __name__ == '__main__':
    trader = HybridStrangleTrader()
    trader.run()
