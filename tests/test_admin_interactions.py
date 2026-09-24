"""Behavior checks for admin filtering, safe rendering and request ownership."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AdminInteractionTests(unittest.TestCase):
    def run_js(self, body):
        setup = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('frontend/static/admin.js', 'utf8');
function extract(name) {
  const start = source.search(new RegExp('(?:async )?function ' + name + '\\('));
  const tail = source.slice(start);
  const end = tail.slice(1).search(/\n(?:async )?function |\nconst tabModuleKey/);
  return end < 0 ? tail : tail.slice(0, end + 1);
}
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'', textContent:'', value:'', hidden:false,
    dataset:{}, classList:{toggle(){}, add(){}}, querySelectorAll(){return []}});
  return elements.get(id);
};
const state = {resourceRequest:0, activeResource:null, currentTab:'providers',
  resourceQuery:'', resourceFilter:'all', resourceRows:[], providerGovernance:new Map()};
const resources = {providers:{title:'模型',endpoint:'/providers'},skills:{title:'技能',endpoint:'/skills'}};
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = () => '<svg></svg>';
const statusBadge = enabled => enabled ? 'enabled' : 'disabled';
const formatAuditTime = value => value || '—';
const handleResourceAction = () => {};
let renderCount = 0;
const renderResourceCollection = () => {renderCount++; $('resource-grid').innerHTML=state.resourceRows.map(row=>row.name).join(',');};
'''
        result = subprocess.run(
            ["node", "-e", setup + "\n" + body], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_filters_search_names_and_files_and_respect_visibility(self):
        self.run_js(r'''
eval(extract('matchesCollection'));
assert(matchesCollection({name:'PDF toolkit',enabled:true}, 'pdf', 'enabled'));
assert(!matchesCollection({name:'PDF toolkit',enabled:false}, 'pdf', 'enabled'));
assert(matchesCollection({name:'产品知识',files:['使用指南.pdf'],is_public:false}, '指南', 'disabled', 'knowledge'));
assert(!matchesCollection({name:'secret',is_public:false}, '', 'public'));
assert(!matchesCollection({name:'model',api_key:'confidential'}, 'confidential', 'all'));
''')

    def test_tool_routes_follow_granted_modules_and_cannot_open_root_pages(self):
        self.run_js(r'''
const granted = new Set(['mcp']);
const Auth = {canModule: name => granted.has(name)};
const document = {querySelectorAll: () => [
  {dataset:{tab:'tools'},style:{display:''}},
  {dataset:{tab:'services'},style:{display:'none'}},
  {dataset:{tab:'users'},style:{display:'none'}},
]};
eval(source.match(/const toolPages = [^\n]+/)[0].replace('const toolPages', 'global.toolPages'));
eval(extract('canOpenAdminPage'));
assert(canOpenAdminPage('tools'));
assert(canOpenAdminPage('mcp'));
for (const page of ['capabilities','skills','http-services','program-services','users','unknown']) assert(!canOpenAdminPage(page), page);
granted.clear();
assert(!canOpenAdminPage('tools'));
granted.add('services');
assert(canOpenAdminPage('http-services'));
assert(canOpenAdminPage('program-services'));
assert(!canOpenAdminPage('mcp'));
''')

    def test_category_navigation_remembers_deep_links_and_skips_forbidden_pages(self):
        self.run_js(r'''
const granted = new Set(['mcp']);
const Auth = {canModule:name=>granted.has(name)};
const tabs = [
  {dataset:{tab:'preferences'},style:{display:''}},
  {dataset:{tab:'projects'},style:{display:''}},
  {dataset:{tab:'agents'},style:{display:'none'}},
  {dataset:{tab:'tools'},style:{display:''}},
  {dataset:{tab:'users'},style:{display:'none'}},
];
const groups = {
  personal:{querySelectorAll:()=>tabs.slice(0,2)},
  create:{querySelectorAll:()=>tabs.slice(2,4)},
  manage:{querySelectorAll:()=>tabs.slice(4)},
};
const document = {
  querySelector:selector=>groups[(selector.match(/data-admin-category="([^"]+)"/)||[])[1]],
  querySelectorAll:()=>tabs,
};
state.categoryPages={};
eval(source.match(/const toolPages = [^\n]+/)[0].replace('const toolPages','global.toolPages'));
eval(extract('canOpenAdminPage'));
eval(extract('adminCategoryPage'));
assert.equal(adminCategoryPage('personal'),'preferences');
assert.equal(adminCategoryPage('create'),'tools');
assert.equal(adminCategoryPage('manage'),null);
state.categoryPages.personal='projects/12';
assert.equal(adminCategoryPage('personal'),'projects/12');
state.categoryPages.create='mcp';
assert.equal(adminCategoryPage('create'),'mcp');
granted.clear();
tabs[3].style.display='none';
assert.equal(adminCategoryPage('create'),null);
''')

    def test_deep_tool_route_selects_its_category_without_exposing_other_groups(self):
        self.run_js(r'''
const groups = ['personal','create','optimize','manage'].map(name=>({dataset:{adminCategory:name},hidden:true,offsetLeft:0,scrollLeft:0,clientWidth:160}));
const buttons = groups.map(group=>({dataset:{category:group.dataset.adminCategory},classList:{toggle(){}},attributes:{},setAttribute(k,v){this.attributes[k]=v},removeAttribute(k){delete this.attributes[k]}}));
const tab = {offsetLeft:250,offsetWidth:60,parentElement:groups[1],closest:()=>groups[1]};
const document = {
  querySelector:selector=>{assert.equal(selector,'.tab[data-tab="tools"]');return tab;},
  querySelectorAll:selector=>selector==='[data-admin-category]'?groups:buttons,
};
state.categoryPages={};
eval(source.match(/const toolPages = [^\n]+/)[0].replace('const toolPages','global.toolPages'));
eval(extract('syncAdminNavigation'));
syncAdminNavigation('mcp','mcp');
assert.deepEqual(groups.map(group=>group.hidden),[true,false,true,true]);
assert.equal(state.categoryPages.create,'mcp');
assert.equal(buttons[1].attributes['aria-current'],'true');
assert.equal(buttons[0].attributes['aria-current'],undefined);
assert.equal(groups[1].scrollLeft,150);
''')

    def test_navigation_arrow_keys_focus_only_visible_permitted_items(self):
        self.run_js(r'''
let focused='',scrolled='',prevented=0;
const make=(name,display='',hidden=false)=>({name,style:{display},hidden:false,closest:()=>hidden?{}:null,focus(){focused=name},scrollIntoView(){scrolled=name}});
const first=make('first'),denied=make('denied','none'),inactive=make('inactive','',true),last=make('last');
const buttons=[first,denied,inactive,last];
const event={key:'ArrowRight',currentTarget:first,preventDefault(){prevented++}};
eval(extract('adminNavigationKey'));
adminNavigationKey(event,buttons);
assert.equal(focused,'last');assert.equal(scrolled,'last');assert.equal(prevented,1);
event.key='ArrowRight';event.currentTarget=last;adminNavigationKey(event,buttons);assert.equal(focused,'first');
event.key='End';adminNavigationKey(event,buttons);assert.equal(focused,'last');
event.key='ArrowDown';event.currentTarget=first;adminNavigationKey(event,buttons,true);assert.equal(focused,'last');
''')

    def test_table_escapes_user_content_and_hides_unowned_actions(self):
        self.run_js(r'''
eval(extract('renderResourceTable'));
const output = renderResourceTable([{id:7,name:'<img src=x onerror=alert(1)>',prefix:'abc',is_active:true,can_manage:false}], 'keys');
assert(output.includes('&lt;img'));
assert(!output.includes('<img'));
assert(!output.includes('data-resource-action'));
assert(output.includes('scope="col"'));
''')

    def test_late_resource_response_cannot_replace_current_module(self):
        self.run_js(r'''
const pending = new Map();
const api = url => new Promise((resolve,reject)=>pending.set(url,{resolve,reject}));
eval(extract('loadResource'));
(async()=>{
  const first = loadResource('providers');
  state.currentTab = 'skills';
  const second = loadResource('skills');
  pending.get('/skills').resolve([{name:'current skills'}]);
  await second;
  pending.get('/providers').resolve([{name:'stale provider'}]);
  await first;
  assert.equal(state.activeResource,'skills');
  assert.equal($('resource-grid').innerHTML,'current skills');
  assert.equal(renderCount,1);
  assert.equal(pending.size,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_resource_error_renders_retry_without_unhandled_rejection(self):
        self.run_js(r'''
const api = async()=>{throw new Error('<network unavailable>');};
eval(extract('loadResource'));
(async()=>{
  state.currentTab = 'skills';
  await loadResource('skills');
  assert($('resource-grid').innerHTML.includes('&lt;network unavailable&gt;'));
  assert.equal(typeof $('resource-retry').onclick,'function');
  assert.equal(renderCount,0);
})().catch(error=>{console.error(error);process.exitCode=1;});
''')

    def test_completed_background_action_cannot_reopen_previous_module(self):
        self.run_js(r'''
let requests = 0;
const api = async()=>{requests++;return [];};
eval(extract('loadResource'));
(async()=>{
  state.currentTab = 'skills';
  state.activeResource = 'skills';
  $('resource-grid').innerHTML = 'current skills';
  await loadResource('providers');
  assert.equal(requests,0);
  assert.equal(state.activeResource,'skills');
  assert.equal($('resource-grid').innerHTML,'current skills');
})().catch(error=>{console.error(error);process.exitCode=1;});
''')


if __name__ == '__main__':
    unittest.main()
