"""Latest family capabilities must match distinct upstream controls, not guesses."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api import reasoning as reasoning_api
from backend.security import get_current_user

from backend.reasoning_options import (
    apply_turn_reasoning, canonical_reasoning_model_id, common_reasoning_capabilities,
    reasoning_capabilities, validate_reasoning_settings,
)


def provider(model, wire='chat_completions', **overrides):
    return dict(model_id=model, provider_type='openai', wire_api=wire,
                model_reasoning=True, reasoning_effort='', reasoning_config={}, **overrides)


class LatestReasoningProfilesTests(unittest.TestCase):
    def test_latest_models_reach_local_capability_preview(self):
        app = FastAPI()
        app.include_router(reasoning_api.router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=99, role='user')
        with TestClient(app) as browser, patch('httpx.AsyncClient.request', side_effect=AssertionError('Capability preview must stay local')):
            for model, expected in (
                ('MiniMax-M3', ['disabled', 'enabled']),
                ('deepseek-flash', ['none', 'low', 'high', 'max']),
                ('tencent/hy4-preview', ['none', 'high']),
            ):
                response = browser.post('/api/v1/reasoning-capabilities', json={
                    'model_id': model, 'model_reasoning': True, 'wire_api': 'messages', 'max_tokens': 512,
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['reasoning_efforts'], expected)

    def test_latest_model_controls_across_protocols(self):
        for wire in ('chat_completions', 'responses', 'messages'):
            for model in ('deepseek-flash', 'deepseek-v4-pro', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'):
                with self.subTest(wire=wire, model=model):
                    self.assertEqual(reasoning_capabilities(provider(model, wire))['reasoning_efforts'], ['none', 'low', 'high', 'max'])
            self.assertEqual(reasoning_capabilities(provider('hy3', wire))['reasoning_efforts'], ['none', 'low', 'high'])
            self.assertEqual(reasoning_capabilities(provider('hy4-preview', wire))['reasoning_efforts'], ['none', 'high'])
            capability = reasoning_capabilities(provider('MiniMax-M3', wire))
            self.assertEqual(capability['reasoning_control'], 'thinking_toggle')
            self.assertEqual(capability['reasoning_efforts'], ['disabled', 'enabled'])

    def test_fixed_thinking_recognized_without_fake_choices(self):
        for model in ('MiniMax-M2', 'MiniMax-M2.1', 'MiniMax-M2.1-highspeed', 'MiniMax-M2.5', 'MiniMax-M2.5-highspeed', 'MiniMax-M2.7', 'MiniMax-M2.7-highspeed'):
            capability = reasoning_capabilities(provider(model))
            self.assertFalse(capability['reasoning_supported'])
            self.assertIn('固定开启思考', capability['reasoning_unavailable_reason'])
            self.assertEqual(capability['reasoning_efforts'], [])
        legacy = provider('MiniMax-M2.7')
        legacy['reasoning_effort'] = 'high'
        self.assertEqual(reasoning_capabilities(legacy)['reasoning_effort'], '')
        self.assertEqual(legacy['reasoning_effort'], 'high')

    def test_bounded_namespaces_and_case_do_not_change_request_id(self):
        for name, canonical, effort in (
            ('MiniMaxAI/MiniMax-M3', 'minimax-m3', 'enabled'),
            ('minimax/MiniMax-M3', 'minimax-m3', 'disabled'),
            ('DeepSeek/deepseek-flash', 'deepseek-flash', 'max'),
            ('deepseek-ai/DeepSeek-V4-Pro', 'deepseek-v4-pro', 'low'),
            ('Tencent/Hy4-preview', 'hy4-preview', 'high'),
            ('hunyuan/hy3', 'hy3', 'none'),
            ('anthropic/claude-sonnet-4-6', 'claude-sonnet-4-6', 'max'),
            ('z-ai/GLM-5.3', 'glm-5.3', 'max'),
            ('moonshotai/kimi-k3', 'kimi-k3', 'max'),
        ):
            with self.subTest(name=name):
                original = provider(name)
                self.assertEqual(canonical_reasoning_model_id(name), canonical)
                self.assertEqual(apply_turn_reasoning(original, effort)['model_id'], name)
                self.assertEqual(original['reasoning_effort'], '')
        for unknown in ('proxy/hy3', 'tencent/future-hy9', 'deepseek/deepseek-v9', 'minimax/MiniMax-M3-FP8', 'custom/path/hy3'):
            self.assertFalse(reasoning_capabilities(provider(unknown))['reasoning_supported'])

    def test_minimax_auto_messages_has_no_claude_budget_requirement(self):
        model = provider('MiniMax-M3', 'messages', max_tokens=512)
        self.assertTrue(reasoning_capabilities(model)['reasoning_supported'])
        validate_reasoning_settings(apply_turn_reasoning(model, 'enabled'))
        model['reasoning_config'] = {'mode': 'custom', 'control': 'thinking_toggle', 'supported_efforts': ['disabled', 'enabled']}
        with self.assertRaisesRegex(ValueError, '预算'):
            validate_reasoning_settings(model)

    def test_off_disabled_and_custom_remain_authoritative(self):
        model = provider('hy4-preview')
        model['model_reasoning'] = False
        self.assertFalse(reasoning_capabilities(model)['reasoning_supported'])
        model.update(model_reasoning=True, reasoning_config={'mode': 'off'})
        self.assertFalse(reasoning_capabilities(model)['reasoning_supported'])
        model['reasoning_config'] = {'mode': 'custom', 'supported_efforts': ['low', 'max']}
        self.assertEqual(reasoning_capabilities(model)['reasoning_efforts'], ['low', 'max'])

    def test_routing_intersection_does_not_mix_toggle_and_effort(self):
        deepseek = provider('deepseek-flash')
        self.assertEqual(common_reasoning_capabilities([deepseek, provider('hy4-preview')])['reasoning_efforts'], ['none', 'high'])
        self.assertEqual(common_reasoning_capabilities([deepseek, provider('MiniMax-M3')])['reasoning_efforts'], [])

    def test_legacy_and_unverified_ids_are_not_silently_upgraded(self):
        for model in ('deepseek-chat', 'deepseek-reasoner', 'hy3-preview', 'deepseek-v4.1-flash'):
            capability = reasoning_capabilities(provider(model))
            self.assertFalse(capability['reasoning_supported'])
            self.assertTrue(capability['reasoning_unavailable_reason'])


if __name__ == '__main__':
    unittest.main()
