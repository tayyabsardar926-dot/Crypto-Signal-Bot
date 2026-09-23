import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import signal_bot as b


def bars(n=240):
    import math
    values = [100+i*.12+math.sin(i*.5)*1.2 for i in range(n)]
    return [dict(o=x-.1, c=x, h=x+.4, l=x-.4, v=100) for x in values]


CFG = b.load_config('config.yaml')


class Indicators(unittest.TestCase):
    def test_ema_constant(self):
        self.assertEqual(b.ema([7]*20, 5), [7]*16)

    def test_ema_seed_and_update(self):
        self.assertEqual(b.ema([1, 2, 3, 4], 3), [2, 3])

    def test_rsi_rising(self):
        self.assertEqual(b.rsi(list(range(30))), 100)

    def test_rsi_falling(self):
        self.assertEqual(b.rsi(list(range(30, 0, -1))), 0)

    def test_rsi_flat(self):
        self.assertEqual(b.rsi([5]*30), 50)

    def test_atr_known(self):
        self.assertAlmostEqual(b.atr([dict(h=11, l=9, c=10)]*30), 2)

    def test_atr_gap(self):
        rows = [dict(h=10, l=9, c=10), dict(h=15, l=14, c=15)]
        self.assertEqual(b.atr(rows, 1), 5)

    def test_short_history(self):
        for fn in [b.rsi, b.atr]:
            with self.assertRaises(ValueError):
                fn([])

    def test_trend_rising(self):
        self.assertTrue(b.trend(bars()))

    def test_trend_falling(self):
        self.assertFalse(b.trend(list(reversed(bars()))))

    def test_structure(self):
        self.assertTrue(b.structure(bars()))

    def test_pivot_confirmation(self):
        rows = [dict(h=x) for x in [1, 2, 5, 2, 1, 8]]
        self.assertEqual(b.pivots(rows, 'h'), [5])

    def test_score_bounds(self):
        self.assertEqual(b.score({}), 0)
        self.assertEqual(b.score(dict.fromkeys(['trend','structure','volume','momentum','volatility','liquidity','btc','extension','resistance','rr'], 2)), 100)

    def test_score_btc_penalty(self):
        self.assertEqual(b.score({'btc':1})-b.score({'btc':0}), 10)

    def test_correlation(self):
        self.assertAlmostEqual(b.correlation(list(range(48)), list(range(48))), 1)

    def test_unknown_correlation(self):
        self.assertEqual(b.correlation([], []), 1)


class Filters(unittest.TestCase):
    def setUp(self):
        self.info = dict(status='TRADING', quoteAsset='USDT', isSpotTradingAllowed=True)
        self.ticker = dict(quoteVolume=20000000)
        self.book = dict(bidPrice=100, askPrice=100.01)

    def test_liquidity_pass(self):
        self.assertTrue(b.liquid(self.info, self.ticker, self.book, CFG))

    def test_spread_fail(self):
        self.book['askPrice'] = 101
        self.assertFalse(b.liquid(self.info, self.ticker, self.book, CFG))

    def test_zero_bid(self):
        self.book['bidPrice'] = 0
        self.assertFalse(b.liquid(self.info, self.ticker, self.book, CFG))

    def test_volume_fail(self):
        self.ticker['quoteVolume'] = 1
        self.assertFalse(b.liquid(self.info, self.ticker, self.book, CFG))

    def test_nonspot(self):
        self.info['isSpotTradingAllowed'] = False
        self.assertFalse(b.liquid(self.info, self.ticker, self.book, CFG))

    def test_age_boundary(self):
        self.assertTrue(b.age_ok(1000, 548*86400+1, 548))
        self.assertFalse(b.age_ok(2000, 548*86400+1, 548))

    def test_age_future_missing(self):
        self.assertFalse(b.age_ok(0, 1000, 548))
        self.assertFalse(b.age_ok(999999999999999, 1000, 548))


class Levels(unittest.TestCase):
    def setUp(self):
        self.rows = bars()
        self.piv = patch.object(b, 'pivots', side_effect=lambda rows, key: [99] if key == 'l' else [102, 104])
        self.piv.start()
        self.addCleanup(self.piv.stop)
        self.em = patch.object(b, 'ema', return_value=[100])
        self.em.start()
        self.addCleanup(self.em.stop)
        self.at = patch.object(b, 'atr', return_value=.5)
        self.at.start()
        self.addCleanup(self.at.stop)

    def test_levels_order_and_rr(self):
        lv = b.levels(self.rows, [self.rows, self.rows], 100, CFG)
        self.assertLess(lv['sl'], lv['low'])
        self.assertLessEqual(lv['low'], lv['entry'])
        self.assertLess(lv['entry'], lv['tp1'])
        self.assertLess(lv['tp1'], lv['tp2'])
        self.assertGreaterEqual(lv['rr'], 1.5)

    def test_rr_rejected(self):
        with self.assertRaisesRegex(ValueError, 'R:R'):
            b.levels(self.rows, [self.rows], 100, dict(CFG, min_rr=3))

    def test_stop_rejected(self):
        with self.assertRaisesRegex(ValueError, 'stop distance'):
            b.levels(self.rows, [self.rows], 100, dict(CFG, max_stop_pct=.5))

    def test_target_rejected(self):
        with self.assertRaisesRegex(ValueError, 'upside'):
            b.levels(self.rows, [self.rows], 100, dict(CFG, min_target_pct=3))

    def test_no_resistance_no_fabrication(self):
        b.pivots.side_effect = lambda rows, key: [99] if key == 'l' else []
        with self.assertRaisesRegex(ValueError, 'resistances'):
            b.levels(self.rows, [self.rows], 100, CFG)


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'nested/state.json'
        self.s = b.State(self.path)
        self.now = 1800000000

    def test_no_state_startup(self):
        self.assertEqual(self.s.data['alerts'], [])
        self.s.save()
        self.assertEqual(b.State(self.path).data, self.s.data)

    def test_cooldown(self):
        self.s.reserve(dict(symbol='BTCUSDT', sector='payments'), self.now)
        self.assertEqual(self.s.allowed('BTCUSDT', 'payments', self.now+1, CFG, 5), 'cooldown')

    def test_daily_cap(self):
        for i in range(5):
            self.s.reserve(dict(symbol=str(i), sector=str(i)), self.now)
        self.assertEqual(self.s.allowed('NEW', 'new', self.now, CFG, 5), 'daily cap')

    def test_date_rollover(self):
        self.s.reserve(dict(symbol='BTCUSDT', sector='payments'), self.now)
        self.assertIsNone(self.s.allowed('BTCUSDT', 'payments', self.now+86400, CFG, 5))

    def test_sector_cap(self):
        self.s.reserve(dict(symbol='BTCUSDT', sector='payments'), self.now)
        self.assertEqual(self.s.allowed('LTCUSDT', 'payments', self.now, CFG, 5), 'sector daily cap')

    def test_corrupt_state_fails_closed(self):
        self.s.save()
        self.path.write_text('bad JSON')
        with self.assertRaises(ValueError):
            b.State(self.path)


class Delivery(unittest.TestCase):
    def test_format(self):
        signal = dict(symbol='BTCUSDT', price=100, low=99.8, entry=100, tp1=102, tp2=104,
                      sl=99, setup='support bounce', score=90, timing='15m', rsi=60, volume=1.5,
                      btc='strong uptrend', rr=2, timestamp='2026-09-20T12:00:00+00:00')
        text = b.format_signal(signal)
        for field in ['Current Price', 'Buy Zone', 'TP1', 'TP2', 'SL', 'Setup', '90/100', 'Risk', '4H', '1H', '15m', '30m', 'RSI', 'Volume', 'BTC', 'UTC', '+2.00%']:
            self.assertIn(field, text)

    @patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN':'private-token', 'TELEGRAM_CHAT_ID':'private-chat'})
    @patch('signal_bot.requests.post')
    def test_timeout_not_retried_or_leaked(self, post):
        post.side_effect = b.requests.Timeout('private-token')
        with self.assertRaises(RuntimeError) as caught:
            b.telegram('test')
        self.assertNotIn('private-token', str(caught.exception))
        self.assertEqual(post.call_count, 1)

    @patch('signal_bot.time.sleep')
    def test_http_retry(self, sleep):
        m = b.Market(CFG)
        bad, good = Mock(status_code=429, headers={}), Mock(status_code=200, headers={})
        good.json.return_value = {'ok': 1}
        m.session.get = Mock(side_effect=[bad, good])
        self.assertEqual(m.get('/api/v3/time'), {'ok': 1})
        self.assertEqual(m.session.get.call_count, 2)
        m.get('/api/v3/time')
        self.assertEqual(m.session.get.call_count, 2)

    def test_stale_bars(self):
        m = b.Market(CFG)
        m.get = Mock(return_value=[])
        with self.assertRaisesRegex(ValueError, 'stale'):
            m.bars('BTCUSDT', '1h', 1800000000)


if __name__ == '__main__':
    unittest.main()
