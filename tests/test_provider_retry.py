"""Kiểm tra transport offline; không gọi API hay dùng key thật."""
from contextlib import ExitStack
from http.client import RemoteDisconnected
from io import BytesIO
import unittest
import urllib.error
from unittest.mock import patch

import providers


class TransportTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.open = stack.enter_context(patch('providers.urllib.request.urlopen'))
        self.sleep = stack.enter_context(patch('providers.time.sleep'))
        stack.enter_context(patch.dict('os.environ', {'API_MIN_INTERVAL_SECONDS': '0'}))
        stack.enter_context(patch('providers.random.uniform', return_value=0))

    def request(self):
        return providers.post_json('https://example.test/chat/completions', {}, {'model': 'fake'})

    def error(self, code, retry_after=None):
        headers = {} if retry_after is None else {'Retry-After': retry_after}
        return urllib.error.HTTPError('https://example.test', code, 'error', headers, BytesIO(b'private body'))

    def response(self):
        response = self.open.return_value.__enter__.return_value
        response.read.return_value = b'{"ok": true}'
        return self.open.return_value

    def test_network_failure_then_success(self):
        self.open.side_effect = [RemoteDisconnected(), self.response()]
        self.assertEqual(self.request(), {'ok': True})
        self.assertEqual(self.open.call_count, 2)

    def test_gemini_429_respects_retry_after(self):
        self.open.side_effect = [self.error(429, '12'), self.response()]
        result = providers.post_json('https://generativelanguage.googleapis.com/v1beta/openai/chat/completions', {}, {})
        self.assertTrue(result['ok'])
        self.sleep.assert_called_once_with(12)

    def test_503_retries_but_401_does_not(self):
        self.open.side_effect = [self.error(503), self.response()]
        self.assertTrue(self.request()['ok'])
        self.open.reset_mock()
        self.open.side_effect = self.error(401)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
            self.request()
        self.assertEqual(self.open.call_count, 1)

    def test_retry_budget_stops_persistent_network_failure(self):
        self.open.side_effect = ConnectionResetError('private data')
        with self.assertRaisesRegex(RuntimeError, 'sau 4 lần thử') as caught:
            self.request()
        self.assertNotIn('private data', str(caught.exception))
        self.assertEqual(self.open.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_long_retry_after_does_not_retry_early(self):
        self.open.side_effect = self.error(429, '3600')
        with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
            self.request()
        self.assertEqual(self.open.call_count, 1)
        self.sleep.assert_not_called()

    def test_agent_and_judge_share_pacing(self):
        with patch.dict('os.environ', {'API_MIN_INTERVAL_SECONDS': '12'}), \
                patch.dict(providers._LAST_REQUEST, {}, clear=True), \
                patch('providers.time.monotonic', side_effect=[100, 100, 105, 112]):
            providers._pace_request('https://example.test', 'fake')
            providers._pace_request('https://example.test', 'fake')
        self.sleep.assert_called_once_with(7)


if __name__ == '__main__':
    unittest.main()
