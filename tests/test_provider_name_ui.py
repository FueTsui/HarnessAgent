"""Exercise the single model-name field and its API payload in the real admin functions."""
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProviderNameUiTests(unittest.TestCase):
    def run_js(self, scenario):
        source = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        start = source.index("const resources = {")
        resources = source[start:source.index("\n};", start) + 3]
        functions = []
        for name in ("providerModelName", "providerName", "resourceName", "fieldValue", "fieldOptions", "renderField", "bindResourceForm", "collectResourcePayload", "saveResource", "resourceCard", "meta", "cardActions", "statusBadge"):
            match = re.search(r"(?:async )?function " + name + r"\(", source)
            tail = source[match.start():]
            end = re.search(r"\n(?:async )?function ", tail[1:])
            functions.append(tail[:end.start() + 1] if end else tail)
        harness = r'''
const assert = require('node:assert/strict');
const state = {activeResource:'providers', editingResource:null, resourceMode:'edit', providerGovernance:new Map(), providerPresets:{
  openai:{base_url:'https://openai.example/v1',model_id:'',model_name:'',wire_api:'responses'},
  anthropic:{base_url:'https://anthropic.example/v1',model_id:'claude-test',model_name:'Claude Test',wire_api:'messages'},
  chatgpt:{base_url:'https://chatgpt.com/backend-api/codex',model_id:'codex-test',model_name:'Codex',wire_api:'responses'},
}};
const inputs = new Map();
const $ = id => inputs.get(id);
const document = {querySelector:()=>null,querySelectorAll:()=>[]};
const escapeHtml = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const icon = () => '<svg></svg>';
const formRoot = {querySelector:()=>null,querySelectorAll:()=>[]};
const calls = [];
let serverError=null;
const api = async (url,options) => {calls.push({url,...options});if(serverError)throw serverError;return url.includes('discover-models')?{models:['discovered-id','second-id']}:{id:42};};
const refreshResourceCatalog = async()=>{};
const loadResource = async()=>{};
function setup(item=null) {
  inputs.clear(); calls.length=0;
  state.activeResource='providers'; state.editingResource=item;
  state.providerReasoningEditor={read:()=>({reasoning_config:{},reasoning_effort:''}),update(){},refresh:async()=>{}};
  for (const field of resources.providers.fields) {
    if (['section','reasoning-config'].includes(field.type)) continue;
    const value=fieldValue(field,item);
    const events={};
    inputs.set(`resource-field-${field.name}`, {
      value:field.type==='password'?'':String(value ?? ''),checked:!!value,required:!!field.required,disabled:false,
      options:(field.options||[]).map(([value,text])=>({value,text})), selectedIndex:0,
      querySelectorAll:()=>[], addEventListener:(name,callback)=>{(events[name]||=[]).push(callback)},
      async fire(name){for(const callback of events[name]||[])await callback({currentTarget:this})},
    });
  }
  for(const id of ['provider-detect-models','provider-model-result','provider-model-options','provider-detected-select','provider-use-model','resource-save','resource-error']) {
    const events={};
    inputs.set(id,{value:'',innerHTML:'',textContent:'',disabled:false,hidden:false,
      addEventListener:(name,callback)=>{(events[name]||=[]).push(callback)},
      async fire(name){for(const callback of events[name]||[])await callback({currentTarget:this})},
    });
  }
  inputs.set('resource-dialog',{close(){}});
  bindResourceForm(formRoot);
}
'''
        result = subprocess.run(
            ["node", "-"], input=harness + resources + "\n".join(functions) + "\n" + scenario,
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_form_has_one_required_model_name_and_legacy_fallback(self):
        self.run_js(r'''
setup();
const fields=resources.providers.fields;
assert.equal(fields.filter(field=>['name','model_name'].includes(field.name)).length,1);
const field=fields.find(field=>field.name==='model_name');
assert.equal(field.label,'模型名称');
assert.equal(field.required,true);
assert.match(renderField(field,null,false),/required/);
const variants=[
  [{name:'Old connection',model_name:'Friendly model',model_id:'real-id'},'Friendly model'],
  [{name:'Old connection',model_name:'  ',model_id:'real-id'},'Old connection'],
  [{name:'__personal_model_1_private',model_name:'',model_id:'real-id'},'real-id'],
  [{name:'',model_name:'',model_id:'real-id'},'real-id'],
];
for(const [item,expected] of variants) assert.equal(fieldValue(field,item),expected);
assert.match(renderField(field,{model_name:'<img src=x onerror=alert(1)>'},true),/&lt;img/);
state.activeResource='skills';
assert.equal(fieldValue({name:'name'},{name:'Skill name',model_name:'Unrelated'}),'Skill name');
''')

    def test_create_and_edit_save_one_name_without_changing_model_id_or_blank_secret(self):
        self.run_js(r'''
(async()=>{
  setup();
  $('resource-field-base_url').value='https://model.example/v1';
  $('resource-field-model_id').value='provider-deployment-v2';
  $('resource-field-model_name').value='  Friendly model  ';
  $('resource-field-api_key').value='synthetic-new-key';
  await saveResource();
  assert.equal(calls.length,1);
  assert.equal(calls[0].method,'POST');
  assert.equal(calls[0].url,'/api/v1/providers');
  assert.equal(calls[0].json.name,'Friendly model');
  assert.equal(calls[0].json.model_name,'Friendly model');
  assert.equal(calls[0].json.model_id,'provider-deployment-v2');
  assert.equal(calls[0].json.api_key,'synthetic-new-key');
  setup({id:7,name:'Old connection',model_name:'Friendly model',model_id:'provider-deployment-v2',base_url:'https://model.example/v1'});
  $('resource-field-model_name').value='Renamed model';
  await saveResource();
  assert.equal(calls[0].method,'PATCH');
  assert.equal(calls[0].url,'/api/v1/providers/7');
  assert.equal(calls[0].json.name,'Renamed model');
  assert.equal(calls[0].json.model_name,'Renamed model');
  assert.equal(calls[0].json.model_id,'provider-deployment-v2');
  assert.ok(!('api_key' in calls[0].json));
  serverError=new Error('提供商名称已存在');
  await saveResource();
  assert.equal($('resource-error').textContent,'模型名称已存在');
  serverError=null;
  calls.length=0;
  $('resource-field-model_name').value='  ';
  await saveResource();
  assert.equal(calls.length,0);
  assert.match($('resource-error').textContent,/模型名称必填/);
})().catch(error=>{console.error(error);process.exitCode=1});
''')

    def test_model_cards_agent_labels_and_delete_prompts_use_same_name(self):
        self.run_js(r'''
const item={id:12,name:'Old connection',model_name:'My Astra',model_id:'real-deployment',base_url:'https://model.example/v1',enabled:true};
state.providers=[item];
assert.equal(providerName(12),'My Astra');
assert.equal(resourceName(item),'My Astra');
const card=resourceCard(item,'providers');
assert.match(card,/<h3>My Astra<\/h3>/);
assert.match(card,/模型 ID/);
assert.match(card,/real-deployment/);
assert.doesNotMatch(card,/Old connection/);
const privateItem={...item,name:'__personal_model_3_private',model_name:''};
assert.doesNotMatch(resourceCard(privateItem,'providers'),/__personal_model_/);
assert.equal(providerName(null),'默认模型');
assert.equal(providerName(100),'#100');
''')

    def test_protocol_presets_and_discovery_preserve_user_name(self):
        self.run_js(r'''
(async()=>{
  setup();
  $('resource-field-model_name').value='My chosen name';
  $('resource-field-provider_type').value='anthropic';
  await $('resource-field-provider_type').fire('change');
  assert.equal($('resource-field-model_name').value,'My chosen name');
  assert.equal($('resource-field-wire_api').value,'messages');
  await $('provider-detect-models').fire('click');
  assert.equal(calls[0].url,'/api/v1/providers/discover-models');
  $('provider-detected-select').value='discovered-id';
  $('provider-use-model').onclick();
  assert.equal($('resource-field-model_id').value,'discovered-id');
  assert.equal($('resource-field-model_name').value,'My chosen name');
  assert.equal(collectResourcePayload().name,'My chosen name');
  $('resource-field-model_name').value='   ';
  $('provider-use-model').onclick();
  assert.equal($('resource-field-model_name').value,'discovered-id');
  $('resource-field-model_name').value='';
  await $('resource-field-provider_type').fire('change');
  assert.equal($('resource-field-model_name').value,'Claude Test');
})().catch(error=>{console.error(error);process.exitCode=1});
''')

    def test_imported_chatgpt_and_personal_identity_are_preserved(self):
        self.run_js(r'''
setup({id:8,name:'My Codex connection',model_name:'My Astra',model_id:'codex-deployment',provider_type:'chatgpt',base_url:'https://chatgpt.com/backend-api/codex'});
assert.equal($('resource-field-provider_type').disabled,true);
assert.equal($('resource-field-model_name').value,'My Astra');
let payload=collectResourcePayload();
assert.equal(payload.name,'My Astra');
assert.equal(payload.model_name,'My Astra');
assert.equal(payload.provider_type,'chatgpt');
assert.equal(payload.model_id,'codex-deployment');
setup({id:9,name:'__personal_model_3_private',model_name:'',model_id:'personal-deployment',base_url:'https://model.example/v1'});
assert.equal($('resource-field-model_name').value,'personal-deployment');
$('resource-field-model_name').value='My personal model';
payload=collectResourcePayload();
assert.equal(payload.name,'__personal_model_3_private');
assert.equal(payload.model_name,'My personal model');
assert.equal(payload.model_id,'personal-deployment');
''')


if __name__ == "__main__":
    unittest.main()
