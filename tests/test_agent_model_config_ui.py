"""Behavior contracts for model-role settings and ordered fallbacks."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AgentModelConfigUITests(unittest.TestCase):
    def run_js(self, body):
        setup = r'''
const assert = require('node:assert/strict');
const config = require('./frontend/static/agent-model-config.js');
function form(extra = {}) {
  return {
    mode:'fixed', providerId:5, strategy:'ordered', fallbackIds:[], rules:'[]',
    health:{enabled:true,lookback_minutes:60,min_samples:3,max_error_rate:0.6,consecutive_failures:3},
    roles:{router:{provider_id:9,reasoning_effort:'low',max_tokens:800},planner:{provider_id:null,reasoning_effort:'',max_tokens:''},critic:{provider_id:null,reasoning_effort:'',max_tokens:null}},
    planning:'auto', toolRouting:'model', confidence:0.7, review:'on_failure', ...extra,
  };
}
'''
        result = subprocess.run(
            ["node", "-e", setup + body], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reopening_editor_resets_the_actual_scroll_container(self):
        self.run_js(r'''
const source = require('node:fs').readFileSync('frontend/static/admin.js','utf8');
const start = source.indexOf('function openAgent(');
const end = source.indexOf('\nasync function saveAgent(',start);
const panel = {scrollTop:420};
let shown=false, selectedTab=false;
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id,{value:'',checked:false,textContent:'',innerHTML:'',
    showModal(){assert(selectedTab);shown=true;},
    querySelector(selector){assert(shown);assert.equal(selector,'.agent-editor-content');return panel;}});
  return elements.get(id);
};
const state = {agents:[{id:1,name:'Existing',skill_ids:[],mcp_ids:[],agent_ids:[],builtin_tools:[]}],providers:[],skills:[],mcp:[],capabilities:[]};
const Auth = {role:()=> 'admin'};
const AgentModelConfig = {load(){selectedTab=true;}};
const choiceHtml = () => '';
const escapeHtml = value => String(value);
eval(source.slice(start,end));
openAgent(1);
assert.equal(panel.scrollTop,0);
panel.scrollTop=900;
openAgent(1);
assert.equal(panel.scrollTop,0);
''')

    def test_fixed_mode_keeps_stage_roles_and_execution_preferences(self):
        self.run_js(r'''
const saved = config.buildRouting({}, form());
assert.equal(saved.version,2);
assert.equal(saved.mode,'fixed');
assert.deepEqual(saved.roles.router,{provider_id:9,reasoning_effort:'low',max_tokens:800});
assert.deepEqual(saved.roles.planner,{provider_id:null,reasoning_effort:'',max_tokens:null});
assert.deepEqual(saved.planning,{mode:'auto'});
assert.deepEqual(saved.tool_routing,{mode:'model',confidence_threshold:0.7});
assert.deepEqual(saved.review,{mode:'on_failure'});
''')

    def test_model_names_render_in_execution_roles_and_fallbacks_without_changing_ids(self):
        self.run_js(r'''
const elements=new Map();
function element(id) {
  if(!elements.has(id)) elements.set(id,{
    value:'',checked:false,disabled:false,textContent:'',html:'',querySelectorAll:()=>[],
    set innerHTML(html) {
      this.html=html;
      if(html.startsWith('<option')) this.value=html.match(/<option value="([^"]*)"[^>]* selected[^>]*>/)?.[1] || '';
      for(const match of html.matchAll(/<select id="([^"]+)"[^>]*>(.*?)<\/select>/gs)) element(match[1]).innerHTML=match[2];
      for(const match of html.matchAll(/<input id="([^"]+)"[^>]*value="([^"]*)"/g)) element(match[1]).value=match[2];
    },get innerHTML(){return this.html;},
  });
  return elements.get(id);
}
global.document={getElementById:element,querySelectorAll:()=>[]};
const catalog=[
  {id:1,name:'Old connection',model_name:'My Astra',model_id:'astra-api-id'},
  {id:2,name:'Legacy model',model_name:'  ',model_id:'legacy-api-id'},
  {id:3,name:'__personal_model_1_private',model_name:'',model_id:'private-api-id'},
  {id:4,name:'Old unsafe',model_name:'<img src=x onerror=alert(1)>',model_id:'safe-api-id'},
  {id:5,name:'Disabled connection',model_name:'Disabled model',model_id:'disabled-api-id',enabled:false},
];
const agent={provider_id:1,routing:{mode:'fixed',fallback_provider_ids:[3,2],roles:{router:{provider_id:2},planner:{provider_id:3},critic:{provider_id:4}}}};
config.load(agent,catalog);
const options=element('agent-provider').innerHTML;
assert.match(options,/<option value="1" selected[^>]*>My Astra · astra-api-id<\/option>/);
assert.match(options,/Legacy model · legacy-api-id/);
assert.match(options,/>private-api-id<\/option>/);
assert.doesNotMatch(options,/Old connection|__personal_model_|<img/);
assert.match(options,/&lt;img/);
assert.doesNotMatch(options,/Disabled model/);
const roleHtml=element('agent-model-roles').innerHTML;
assert.match(roleHtml,/My Astra · astra-api-id/);
assert.equal(element('agent-router-provider').value,'2');
assert.equal(element('agent-planner-provider').value,'3');
assert.equal(element('agent-critic-provider').value,'4');
const fallbackHtml=element('agent-provider-fallbacks').innerHTML;
assert.ok(fallbackHtml.indexOf('value="3" checked')<fallbackHtml.indexOf('value="2" checked'));
assert.match(fallbackHtml,/aria-label="下移 private-api-id"/);
assert.match(fallbackHtml,/aria-label="上移 Legacy model"/);
assert.doesNotMatch(fallbackHtml,/__personal_model_|Old connection|<img/);
const saved=config.read(agent.routing,1);
assert.deepEqual(saved.fallback_provider_ids,[3,2]);
assert.deepEqual(Object.values(saved.roles).map(role=>role.provider_id),[2,3,4]);
assert.equal(saved.default_provider_id,1);
config.load({provider_id:5,routing:{fallback_provider_ids:[99]}},catalog);
assert.match(element('agent-provider').innerHTML,/<option value="5" selected disabled>Disabled model · disabled-api-id/);
assert.match(element('agent-provider-fallbacks').innerHTML,/不可用模型 #99/);
''')

    def test_fixed_mode_ignores_but_preserves_inactive_policy_default(self):
        self.run_js(r'''
const agent = {provider_id:5,routing:{mode:'fixed',default_provider_id:9,fallback_provider_ids:[5,12]}};
assert.equal(config.executionProviderId(agent),5);
const saved = config.buildRouting(agent.routing,form({fallbackIds:[5,12]}));
assert.equal(saved.default_provider_id,9);
assert.deepEqual(saved.fallback_provider_ids,[5,12]);
assert.equal(config.executionProviderId({provider_id:null,routing:{mode:'fixed',default_provider_id:9}}),null);
assert.equal(config.executionProviderId({provider_id:5,routing:{}}),5);
for (const mode of ['rules','policy']) assert.equal(config.executionProviderId({provider_id:5,routing:{mode,default_provider_id:9}}),9);
''')

    def test_saved_agent_reports_refresh_failure_without_reopening_or_hidden_error(self):
        self.run_js(r'''
const source = require('node:fs').readFileSync('frontend/static/admin.js','utf8');
eval(source.slice(source.indexOf('function agentBindingUpdate('), source.indexOf('\nfunction checked(')));
const start = source.indexOf('async function saveAgent()');
const end = source.indexOf('\nasync function openVersions(',start);
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id,{value:'',checked:false,textContent:'',disabled:false,close(){this.closed=true;},focus(){}});
  return elements.get(id);
};
const state = {editingAgent:{id:17,routing:{}},capabilities:[]};
const checked = () => [];
const Auth = {role:()=> 'admin'};
const AgentModelConfig = {read:()=>({version:2,mode:'fixed'}),selectTab(){},focusError(){}};
const notices = [];
const showToast = value => notices.push(value);
let writes = 0, rejectWrite = false;
const api = async () => { writes++; if (rejectWrite) throw new Error('save unavailable'); return {}; };
const loadAgents = async () => { throw new Error('list unavailable'); };
eval(source.slice(start,end));
(async()=>{
  $('agent-name').value='Existing assistant';
  $('agent-provider').value='5';
  await saveAgent();
  assert.equal(writes,1);
  assert.equal($('agent-dialog').closed,true);
  assert.equal($('agent-error').textContent,'');
  assert.equal($('agent-save').disabled,false);
  assert(notices.some(value=>value==='智能体配置已保存'));
  assert(!notices.some(value=>value.includes('已发布')));
  assert(notices.some(value=>value.includes('list unavailable')));
  rejectWrite=true;
  $('agent-dialog').closed=false;
  $('agent-name').value='Unsaved edits';
  await saveAgent();
  assert.equal($('agent-dialog').closed,false);
  assert.equal($('agent-error').textContent,'save unavailable');
  assert.equal($('agent-name').value,'Unsaved edits');
  assert.equal($('agent-save').disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_skill_and_service_ids_are_not_confused_with_agent_identity(self):
        self.run_js(r'''
const source = require('node:fs').readFileSync('frontend/static/admin.js','utf8');
const escapeHtml = value => String(value);
eval(source.slice(source.indexOf('function choiceHtml('),source.indexOf('\nfunction agentBindingUpdate(')));
const item = [{id:1,name:'Existing attachment'}];
assert(choiceHtml(item,[1],'agent_skill').includes('value="1"\n      checked'));
assert(choiceHtml(item,[1],'agent_mcp').includes('value="1"\n      checked'));
assert(!choiceHtml(item,[],'agent_child',1).includes('type="checkbox"'));
// The editor must pass its own ID only to the child-agent picker.
assert(source.includes('choiceHtml(state.skills, agent?.skill_ids, "agent_skill")'));
assert(source.includes('choiceHtml(state.mcp, agent?.mcp_ids, "agent_mcp")'));
''')

    def test_missing_catalog_and_untouched_capabilities_are_never_cleared(self):
        self.run_js(r'''
const source = require('node:fs').readFileSync('frontend/static/admin.js','utf8');
eval(source.slice(source.indexOf('function agentBindingUpdate('),source.indexOf('\nfunction checked(')));
assert.equal(agentBindingUpdate([1],[],[]),undefined);
assert.equal(agentBindingUpdate([1],[1],[1]),undefined);
assert.equal(agentBindingUpdate(['disabled','missing'],[],[]),undefined);
assert.deepEqual(agentBindingUpdate([1,3],[1,2],[1,2]),[3,1,2]);
assert.deepEqual(agentBindingUpdate([1],[1],[]),[]);
assert.deepEqual(agentBindingUpdate(undefined,[1],[1]),[1]);
assert.equal(JSON.stringify({skill_ids:agentBindingUpdate([1],[],[])}),'{}');
''')

    def test_advanced_fields_and_rules_survive_ordinary_role_edit(self):
        self.run_js(r'''
const original = {mode:'policy',strategy:'lowest_cost',fallback_provider_ids:[12,7],
  rules:[{when:{image:true},provider_id:18,fallback_provider_ids:[19]}],
  health:{enabled:false,lookback_minutes:90,custom_threshold:7},future_policy:{a:1}};
const snapshot = JSON.stringify(original);
const saved = config.buildRouting(original, form({mode:'policy',strategy:'lowest_cost',fallbackIds:[12,7],rules:JSON.stringify(original.rules),health:{enabled:false,lookback_minutes:90,min_samples:3,max_error_rate:0.6,consecutive_failures:3}}));
assert.deepEqual(saved.rules,original.rules);
assert.deepEqual(saved.fallback_provider_ids,[12,7]);
assert.equal(saved.health.enabled,false);
assert.equal(saved.health.custom_threshold,7);
assert.deepEqual(saved.future_policy,{a:1});
assert.equal(JSON.stringify(original),snapshot);
saved.rules[0].when.image=false;
assert.equal(original.rules[0].when.image,true);
''')

    def test_fallback_reordering_is_persisted_without_catalog_resorting(self):
        self.run_js(r'''
const existing = [21,4,12];
const reordered = config.moveFallback(existing,12,-1);
assert.deepEqual(reordered,[21,12,4]);
assert.deepEqual(existing,[21,4,12]);
assert.deepEqual(config.moveFallback(reordered,21,-1),reordered);
const saved = config.buildRouting({}, form({mode:'policy',fallbackIds:[...reordered,21,5]}));
assert.deepEqual(saved.fallback_provider_ids,[21,12,4]);
''')

    def test_validation_rejects_invalid_values_and_keeps_zero_confidence(self):
        self.run_js(r'''
assert.equal(config.buildRouting({},form({confidence:0})).tool_routing.confidence_threshold,0);
for (const bad of [1.1,-1,'nope','']) assert.throws(()=>config.buildRouting({},form({confidence:bad})),error=>error.field==='agent-router-confidence');
for (const bad of ['{','{}','[null]','[[1]]']) assert.throws(()=>config.buildRouting({},form({rules:bad})),error=>error.field==='agent-route-rules');
for (const bad of [0,-1,1.5,1000001,'nope']) {
  const input = form(); input.roles.planner.max_tokens=bad;
  assert.throws(()=>config.buildRouting({},input),error=>error.field==='agent-planner-tokens');
}
for (const mode of ['rules','policy','fixed']) assert.equal(config.buildRouting({},form({mode})).mode,mode);
''')


if __name__ == "__main__":
    unittest.main()
