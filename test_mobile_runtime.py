"""Exercise phone setting bounds and honest quota/context metadata."""
import json
from pathlib import Path
import tempfile
import unittest

import mobile_runtime as runtime


class MobileRuntimeTests(unittest.TestCase):
    def catalog(self):
        return runtime.catalog_models({'data': [
            {'id': 'fast', 'model': 'model-fast', 'displayName': 'Fast Model',
             'defaultReasoningEffort': 'high', 'supportedReasoningEfforts': [
                 {'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]},
            {'id': 'deep', 'model': 'model-deep', 'supportedReasoningEfforts': [
                {'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}, {'reasoningEffort': 'ultra'}],
             'defaultReasoningEffort': 'max', 'serviceTiers': [{'id': 'priority', 'name': 'Fast',
                'description': 'Uses more quota'}], 'defaultServiceTier': 'priority'},
            {'id': 'secret-model', 'hidden': True},
        ]})

    def test_catalog_filters_hidden_malformed_and_duplicate_rows(self):
        result = runtime.catalog_models({'result': {'data': [None, {}, {'model': 'line\nbreak'},
            {'model': 'usable', 'supportedReasoningEfforts': ['low', 'max', 'max', {}]},
            {'model': 'usable', 'hidden': False}, {'model': 'hidden', 'hidden': True}]}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['efforts'], ['low', 'max'])
        self.assertEqual(result[0]['serviceTiers'], [])
        self.assertIsNone(result[0]['defaultEffort'])
        self.assertEqual(runtime.catalog_models(None), [])

    def test_validates_dynamic_max_and_ultra_without_fixed_ceiling(self):
        self.assertEqual(runtime.validate_options({'model': 'model-deep', 'effort': 'ultra',
            'serviceTier': 'priority'}, self.catalog()),
            {'model': 'model-deep', 'effort': 'ultra', 'serviceTier': 'priority'})
        self.assertEqual(runtime.validate_options({'model': 'model-deep'}, self.catalog()),
                         {'model': 'model-deep', 'effort': 'max'})
        for effort in ('max', 'ultra', 'xhigh'):
            with self.assertRaises(ValueError):
                runtime.validate_options({'model': 'model-fast', 'effort': effort}, self.catalog())

    def test_unknown_model_and_keys_are_rejected(self):
        for value in ({'model': 'unknown'}, {}, None, {'model': 'model-fast', 'sandbox': 'danger'}):
            with self.assertRaises(ValueError):
                runtime.validate_options(value, self.catalog())

    def test_fast_tier_requires_advertised_support_and_can_be_cleared(self):
        with self.assertRaises(ValueError):
            runtime.validate_options({'model': 'model-fast', 'serviceTier': 'priority'}, self.catalog())
        self.assertEqual(runtime.validate_options({'model': 'model-fast', 'serviceTier': None}, self.catalog()),
                         {'model': 'model-fast', 'effort': 'high', 'serviceTier': None})
        self.assertEqual(runtime.validate_options({'model': 'model-fast', 'serviceTier': 'default'}, self.catalog())['serviceTier'], 'default')
        self.assertEqual(self.catalog()[1]['defaultServiceTier'], 'priority')
        with self.assertRaises(ValueError):
            runtime.validate_options({'model': 'model-deep', 'serviceTier': 'ultrafast'}, self.catalog())

    def test_options_describes_thread_next_turn_and_explicit_clear(self):
        text = runtime.options_text({'model': 'model-fast', 'reasoning_effort': 'high',
                                     'service_tier': 'priority'}, {'serviceTier': None})
        self.assertIn('下一轮使用', text)
        self.assertIn('速度档位：标准（默认）', text)
        self.assertIn('速度档位：未知（沿用原会话）', runtime.options_text({'model': 'model-fast'}))
        self.assertIn('不修改全局配置', text)

    def test_used_percent_is_consumed_not_remaining_and_zero_is_known(self):
        text = runtime.usage_text({'rateLimits': {'primary': {'usedPercent': 25,
            'windowDurationMins': 300, 'resetsAt': 2000}, 'secondary': {'usedPercent': 0,
            'windowDurationMins': 10080, 'resetsAt': 9000}}}, sampled_at=1000, now=1001)
        self.assertIn('5 小时窗口：剩余 75%', text)
        self.assertIn('7 天窗口：剩余 100%', text)
        self.assertIn('查询不会调用模型', text)

    def test_unknown_quota_is_not_zero_and_reset_unknown_is_explicit(self):
        text = runtime.usage_text({'primary': {'usedPercent': None}}, sampled_at=1000, now=1001)
        self.assertIn('剩余额度未知', text)
        self.assertIn('重置时间未知', text)
        self.assertNotIn('剩余 100%', text)
        self.assertIn('暂时无法读取', runtime.usage_text(None, now=1001))

    def test_remaining_percentage_is_clamped(self):
        text = runtime.usage_text({'primary': {'usedPercent': 120},
                                   'secondary': {'usedPercent': -10}}, sampled_at=1000, now=1001)
        self.assertIn('剩余 0%', text)
        self.assertIn('剩余 100%', text)

    def test_multi_bucket_view_takes_priority_and_is_bounded(self):
        text = runtime.usage_text({'rateLimits': {'limitName': 'LEGACY', 'primary': {'usedPercent': 99}},
            'rateLimitsByLimitId': {str(index): {'limitName': f'Model {index}',
                'primary': {'usedPercent': index}} for index in range(8)}}, sampled_at=1000, now=1001)
        self.assertNotIn('LEGACY', text)
        self.assertIn('Model 5', text)
        self.assertNotIn('Model 6', text)
        self.assertIn('另有 2 个额度分组', text)

    def test_past_reset_is_stale_even_with_a_fresh_fetch_timestamp(self):
        text = runtime.usage_text({'primary': {'usedPercent': 25, 'resetsAt': 900}},
                                  sampled_at=1000, now=1001)
        self.assertIn('上次剩余 75%', text)
        self.assertIn('已过重置时间，待刷新', text)

    def test_cached_missing_and_future_timestamps_are_not_fresh(self):
        for sampled in (None, 0, 2000):
            text = runtime.usage_text({'primary': {'usedPercent': 25, 'resetsAt': 3000}},
                                      sampled_at=sampled, now=1001)
            self.assertIn('上次剩余 75%', text)
            self.assertIn('缓存待刷新', text)

    def test_context_uses_last_not_cumulative_usage(self):
        context = {'info': {'total_token_usage': {'total_tokens': 900000},
            'last_token_usage': {'total_tokens': 2500}, 'model_context_window': 10000}}
        text = runtime.usage_text(None, context, now=1001)
        self.assertIn('2,500 / 10,000 tokens（占用 25%）', text)
        self.assertNotIn('900,000', text)

    def test_context_server_shape_and_zero_tokens(self):
        text = runtime.usage_text(None, {'method': 'thread/tokenUsage/updated',
            'params': {'tokenUsage': {'last': {'totalTokens': 0}, 'modelContextWindow': 10000}}})
        self.assertIn('0 / 10,000 tokens（占用 0%）', text)
        self.assertIn('上下文上限未知', runtime.usage_text(None, {'used_tokens': 0}))

    def write_rollout(self, directory, rows, suffix=b''):
        path = Path(directory) / 'rollout.jsonl'
        path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows) + suffix)
        return path

    def test_snapshot_returns_only_allowlisted_latest_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_rollout(directory, [
                {'type': 'session_meta', 'payload': {'id': 'thread-1', 'instructions': 'PRIVATE'}},
                {'type': 'turn_context', 'payload': {'model': 'model-deep', 'effort': 'ultra',
                    'approval_policy': 'never', 'developer_instructions': 'PRIVATE'}},
                {'timestamp': '2026-10-21T01:00:00Z', 'type': 'event_msg', 'payload': {'type': 'token_count',
                    'info': {'total_token_usage': {'total_tokens': 999999},
                             'last_token_usage': {'total_tokens': 100}, 'model_context_window': 1000}}},
                {'type': 'response_item', 'payload': {'type': 'reasoning', 'text': 'PRIVATE'}},
                {'timestamp': '2026-10-21T01:01:00Z', 'type': 'event_msg', 'payload': {'type': 'token_count',
                    'info': {'last_token_usage': {'total_tokens': 200}, 'model_context_window': 1000},
                    'rate_limits': {'primary': {'used_percent': 30, 'resets_at': 9999999999,
                                               'secret': 'PRIVATE'}}, 'message': 'PRIVATE'}},
            ], b'{"type": "event_msg", "payload": {"type":"token_count", "info":')
            snapshot = runtime.safe_read_snapshot(path, 'thread-1')
            self.assertEqual(snapshot['current'], {'model': 'model-deep', 'effort': 'ultra'})
            self.assertEqual(snapshot['context']['used_tokens'], 200)
            self.assertEqual(snapshot['rate_limits']['primary']['used_percent'], 30)
            rendered = json.dumps(snapshot)
            self.assertNotIn('PRIVATE', rendered)
            self.assertNotIn('999999,', rendered)
            self.assertEqual(runtime.safe_read_snapshot(path, 'different')['context'], {})

    def test_snapshot_reads_bounded_tail_and_does_not_scan_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_rollout(directory, [
                {'type': 'session_meta', 'payload': {'id': 'thread-1'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count',
                    'info': {'last_token_usage': {'total_tokens': 123}}}},
                {'type': 'response_item', 'payload': {'text': 'PRIVATE' * 1000}},
            ])
            self.assertEqual(runtime.safe_read_snapshot(path, 'thread-1', max_bytes=1024)['context'], {})
            self.assertEqual(runtime.safe_read_snapshot('/missing/file')['context'], {})

    def test_snapshot_ignores_invalid_records_and_non_regular_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_rollout(directory, [{'type': 'session_meta', 'payload': {'id': 'thread-1'}}])
            with path.open('ab') as output:
                output.write(b'not-json\n[]\n')
            self.assertEqual(runtime.safe_read_snapshot(path, 'thread-1')['thread'], 'thread-1')
            self.assertEqual(runtime.safe_read_snapshot(directory)['context'], {})

    def test_unknown_latest_context_does_not_reuse_older_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_rollout(directory, [
                {'type': 'session_meta', 'payload': {'id': 'thread-1'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count',
                    'info': {'last_token_usage': {'total_tokens': 123}},
                    'rate_limits': {'primary': {'used_percent': 25}}}},
                {'timestamp': '2026-10-21T01:00:00Z', 'type': 'event_msg',
                 'payload': {'type': 'token_count', 'info': None}},
            ])
            result = runtime.safe_read_snapshot(path, 'thread-1')
            self.assertNotIn('used_tokens', result['context'])
            self.assertIsNotNone(result['sampled_at'])
            self.assertIsNone(result['rate_limits'])

    def test_invalid_numeric_values_do_not_raise_or_look_available(self):
        for value in (float('nan'), float('inf'), True, 10 ** 1000):
            text = runtime.usage_text({'primary': {'usedPercent': value}}, now=1001)
            self.assertIn('剩余额度未知', text)

    def test_invalid_context_counters_stay_unknown_and_usage_fits_telegram(self):
        for value in (0.5, -1, True, float('inf'), 10 ** 100):
            text = runtime.usage_text(None, {'used_tokens': value, 'window_tokens': value})
            self.assertIn('暂时没有可用', text)
        text = runtime.usage_text({'rateLimitsByLimitId': {str(index): {
            'limitName': 'x' * 200, 'primary': {'usedPercent': 0, 'windowDurationMins': 1e308},
            'secondary': {'usedPercent': 0, 'windowDurationMins': 1e307}} for index in range(6)}})
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 3800)

    def test_large_committed_metadata_header_preserves_identity_and_context(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = {'type':'session_meta','payload':{'id':'thread-1',
                'instructions':'PRIVATE-INSTRUCTIONS:' + 'x' * 23195}}
            header_bytes = json.dumps(metadata).encode() + b'\n'
            self.assertGreater(len(header_bytes),16*1024)
            self.assertLess(len(header_bytes),runtime.MAX_METADATA_BYTES)
            path = self.write_rollout(directory,[metadata,
                {'type':'turn_context','payload':{'model':'model-deep','effort':'ultra',
                    'developer_instructions':'PRIVATE-INSTRUCTIONS'}},
                {'timestamp':'2026-10-21T01:00:00Z','type':'event_msg','payload':{
                    'type':'token_count','info':{'last_token_usage':{'total_tokens':3456},
                        'model_context_window':10000}}}])
            result = runtime.safe_read_snapshot(path,'thread-1')
            self.assertEqual(result['thread'],'thread-1')
            self.assertEqual(result['context']['used_tokens'],3456)
            self.assertEqual(result['context']['window_tokens'],10000)
            self.assertEqual(result['current'],{'model':'model-deep','effort':'ultra'})
            self.assertNotIn('PRIVATE-INSTRUCTIONS',json.dumps(result))
            mismatch = runtime.safe_read_snapshot(path,'wrong-thread')
            self.assertIsNone(mismatch['thread'])
            self.assertEqual(mismatch['context'],{})

    def test_oversized_or_uncommitted_metadata_is_refused_before_tail_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = {'type':'session_meta','payload':{'id':'thread-1',
                'instructions':'PRIVATE:' + 'x'*runtime.MAX_METADATA_BYTES}}
            path = self.write_rollout(directory,[metadata,
                {'type':'event_msg','payload':{'type':'token_count','info':{
                    'last_token_usage':{'total_tokens':123},'model_context_window':1000}}}])
            for thread in (None,'thread-1'):
                result = runtime.safe_read_snapshot(path,thread)
                self.assertIsNone(result['thread'])
                self.assertEqual(result['context'],{})
                self.assertEqual(result['current'],{})
            path.write_bytes(json.dumps({'type':'session_meta',
                'payload':{'id':'thread-1'}}).encode())
            self.assertIsNone(runtime.safe_read_snapshot(path,'thread-1')['thread'])


if __name__ == '__main__':
    unittest.main()
