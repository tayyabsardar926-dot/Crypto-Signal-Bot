"""Offline orchestration contracts; no public network or Telegram calls."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import signal_bot as b
from test_bot import CFG, bars


class ScanIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = dict(CFG, state_path=str(Path(self.tmp.name)/'state.json'))
        self.now = 1800000000
        self.signal = dict(low=99.8, entry=100, sl=99, tp1=102, tp2=104, rr=2,
                           price=100, setup='support bounce', timing='15m', score=90,
                           rsi=60, volume=1.5, btc='strong uptrend', returns=list(range(48)))

    def run_scan(self, mode=None, bullish=True, fail_persist=False):
        def get(endpoint, **kwargs):
            if endpoint.endswith('/time'):
                return {'serverTime': self.now*1000}
            if endpoint.endswith('/exchangeInfo'):
                return {'symbols': [dict(symbol='BTCUSDT', baseAsset='BTC', quoteAsset='USDT', status='TRADING', isSpotTradingAllowed=True)]}
            if endpoint.endswith('/24hr'):
                return [dict(symbol='BTCUSDT', quoteVolume=20000000)]
            if endpoint.endswith('/bookTicker'):
                row = dict(symbol='BTCUSDT', bidPrice=99.99, askPrice=100.01)
                return row if kwargs else [row]
            if endpoint.endswith('/klines'):
                return [[1000]]
            raise AssertionError(endpoint)
        market = Mock()
        market.cache = {}
        market.get.side_effect = get
        market.bars.return_value = bars()
        args = ['signal_bot.py'] + ([mode] if mode else [])
        with patch.object(sys, 'argv', args), patch.object(b, 'load_config', return_value=self.cfg), patch.object(b, 'Market', return_value=market), patch.object(b, 'trend', return_value=bullish), patch.object(b, 'evaluate', return_value=self.signal.copy()) as evaluate, patch.object(b.time, 'time', return_value=self.now), patch.object(b, 'telegram') as telegram, patch.object(b, 'persist_remote') as persist, patch.dict(b.os.environ, {'TELEGRAM_BOT_TOKEN':'test', 'TELEGRAM_CHAT_ID':'test'}), contextlib.redirect_stdout(io.StringIO()) as output:
            if fail_persist:
                persist.side_effect = RuntimeError('disk unavailable')
                with self.assertRaises(RuntimeError):
                    b.main()
            else:
                b.main()
            return telegram.call_count, evaluate.call_count, output.getvalue()

    def test_dry_run_never_sends_or_writes(self):
        sent, evaluated, output = self.run_scan('--dry-run')
        self.assertEqual(sent, 0)
        self.assertEqual(evaluated, 1)
        self.assertIn('SPOT WATCH', output)
        self.assertFalse(Path(self.cfg['state_path']).exists())

    def test_btc_suppresses_before_evaluation(self):
        sent, evaluated, output = self.run_scan('--dry-run', bullish=False)
        self.assertEqual((sent, evaluated), (0, 0))
        self.assertIn('NO TRADE', output)

    def test_reservation_survives_and_cooldown_blocks_next_run(self):
        self.assertEqual(self.run_scan()[0], 1)
        state = b.State(self.cfg['state_path'])
        self.assertEqual(len(state.data['alerts']), 1)
        self.assertEqual(self.run_scan()[0], 0)

    def test_persist_failure_prevents_delivery(self):
        self.assertEqual(self.run_scan(fail_persist=True)[0], 0)

    def test_test_mode_no_market_or_state(self):
        sent, evaluated, output = self.run_scan('--test-telegram')
        self.assertEqual((sent, evaluated), (1, 0))
        self.assertFalse(Path(self.cfg['state_path']).exists())


if __name__ == '__main__':
    unittest.main()
