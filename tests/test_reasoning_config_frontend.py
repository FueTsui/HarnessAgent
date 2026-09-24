"""Shared frontend reasoning configuration and model-specific stage capabilities."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReasoningConfigFrontendTests(unittest.TestCase):
    def run_js(self, body):
        setup = '''
const assert = require('node:assert/strict');
const config = require('./frontend/static/provider-reasoning-config.js');
const roles = require('./frontend/static/agent-model-config.js');
const chat = require('./frontend/static/chat-workspace.js');
'''
        result = subprocess.run(['node', '-'], cwd=ROOT, text=True, encoding='utf-8',
                                input=setup + body, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_custom_effort_values_are_validated_and_ordered_without_inventing_levels(self):
        self.run_js('''
const input={mode:'custom',control:'effort',effort_param:'reasoning.effort',supported_efforts:['max','low','high','low']};
assert.deepEqual(config.normalizeConfig(input),{...input,supported_efforts:['low','high','max']});
for (const supported_efforts of [[],['ultra'],['enabled'],['medium','made_up']])
  assert.throws(()=>config.normalizeConfig({...input,supported_efforts}));
assert.throws(()=>config.normalizeConfig({...input,effort_param:'arbitrary.json.path'}));
assert.deepEqual(config.normalizeConfig({}),{mode:'auto'});
assert.deepEqual(config.normalizeConfig({...input,mode:'off'}),{...input,mode:'off',supported_efforts:['low','high','max']});
''')

    def test_toggle_has_two_states_and_messages_budget_obeys_output_limit(self):
        self.run_js('''
const input={mode:'custom',control:'thinking_toggle',supported_efforts:['enabled','disabled']};
assert.deepEqual(config.normalizeConfig(input,{wire_api:'messages',max_tokens:8192}),
 {mode:'custom',control:'thinking_toggle',effort_param:'auto',supported_efforts:['disabled','enabled'],budget_tokens:2048});
assert.equal(Object.hasOwn(config.normalizeConfig(input,{wire_api:'chat_completions'}),'budget_tokens'),false);
for (const budget_tokens of [1023,8192,10000,1024.5,'bad'])
 assert.throws(()=>config.normalizeConfig({...input,budget_tokens},{wire_api:'messages',max_tokens:8192}));
assert.throws(()=>config.normalizeConfig({...input,supported_efforts:['low','high']}));
assert.equal(config.label('disabled'),'关闭');
assert.equal(config.label('enabled'),'开启');
''')

    def test_default_uses_actual_capabilities_and_disabled_reasoning_clears_it(self):
        self.run_js('''
const context={model_reasoning:true,wire_api:'responses'}, capability={reasoning_efforts:['low','high','max']};
assert.equal(config.buildPayload({},'high',context,capability).reasoning_effort,'high');
assert.equal(config.buildPayload({},'',context,capability).reasoning_effort,'');
assert.throws(()=>config.buildPayload({},'medium',context,capability));
assert.throws(()=>config.buildPayload({},'high',context,null));
assert.equal(config.buildPayload({},'high',{...context,model_reasoning:false},capability).reasoning_effort,'');
assert.equal(config.buildPayload({mode:'off'},'high',context,capability).reasoning_effort,'');
const disabled={mode:'off',control:'thinking_toggle',effort_param:'auto',supported_efforts:['disabled','enabled'],budget_tokens:4096};
const saved=config.buildPayload(disabled,'enabled',{model_reasoning:false,wire_api:'messages',max_tokens:2048},null);
assert.deepEqual(saved.reasoning_config,disabled);
assert.equal(saved.reasoning_effort,'');
const custom={mode:'custom',control:'effort',supported_efforts:['low','max']};
assert.throws(()=>config.buildPayload(custom,'high',context,capability));
assert.equal(config.buildPayload(custom,'max',context,capability).reasoning_effort,'max');
''')


    def test_stage_roles_follow_selected_connection_and_inherited_intersection(self):
        self.run_js('''
const providers=[
 {id:1,reasoning_supported:true,reasoning_efforts:['low','medium','high','max']},
 {id:2,reasoning_supported:true,reasoning_efforts:['low','high','max']},
 {id:3,reasoning_supported:true,reasoning_efforts:['disabled','enabled']},
 {id:4,reasoning_supported:false,reasoning_efforts:[]},
];
assert.deepEqual(roles.roleCapabilities(providers,'3',{}).reasoning_efforts,['disabled','enabled']);
assert.deepEqual(roles.roleCapabilities(providers,null,{providerId:1,fallbackIds:[2]}).reasoning_efforts,['low','high','max']);
assert.deepEqual(roles.roleCapabilities(providers,null,{providerId:1,fallbackIds:[4]}).reasoning_efforts,[]);
assert.deepEqual(roles.roleCapabilities(providers,null,{providerId:1,mode:'rules',rules:[{provider_id:3}]}).reasoning_efforts,[]);
assert.deepEqual(roles.roleCapabilities(providers,null,{fallbackIds:[2]}).reasoning_efforts,[]);
assert.deepEqual(roles.roleCapabilities(providers,'99',{}).reasoning_efforts,[]);
''')

    def test_chat_uses_server_labels_and_toggle_values_without_extra_stops(self):
        self.run_js('''
const model={provider_id:1,model:'thinking-only',reasoning_supported:true,reasoning_control:'thinking_toggle',
 reasoning_efforts:['disabled','enabled'],reasoning_effort:'enabled',reasoning_effort_labels:{disabled:'关闭思考',enabled:'开启思考'}};
assert.deepEqual(chat.reasoningSlider(model,{}).efforts,['disabled','enabled']);
assert.equal(chat.reasoningSlider(model,{}).label,'开启思考');
assert.equal(chat.reasoningSlider(model,{reasoning_effort:'disabled'}).label,'关闭思考');
assert.equal(chat.reasoningSlider(model,{reasoning_effort:'max'}).value,'enabled');
assert.equal(chat.reasoningLabel('enabled'),'开启');
''')


if __name__ == '__main__':
    unittest.main()
