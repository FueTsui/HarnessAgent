"""Behavior checks for conversation preferences and the settings boundary."""
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")


def function_source(start, end):
    return APP[APP.index(start):APP.index(end)]


class ChatWorkspaceTests(unittest.TestCase):
    def run_js(self, scenario, extra=""):
        result = subprocess.run(
            ["node", "-"], cwd=ROOT, text=True, encoding="utf-8",
            input=(
                'const assert = require("node:assert/strict");\n'
                'const ChatWorkspace = require("./frontend/static/chat-workspace.js");\n'
                + extra + '\n(async () => {\n' + scenario
                + '\n})().catch(error => { console.error(error); process.exitCode = 1; });'
            ), capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_composer_model_labels_use_names_without_changing_routing_identity(self):
        extra = function_source("function composerModelLabel", "function positionComposerPopover")
        self.run_js(r'''
for (const providerId of [null, 8]) {
  const model = {provider_id:providerId, name:"智能体默认", model:"gpt-6-astra", model_name:"  GPT-6 Astra  "};
  const before = JSON.stringify(model);
  assert.equal(composerModelLabel(model), "GPT-6 Astra");
  assert.equal(JSON.stringify(model), before);
}
assert.equal(composerModelLabel({model:"legacy-id",name:"旧模型名称",model_name:"  "}), "旧模型名称");
assert.equal(composerModelLabel({model:"wire-id",name:"智能体默认"}), "wire-id");
assert.equal(composerModelLabel({model:"wire-id",name:"__personal_model_1_private"}), "wire-id");
assert.equal(composerModelLabel({model:"wire-id",model_name:"显示名称",available:false}), "暂无可用模型");
assert.equal(composerModelLabel(null), "智能体自动");
const automatic={provider_id:7,name:"模型别名",model_name:"GPT-6 Astra",model:"gpt-6-astra"};
const catalog=ChatWorkspace.normalizeModelCatalog({default:automatic,items:[{...automatic}]});
assert.equal(composerModelLabel(catalog[0]),"GPT-6 Astra");
assert.equal(catalog[0].provider_id,null);
assert.equal(catalog[0].automatic_provider_id,7);
assert.equal(catalog[0].model,"gpt-6-astra");
assert.equal(catalog[1].provider_id,7);
''', extra)

    def test_account_theme_updates_same_server_preference_for_member_and_guest(self):
        extra = function_source("async function loadPreferences", "function hasComposerOverrides")
        self.run_js(r'''
const controls = ["light","dark","system"].map(theme=>({dataset:{accountTheme:theme},attributes:{},setAttribute(key,value){this.attributes[key]=value;}}));
const group={attributes:{},setAttribute(key,value){this.attributes[key]=value;},querySelectorAll:()=>controls};
global.$=()=>group;
let applied="system",stored="system";
global.Theme={setAccent(){},current:()=>stored,apply:mode=>{applied=mode;}};
global.localStorage={setItem(key,value){assert.equal(key,"gca_theme");stored=value;}};
global.renderComposerControls=()=>{};
global.showToast=message=>{throw new Error(message);};
const initial={theme:"system",approval_policy:"ask",default_agent_id:null,recent_sort:"priority",revision:4};
for (const role of ["user","guest"]) {
  global.Auth={role:()=>role};
  global.state={sessionReady:true,themeSaving:false,preferences:{...initial},composerSelection:{approval_policy:"auto"}};
  let server={...initial};
  const requests=[];
  global.api=async(url,options)=>{
    assert.equal(url,"/api/v1/users/me/preferences");
    requests.push(options);
    if (options?.method === "PATCH") {
      assert.deepEqual(Object.keys(options.json).sort(),["revision","theme"]);
      assert.equal(options.json.revision,server.revision);
      server={...server,theme:options.json.theme,revision:server.revision+1};
    }
    return {...server};
  };
  for(const theme of ["light","dark","system"]) {
    await setAccountTheme(theme);
    assert.equal(applied,theme);
    assert.equal(stored,theme);
    assert.equal(state.preferences.theme,theme);
    assert.equal(state.composerSelection.approval_policy,"auto");
    assert.equal(controls.find(button=>button.attributes["aria-checked"]==="true").dataset.accountTheme,theme);
    assert.equal(group.attributes["aria-busy"],"false");
  }
  assert.equal(requests.length,6);
  const confirmed=state.preferences;
  applyPreferences({...initial,theme:"dark"});
  assert.equal(state.preferences,confirmed,"stale refresh cannot replace a confirmed save");
  assert.equal(applied,"system");
}
''', extra)

    def test_account_theme_failure_and_revision_conflict_do_not_claim_unsaved_choice(self):
        extra = function_source("async function loadPreferences", "function hasComposerOverrides")
        self.run_js(r'''
global.Auth={role:()=>"guest"};
const initial={theme:"system",approval_policy:"ask",default_agent_id:null,recent_sort:"priority",revision:1};
global.state={sessionReady:true,themeSaving:false,preferences:{...initial}};
global.$=()=>({setAttribute(){},querySelectorAll:()=>[]});
let applied="system";
global.Theme={setAccent(){},apply:theme=>{applied=theme;},current:()=>applied};
global.localStorage={setItem(){}};
global.renderComposerControls=()=>{};
const messages=[];
global.showToast=message=>messages.push(message);
let fail=Object.assign(new Error("设置已在其他页面更新"),{status:409});
let reads=0;
global.api=async(url,options)=>{
  if(options?.method==="PATCH") throw fail;
  reads++;
  return reads===1?{...initial}:{...initial,theme:"dark",revision:2};
};
await setAccountTheme("light");
assert.equal(reads,2);
assert.equal(applied,"dark");
assert.equal(state.preferences.theme,"dark");
assert.equal(state.themeSaving,false);
assert.match(messages[0],/外观切换失败/);
fail=new Error("服务暂不可用");
await setAccountTheme("light");
assert.equal(applied,"dark");
assert.equal(state.preferences.theme,"dark");
assert.equal(messages.length,2);
assert.equal(state.themeSaving,false);
''', extra)

    def test_account_theme_keyboard_navigation_includes_controls_without_hidden_links(self):
        extra = function_source("function accountMenuItems", "function setAccountMenu")
        extra += function_source('$("account-theme-options").onkeydown', '$("account-menu").addEventListener("focusout"')
        self.run_js(r'''
''', r'''
const focus=[];
const themes=["light","dark","system"].map(name=>({name,focus(){focus.push(name);}}));
const menu=[{name:"models"},{name:"admin",hidden:true},{name:"login"},...themes];
const group={querySelectorAll:()=>themes};
global.$=id=>id==="account-theme-options"?group:{querySelectorAll:()=>menu};
global.document={activeElement:themes[0]};
''' + extra + r'''
assert.deepEqual(accountMenuItems().map(item=>item.name),["models","login","light","dark","system"]);
const event=key=>({key,preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
const right=event("ArrowRight");
group.onkeydown(right);
assert.deepEqual(focus,["dark"]);
assert.equal(right.prevented,true);
const left=event("ArrowLeft");
group.onkeydown(left);
assert.deepEqual(focus,["dark","system"]);
''')

    def test_reasoning_slider_uses_only_supported_levels_and_does_not_invent_defaults(self):
        self.run_js(r'''
const model={provider_id:8,model:"supported-model",reasoning_supported:true,reasoning_effort:"low",
  reasoning_efforts:["none","low","medium","high","xhigh","max"]};
const inherited=ChatWorkspace.reasoningSlider(model,{reasoning_effort:""});
assert.deepEqual(inherited.efforts,model.reasoning_efforts);
assert.equal(inherited.index,1);
assert.equal(inherited.label,"轻度");
assert.equal(inherited.inherited,true);
const maximum=ChatWorkspace.reasoningSlider(model,{reasoning_effort:"max"});
assert.equal(maximum.label,"最高");
assert.equal(maximum.index,5);
assert.equal(maximum.inherited,false);
assert.equal(maximum.efforts.includes("ultra"),false);
const undeclared=ChatWorkspace.reasoningSlider({...model,reasoning_effort:""},{reasoning_effort:""});
assert.equal(undeclared.value,"");
assert.equal(undeclared.label,"默认");
assert.equal(undeclared.inherited,true);
assert.equal(ChatWorkspace.reasoningSlider(model,{reasoning_effort:"ultra"}).value,"low");
assert.equal(ChatWorkspace.reasoningLabel("ultra"),"Ultra");
for(const patch of [{reasoning_supported:false},{reasoning_efforts:[]},{available:false},{model:""}]) {
  assert.deepEqual(ChatWorkspace.reasoningSlider({...model,...patch},{}).efforts,[]);
}
const automatic={...model,provider_id:null,reasoning_efforts:["low","high"]};
assert.deepEqual(ChatWorkspace.reasoningSlider(automatic,{}).efforts,["low","high"]);
''')

    def test_reasoning_slider_selection_and_reset_keep_turn_options_and_dom_focus_stable(self):
        extra = function_source("function setReasoningState", "function emptyComposerContext")
        extra += function_source("function renderReasoningControl", "async function fetchComposerModels")
        reset = function_source('$("reasoning-reset").onclick', '$("reasoning-model-btn").onclick')
        self.run_js(r'''
const model={provider_id:8,model:"fixture",reasoning_supported:true,reasoning_effort:"low",reasoning_efforts:["low","medium","high","max"]};
global.state={models:[model],composerSelection:{provider_id:8,reasoning_effort:"",approval_policy:"auto"},submitting:false,modelsLoading:false};
global.composerModelLabel=value=>value?.model||"默认模型";
global.escapeHtml=value=>String(value);
global.setSendState=()=>{};
const pending=Object.freeze({...state.composerSelection,reasoning_effort:"medium"});
const originalSlider=$("reasoning-slider");
selectReasoningStep(3);
assert.equal(state.composerSelection.reasoning_effort,"max");
assert.equal(state.composerSelection.provider_id,8);
assert.equal(state.composerSelection.approval_policy,"auto");
assert.equal($("reasoning-current-label").textContent,"最高");
assert.equal($("reasoning-slider"),originalSlider);
assert.equal($("reasoning-slider").value,"3");
assert.equal($("reasoning-slider-wrap").attributes["data-energy"],"maximum");
assert.equal(($("reasoning-slider-energy").innerHTML.match(/class="reasoning-particle"/g)||[]).length,42);
const stableParticles=$("reasoning-slider-energy").innerHTML;
renderReasoningControl();
assert.equal($("reasoning-slider-energy").innerHTML,stableParticles);
assert.equal(pending.reasoning_effort,"medium");
selectReasoningStep(10);
assert.equal(state.composerSelection.reasoning_effort,"max");
$("reasoning-reset").onclick();
assert.equal(state.composerSelection.reasoning_effort,"");
assert.equal($("reasoning-current-label").textContent,"轻度");
assert.equal($("reasoning-slider").focused,true);
assert.equal($("reasoning-slider-wrap").attributes["data-energy"],"subtle");
model.reasoning_effort="";
renderReasoningControl();
assert.equal($("reasoning-current-label").textContent,"默认");
assert.match($("reasoning-slider").attributes["aria-valuetext"],/^默认/);
assert.equal($("reasoning-slider-wrap").attributes["data-energy"],"off");
assert.equal($("reasoning-slider-energy").innerHTML,"");
state.submitting=true;
selectReasoningStep(2);
assert.equal(state.composerSelection.reasoning_effort,"");
''', r'''
const elements={};
global.$=id=>elements[id] ||= {attributes:{},style:{setProperty(name,value){this[name]=value;}},classList:{toggle(){}},
  setAttribute(name,value){this.attributes[name]=value;},focus(){this.focused=true;}};
''' + extra + reset)

    def test_visual_grades_increase_without_inventing_model_capabilities(self):
        self.run_js(r'''
const values=["low","medium","high","xhigh","max","ultra"];
const names=["轻度","中","高","极高","最高","Ultra"];
const profiles=values.map(value=>ChatWorkspace.reasoningVisual(value));
values.forEach((value,i)=>{
  assert.equal(ChatWorkspace.reasoningLabel(value,{reasoning_effort_labels:{[value]:"旧标签"}}),names[i]);
  assert.ok(profiles[i].count>0 && profiles[i].count<=52);
  if(i) {
    assert.ok(profiles[i].count>profiles[i-1].count);
    assert.ok(profiles[i].power>profiles[i-1].power);
    assert.ok(profiles[i].duration<profiles[i-1].duration);
    assert.ok(profiles[i].drift>profiles[i-1].drift);
    assert.ok(profiles[i].drift/profiles[i].duration >= (i<=3 ? 2 : 1.8) * profiles[i-1].drift/profiles[i-1].duration);
  }
});
for(const value of ["",null,"none","disabled","unrecognized","__proto__","constructor"]){
  assert.equal(ChatWorkspace.reasoningVisual(value).count,0);
  assert.equal(ChatWorkspace.reasoningVisual(value).tier,"off");
}
const model={model:"astra",reasoning_supported:true,reasoning_effort:"medium",reasoning_efforts:values.slice(0,5)};
const slider=ChatWorkspace.reasoningSlider(model,{reasoning_effort:"ultra"});
assert.equal(slider.value,"medium");
assert.deepEqual(slider.efforts,values.slice(0,5));
''')

    def test_reasoning_popover_keeps_native_range_keys_and_escape_returns_focus(self):
        handler = function_source('$("reasoning-menu").onkeydown', '$("reasoning-menu").addEventListener("focusout"')
        self.run_js(r'''
for(const key of ["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Home","End"]) {
  const event={key,preventDefault(){throw new Error("Native range key intercepted");}};
  menu.onkeydown(event);
}
let prevented=false,stopped=false;
menu.onkeydown({key:"Escape",preventDefault(){prevented=true;},stopPropagation(){stopped=true;}});
assert.equal(prevented,true);
assert.equal(stopped,true);
assert.equal(restored,true);
''', r'''
const menu={};
let restored=false;
global.$=()=>menu;
global.closeComposerMenus=restore=>{restored=restore;};
''' + handler)

    def test_unchanged_first_slider_stop_can_be_explicitly_selected_and_reset(self):
        extra = function_source("function selectReasoningStep", "async function fetchComposerModels")
        handlers = function_source('$("reasoning-slider").oninput', '$("reasoning-model-btn").onclick')
        self.run_js(r'''
const slider=$("reasoning-slider");
const model={provider_id:2,model:"thinking-toggle",reasoning_supported:true,reasoning_effort:"",reasoning_efforts:["disabled","enabled"]};
global.state={models:[model],composerSelection:{provider_id:2,reasoning_effort:""},submitting:false,modelsLoading:false};
global.renderReasoningControl=()=>{};
global.setSendState=()=>{};
slider.value="0";
slider.onkeyup({key:"Home",currentTarget:slider});
assert.equal(state.composerSelection.reasoning_effort,"disabled");
$("reasoning-reset").onclick();
assert.equal(state.composerSelection.reasoning_effort,"");
slider.onpointerup({button:0,currentTarget:slider});
assert.equal(state.composerSelection.reasoning_effort,"disabled");
$("reasoning-reset").onclick();
slider.onkeyup({key:"Tab",currentTarget:slider});
slider.onpointerup({button:2,currentTarget:slider});
assert.equal(state.composerSelection.reasoning_effort,"");
slider.value="1";
slider.onkeyup({key:"End",currentTarget:slider});
assert.equal(state.composerSelection.reasoning_effort,"enabled");
state.submitting=true;
slider.value="0";
slider.onkeyup({key:"Home",currentTarget:slider});
slider.onpointerup({button:0,currentTarget:slider});
assert.equal(state.composerSelection.reasoning_effort,"enabled");
''', r'''
const elements={};
global.$=id=>elements[id] ||= {value:"0",focus(){}};
''' + extra + handlers)

    def test_reasoning_controls_unlock_after_submit_and_keep_unsupported_slider_disabled(self):
        extra = function_source("function setSendState", "function emptyComposerContext")
        self.run_js(r'''
global.state={sessionReady:true,agentId:2,submitting:true,modelsLoading:false,
  models:[{provider_id:9,model:"fixture",reasoning_supported:true,reasoning_efforts:["low","medium","max"]}],
  composerSelection:{provider_id:9,approval_policy:"auto",reasoning_effort:"max"},attachments:[],runningJob:null,
  selectedDatasets:new Set(),selectedTemplates:new Set(),selectedSkills:new Set(),selectedMcp:new Set(),selectedAgentCalls:new Set()};
const elements={};
global.$=id=>elements[id] ||= {querySelectorAll:()=>[]};
global.query={value:"testing"};
global.send={classList:{toggle(){}},setAttribute(){}};
global.icon=()=>"";
global.hasComposerOverrides=()=>true;
setSendState();
for(const id of ["model-btn","reasoning-slider","reasoning-reset","reasoning-model-btn"]) assert.equal($(id).disabled,true);
state.submitting=false;
state.runningJob="turn";
setSendState();
for(const id of ["model-btn","reasoning-slider","reasoning-reset","reasoning-model-btn"]) assert.equal($(id).disabled,false);
state.runningJob=null;
query.value="";
setSendState();
assert.equal($("reasoning-slider").disabled,false);
assert.equal(state.composerSelection.reasoning_effort,"max");
state.composerSelection.reasoning_effort="";
state.models[0].reasoning_supported=false;
setSendState();
assert.equal($("reasoning-slider").disabled,true);
assert.equal($("reasoning-reset").disabled,true);
assert.equal($("reasoning-model-btn").disabled,false);
''', extra)

    def test_composer_popup_internal_focus_changes_do_not_swallow_model_clicks(self):
        extra = function_source("function closeComposerMenuOnFocusOut", "function renderComposerControls")
        self.run_js(r'''
const tasks=[];
let closed=0;
global.queueMicrotask=callback=>tasks.push(callback);
global.closeComposerMenus=()=>{closed++;};
global.document={activeElement:{name:"body"}};
const range={},modelLink={},firstModel={},astraModel={},reasoningTrigger={},modelTrigger={};
const reasoningMenu={hidden:false,contains:item=>[range,modelLink].includes(item)};
const modelMenu={hidden:false,contains:item=>[firstModel,astraModel].includes(item)};
// Clicking a non-focusable <small> or rebuilding a row may blur to body with no
// relatedTarget. The upcoming click must survive without a queued close.
closeComposerMenuOnFocusOut({relatedTarget:null},modelMenu,modelTrigger);
assert.equal(tasks.length,0);
assert.equal(closed,0);
// Browser focusout may precede focusin: activeElement is temporarily body.
closeComposerMenuOnFocusOut({relatedTarget:modelLink},reasoningMenu,reasoningTrigger);
assert.equal(tasks.length,0,"range to model link must not close the popup before click");
closeComposerMenuOnFocusOut({relatedTarget:astraModel},modelMenu,modelTrigger);
assert.equal(tasks.length,0,"first model to Astra must preserve the upcoming click");
closeComposerMenuOnFocusOut({relatedTarget:modelTrigger},modelMenu,modelTrigger);
assert.equal(tasks.length,0);
// Moving from an old popup into the newly opened one cannot close the new menu.
closeComposerMenuOnFocusOut({relatedTarget:firstModel},reasoningMenu,reasoningTrigger);
reasoningMenu.hidden=true;
document.activeElement=firstModel;
tasks.splice(0).forEach(callback=>callback());
assert.equal(closed,0);
document.activeElement={name:"outside"};
closeComposerMenuOnFocusOut({relatedTarget:document.activeElement},modelMenu,modelTrigger);
tasks.splice(0).forEach(callback=>callback());
assert.equal(closed,1);
''', extra)

    def test_unified_model_control_switches_modes_in_one_popup_and_restores_same_trigger(self):
        extra = function_source("function closeComposerMenus", "function closeComposerMenuOnFocusOut")
        self.run_js(r'''
global.state={submitting:false,modelsLoading:false};
const elements={};
let focused=null;
global.$=id=>elements[id] ||= {id,hidden:true,disabled:false,attributes:{},classList:{add(){},remove(){},toggle(){}},
  setAttribute(key,value){this.attributes[key]=value;},focus(){focused=this.id;},
  querySelector(){return this.id==="model-menu"?$("first-model-option"):$("reasoning-model-btn");}};
global.closeAddMenu=global.closePalette=global.renderReasoningControl=global.positionComposerPopover=()=>{};
toggleComposerMenu("reasoning",true);
assert.equal($("reasoning-menu").hidden,false);
assert.equal($("model-menu").hidden,true);
assert.equal($("model-btn").attributes["aria-expanded"],"true");
assert.equal(focused,"reasoning-slider");
setComposerModelMode(true,true);
assert.equal($("reasoning-menu").hidden,false);
assert.equal($("model-menu").hidden,false);
assert.equal(focused,"first-model-option");
setComposerModelMode(false,true);
assert.equal($("reasoning-menu").hidden,false);
assert.equal($("model-menu").hidden,true);
assert.equal(focused,"reasoning-slider");
closeComposerMenus(true);
assert.equal($("reasoning-menu").hidden,true);
assert.equal(focused,"model-btn");
$("reasoning-slider").disabled=true;
toggleComposerMenu("reasoning",true);
assert.equal(focused,"reasoning-model-btn");
setComposerModelMode(true,true);
assert.equal(focused,"first-model-option");
assert.equal($("reasoning-menu").hidden,false);
''', extra)

    def test_invalid_or_unauthorized_preferences_fail_closed(self):
        self.run_js(r'''
const valid = {theme:"system",approval_policy:"ask",default_agent_id:null,recent_sort:"priority"};
assert.deepEqual(ChatWorkspace.normalizePreferences(valid, "user"), valid);
assert.equal(ChatWorkspace.normalizePreferences({...valid,approval_policy:"auto"}, "user").approval_policy,"auto");
assert.equal(ChatWorkspace.normalizePreferences({...valid,approval_policy:"full_access"}, "root").approval_policy,"full_access");
for (const patch of [{theme:"invalid"},{approval_policy:"invalid"},{default_agent_id:-1},
  {default_agent_id:"7"},{recent_sort:"invalid"}]) {
  assert.throws(() => ChatWorkspace.normalizePreferences({...valid,...patch},"root"));
}
assert.throws(() => ChatWorkspace.normalizePreferences({...valid,approval_policy:"full_access"},"user"));
assert.throws(() => ChatWorkspace.normalizePreferences(null,"root"));
''')

    def test_every_send_refreshes_defaults_without_importing_old_browser_model_choice(self):
        extra = function_source("async function loadPreferences", "function renderAgentMenu")
        extra += function_source("async function enqueueMessage", "async function enqueueQueuedMessage")
        self.run_js(r'''
global.state = {agentId:7,sessionId:"thread-1",activeProjectId:null,approvalPolicy:"full_access",providerId:999,
  models:[],composerSelection:ChatWorkspace.newComposerSelection()};
global.Auth = {role: () => "root"};
global.localStorage = {getItem(){throw new Error("Legacy browser preferences must not be read");},setItem(){}};
global.Theme = {setAccent(){},apply(){}};
renderComposerControls = renderAccountTheme = () => {};
const requests = [];
let policy = "ask";
global.api = async (url, options) => {
  requests.push({url,options});
  if (url.endsWith("/preferences")) return {theme:"dark",approval_policy:policy,default_agent_id:3,recent_sort:"updated"};
  if (url.includes("/models?")) return {default:{provider_id:null,reasoning_supported:false},items:[]};
  return {turn_id:"turn-1"};
};
const context = {datasets:[],templates:[],skills:[],mcpServers:[],agentCalls:[],attachments:[]};
await enqueueMessage("First",{...context});
policy = "auto";
await enqueueMessage("Second",{...context});
assert.deepEqual(requests.map(r=>r.url), ["/api/v1/users/me/preferences","/api/v1/chat/models?agent_id=7","/api/v1/chat","/api/v1/users/me/preferences","/api/v1/chat/models?agent_id=7","/api/v1/chat"]);
assert.equal(requests[2].options.body.get("approval_policy"),"ask");
assert.equal(requests[5].options.body.get("approval_policy"),"auto");
assert.equal(requests[2].options.body.get("agent_id"),"7");
assert.equal(requests[2].options.body.get("provider_id"),"");
assert.equal(requests[5].options.body.get("provider_id"),"");
assert.equal(state.recentSortMode,"updated");
''', extra)

    def test_settings_failure_never_posts_a_task_with_stale_approval(self):
        extra = function_source("async function loadPreferences", "function renderAgentMenu")
        extra += function_source("async function enqueueMessage", "async function enqueueQueuedMessage")
        self.run_js(r'''
global.state = {agentId:7,sessionId:null,activeProjectId:null,approvalPolicy:"full_access"};
global.Auth = {role: () => "root"};
global.localStorage = {setItem(){}};
global.Theme = {setAccent(){},apply(){}};
const requests = [];
global.api = async url => { requests.push(url); throw new Error("Preferences unavailable"); };
const context = {datasets:[],templates:[],skills:[],mcpServers:[],agentCalls:[],attachments:[]};
await assert.rejects(enqueueMessage("Hello",context),/Preferences unavailable/);
assert.deepEqual(requests,["/api/v1/users/me/preferences"]);
''', extra)

    def test_server_default_agent_takes_priority_over_old_browser_selection(self):
        extra = function_source("async function loadAgents", "async function loadAgentStatuses")
        self.run_js(r'''
global.state = {preferences:{default_agent_id:9},agents:[]};
global.api = async () => [{id:3,name:"System default",is_default:true},{id:9,name:"Personal default"}];
global.localStorage = {getItem(){throw new Error("Old active_agent_id is not authoritative");}};
global.$ = () => ({textContent:""});
global.renderAgentMenu = global.renderWelcome = global.setSendState = () => {};
global.loadModels = async () => {};
await loadAgents();
assert.equal(state.agentId,9);
state.preferences.default_agent_id = 100;
await loadAgents();
assert.equal(state.agentId,3);
''', extra)

    def test_explicit_composer_choices_are_frozen_and_survive_default_refresh(self):
        extra = function_source("async function loadPreferences", "function renderAgentMenu")
        extra += function_source("async function enqueueMessage", "async function enqueueQueuedMessage")
        self.run_js(r'''
global.state = {agentId:7,sessionId:null,activeProjectId:null,models:[],
  composerSelection:{approval_policy:"auto",provider_id:9,reasoning_effort:"high"}};
global.Auth = {role: () => "root"};
global.localStorage = {setItem(){}};
global.Theme = {setAccent(){},apply(){}};
renderComposerControls = renderAccountTheme = () => {};
const calls=[];
global.api = async (url,options) => {
  calls.push({url,options});
  if (url.endsWith("/preferences")) return {theme:"light",approval_policy:"ask",default_agent_id:null,recent_sort:"priority"};
  if (url.includes("/models?")) return {default:{provider_id:null},items:[{provider_id:9,reasoning_supported:true,reasoning_efforts:["low","high"]}]};
  return {turn_id:"turn-1"};
};
const context={agentId:7,selection:{...state.composerSelection},datasets:[],templates:[],skills:[],mcpServers:[],agentCalls:[],attachments:[]};
state.composerSelection={approval_policy:"ask",provider_id:null,reasoning_effort:""};
await enqueueMessage("Frozen",context);
const body=calls.at(-1).options.body;
assert.equal(body.get("provider_id"),"9");
assert.equal(body.get("reasoning_effort"),"high");
assert.equal(body.get("approval_policy"),"auto");
assert.equal(Object.isFrozen(context.runtimeOptions),true);
state.composerSelection=context.selection;
await loadPreferences();
assert.equal(state.approvalPolicy,"auto");
assert.equal(state.composerSelection.reasoning_effort,"high");
''', extra)

    def test_model_or_effort_revocation_prevents_silent_submission_with_different_options(self):
        extra = function_source("async function loadPreferences", "function renderAgentMenu")
        extra += function_source("async function enqueueMessage", "async function enqueueQueuedMessage")
        self.run_js(r'''
global.state = {agentId:7,sessionId:null,activeProjectId:null,models:[],
  composerSelection:{approval_policy:null,provider_id:9,reasoning_effort:"high"}};
global.Auth = {role: () => "root"};
global.localStorage = {setItem(){}};
global.Theme = {setAccent(){},apply(){}};
renderComposerControls = renderAccountTheme = () => {};
let posts=0;
global.api = async (url,options) => {
  if (url.endsWith("/preferences")) return {theme:"light",approval_policy:"ask",default_agent_id:null,recent_sort:"priority"};
  if (url.includes("/models?")) return {default:{provider_id:null,reasoning_supported:false},items:[]};
  posts++;return {};
};
const context={selection:{...state.composerSelection},datasets:[],templates:[],skills:[],mcpServers:[],agentCalls:[],attachments:[]};
await assert.rejects(enqueueMessage("Revoked",context),/所选模型已不可用/);
assert.equal(posts,0);
assert.equal(state.composerSelection.provider_id,null);
assert.equal(state.composerSelection.reasoning_effort,"");
const models=[{provider_id:null},{provider_id:9,reasoning_supported:true,reasoning_efforts:["high"]},
  {provider_id:10,reasoning_supported:true,reasoning_efforts:["low"]}];
assert.equal(ChatWorkspace.reconcileComposerSelection({provider_id:9,reasoning_effort:"high"},models).selection.reasoning_effort,"high");
assert.equal(ChatWorkspace.reconcileComposerSelection({provider_id:10,reasoning_effort:"high"},models).selection.reasoning_effort,"");
assert.throws(()=>ChatWorkspace.effectiveApproval({approval_policy:"ask"},{approval_policy:"full_access"},"user"));
''', extra)

    def test_next_turn_options_never_modify_running_guidance_or_guess_unknown_snapshot(self):
        extra = function_source("function beginComposerSubmission", "async function enqueueMessage")
        extra += function_source("async function submitDuringRun", "async function submit()")
        self.run_js(r'''
const running={agent_id:7,provider_id:9,reasoning_effort:"high",approval_policy:"ask",job_id:"running",guidance:[],status:"running"};
const desired={agent_id:7,provider_id:9,reasoning_effort:"high",approval_policy:"ask"};
assert.equal(ChatWorkspace.sameTurnOptions(running,desired),true);
assert.equal(ChatWorkspace.sameTurnOptions({...running,reasoning_effort:undefined},desired),false);
assert.equal(ChatWorkspace.sameTurnOptions({job_id:"legacy",approval_policy:"ask"},desired),false);
global.state={runningJob:"running",submitting:false,attachments:[],activeJobs:[running]};
global.query={value:"next"};
global.captureComposerContext=()=>({});
global.closeComposerMenus=global.setSendState=global.clearComposerContext=global.resizeComposer=global.renderStagedMessages=global.scrollToLatest=()=>{};
global.hasStructuredComposerContext=()=>false;
let queued=0,guidance=0;
const messages=[];
global.showToast=message=>messages.push(message);
global.prepareComposerSubmission=async context=>{context.runtimeOptions={...desired};return context.runtimeOptions};
global.enqueueQueuedMessage=async()=>{queued++};
global.api=async()=>{guidance++;return {id:"guidance"}};
await submitDuringRun();
assert.equal(guidance,1);assert.equal(queued,0);
desired.reasoning_effort="low";query.value="new settings";
await submitDuringRun();
assert.equal(guidance,1);assert.equal(queued,1);
assert.equal(running.reasoning_effort,"high");
assert.ok(messages.includes("设置已用于下一轮任务"));
assert.equal(state.submitting,false);
''', extra)

    def test_old_run_completion_cannot_unlock_pending_submission_or_retarget_guidance(self):
        extra = function_source("function beginComposerSubmission", "function setSidebarCollapsed")
        self.run_js(r'''
const options={agent_id:7,provider_id:null,reasoning_effort:"",approval_policy:"ask"};
global.state={agentId:7,sessionId:"original-session",activeProjectId:3,runningJob:null,submitting:false,attachments:[],activeJobs:[]};
global.query={value:"first",disabled:false};
const article=()=>({dataset:{},_agentWork:{},querySelector:()=>({textContent:""})});
global.addMessage=()=>article();
global.captureComposerContext=()=>({attachments:[],runtimeOptions:{...options}});
global.setSendState=()=>{query.disabled=state.submitting};
for (const name of ["closeComposerMenus","resetRunDetails","resizeComposer","startAgentWork","clearComposerContext",
  "setMessageAttachmentContext","rememberChatView","rememberRunningJob","renderStagedMessages","renderProjects",
  "renderHistory","renderAgentMenu","setRunStatus","updateRunPhase","addTurnEvent","markAgentStatusSeen",
  "forgetRunningJob","scrollToLatest"]) global[name]=()=>{};
for (const name of ["loadHistory","loadActiveJobs","loadSchedules","loadAgentStatuses"]) global[name]=async()=>{};
const errors=[];
global.showToast=message=>errors.push(message);
global.hasStructuredComposerContext=()=>false;
enqueueMessage=async()=>({turn_id:"first-job",session_id:"original-session",status:"running"});
let finishRun;
global.connectRun=()=>new Promise(resolve=>{finishRun=resolve});
const initial=submit();
await new Promise(setImmediate);
assert.equal(state.runningJob,"first-job");
assert.equal(state.submitting,false);
query.value="next message";
let finishPreferences;
global.prepareComposerSubmission=()=>new Promise(resolve=>{finishPreferences=resolve});
const queued=[];
enqueueQueuedMessage=async(...args)=>{queued.push(args)};
let guidance=0;
global.api=async()=>{guidance++;return {id:"unexpected"}};
const next=submitDuringRun();
assert.equal(query.disabled,true);
finishRun("done");
await initial;
assert.equal(state.submitting,true,"Old submit finally must not release the new submission lock");
assert.equal(query.disabled,true);
assert.equal(state.runningJob,null);
assert.equal(query.value,"next message");
await submitDuringRun();
assert.equal(queued.length,0,"A second click must not duplicate a pending submission");
// Even an externally selected next job cannot redirect this message to a different Turn.
state.runningJob="other-job";
state.sessionId="other-session";
state.activeProjectId=8;
state.activeJobs=[{...options,job_id:"other-job",status:"running",guidance:[]}];
finishPreferences({...options});
await next;
assert.equal(guidance,0);
assert.equal(queued.length,1);
assert.equal(queued[0][0],"next message");
assert.equal(queued[0][2],"original-session");
assert.equal(queued[0][3],3);
assert.equal(state.runningJob,"other-job");
assert.equal(state.submitting,false);
assert.equal(query.disabled,false);
assert.ok(!errors.some(message=>message.startsWith("消息未发送")),errors.join("; "));
''', extra)

    def test_queue_to_guidance_preserves_different_or_unknown_turn_options(self):
        extra = function_source("async function moveStagedMessage", "function capabilityLabel")
        self.run_js(r'''
const active={job_id:"active",agent_id:7,provider_id:9,reasoning_effort:"high",approval_policy:"ask"};
const queued={...active,job_id:"queued",reasoning_effort:"low"};
global.state={runningJob:"active",activeJobs:[active,queued]};
global.stagedMessageById=()=>({kind:"queue",id:"queued",jobId:"queued"});
const calls=[],messages=[];
global.api=async(url,request)=>calls.push({url,request});
global.loadActiveJobs=async()=>{};
global.showToast=message=>messages.push(message);
global.handleStagedActionFailure=async()=>assert.fail("Unexpected transform failure");
await moveStagedMessage("queued","toggle");
assert.equal(calls.length,0);
assert.ok(messages.includes("该消息使用不同设置，保留为下一轮任务"));
delete queued.reasoning_effort;
await moveStagedMessage("queued","toggle");
assert.equal(calls.length,0);
queued.reasoning_effort="high";
await moveStagedMessage("queued","toggle");
assert.equal(calls.length,1);
assert.equal(calls[0].request.json.target,"guidance");
assert.equal(calls[0].request.json.target_job_id,"active");
''', extra)

    def test_chat_schedule_reads_and_polling_respect_module_permissions(self):
        extra = function_source("async function loadSchedules", "async function refreshRestoredPage")
        self.run_js(r'''
global.state={sessionId:"member-session",schedules:[{id:1}],scheduleSeen:new Map()};
global.document={hidden:false};
let allowed=false, requests=0, intervals=0, renders=0;
global.Auth={canModule:module=>{assert.equal(module,"schedules");return allowed}};
global.api=async()=>{requests++;return []};
global.renderSchedules=()=>{renders++};
global.loadActiveJobs=async()=>{};
global.clearInterval=()=>{};
global.setInterval=()=>{intervals++;return 1};
await loadSchedules();
startSchedulePolling();
assert.equal(requests,0);
assert.equal(intervals,0);
assert.deepEqual(state.schedules,[]);
assert.equal(renders,1);
allowed=true;
await loadSchedules();
startSchedulePolling();
assert.equal(requests,1);
assert.equal(intervals,1);
''', extra)

    def test_completed_turn_resumes_queue_without_schedule_access_or_schedule_availability(self):
        extra = function_source("async function loadSchedules", "function stopSchedulePolling")
        extra += function_source("function beginComposerSubmission", "function setSidebarCollapsed")
        self.run_js(r'''
const options={agent_id:7,provider_id:null,reasoning_effort:"",approval_policy:"ask"};
const article=()=>({dataset:{},_agentWork:{},querySelector:()=>({textContent:""})});
global.addMessage=()=>article();
global.captureComposerContext=()=>({attachments:[],runtimeOptions:{...options}});
global.query={value:"message"};
for (const name of ["closeComposerMenus","resetRunDetails","resizeComposer","startAgentWork","clearComposerContext",
  "setMessageAttachmentContext","rememberChatView","rememberRunningJob","renderStagedMessages","renderProjects",
  "renderHistory","renderAgentMenu","setRunStatus","updateRunPhase","addTurnEvent","markAgentStatusSeen",
  "forgetRunningJob","setSendState","renderSchedules"]) global[name]=()=>{};
for (const name of ["loadHistory","loadAgentStatuses"]) global[name]=async()=>{};
global.loadActiveJobs=async()=>{state.activeJobs=[{...options,job_id:"queued",session_id:"session",status:"pending",source:"web"}]};
enqueueMessage=async()=>({turn_id:"first",session_id:"session",status:"running"});
global.connectRun=async()=>"done";
const resumed=[];
global.resumeActiveJob=async id=>resumed.push(id);
let allowed=false,scheduleRequests=0;
global.Auth={canModule:()=>allowed};
global.api=async()=>{scheduleRequests++;throw Object.assign(new Error("Schedule service unavailable"),{status:503})};
for (allowed of [false,true]) {
  global.state={agentId:7,sessionId:"session",activeProjectId:null,runningJob:null,submitting:false,
    attachments:[],activeJobs:[],schedules:[],scheduleSeen:new Map()};
  query.value="message";
  await submit();
  assert.equal(state.submitting,false);
}
assert.deepEqual(resumed,["queued","queued"]);
assert.equal(scheduleRequests,1,"An account without permission must not request schedules at all");
''', extra)

    def test_model_routing_events_use_only_public_labels_and_keep_fallback_evidence(self):
        extra = 'const RunWorkspace = require("./frontend/static/run-workspace.js");\n'
        extra += function_source("function runtimeActivityIdentity", "function runtimeStopReasonText")
        extra += function_source("function runtimeEventPresentation", "function recordRuntimeActivity")
        self.run_js(r'''
const privateData = {provider_id:"PRIVATE_PROVIDER",raw:"PRIVATE_OUTPUT",reason:"PRIVATE_REASON",source:"PRIVATE_SOURCE"};
assert.deepEqual(runtimeEventPresentation("model.role.selected",{...privateData,role:"planner",inherited:true}),
  {text:"规划：沿用主模型",kind:"done"});
assert.deepEqual(runtimeEventPresentation("model.role.selected",{...privateData,role:"router",inherited:false}),
  {text:"路由：已选择专用模型",kind:"done"});
assert.deepEqual(runtimeEventPresentation("model.role.fallback",{...privateData,role:"critic"}),
  {text:"审查模型暂不可用，已回退到主模型",kind:"warning"});
const payload = {...privateData,offered:["PRIVATE_TOOL_A","PRIVATE_TOOL_B"],confidence:0.81,confidence_kind:"model_estimate"};
assert.deepEqual(runtimeEventPresentation("tools.selection",{...payload,decision:"accepted"}),
  {text:"工具路由：已选择 2 项能力（模型自评 81%）",kind:"done"});
for (const decision of ["invalid_selection","low_confidence","model_unavailable"]) {
  const result = runtimeEventPresentation("tools.selection",{...payload,decision});
  assert.equal(result.kind,"warning");
  assert.ok(result.text.includes("沿用规则匹配的 2 项能力"));
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
}
const unknown = runtimeEventPresentation("model.role.selected",{...privateData,role:"PRIVATE_ROLE"});
assert.equal(unknown.text,"当前环节：已选择专用模型");
assert.ok(!runtimeEventPresentation("tools.selection",{...payload,confidence:Infinity,decision:"accepted"}).text.includes("自评"));
assert.ok(!runtimeEventPresentation("tools.selection",{...payload,confidence_kind:"PRIVATE_KIND",decision:"accepted"}).text.includes("自评"));
assert.notEqual(runtimeActivityIdentity("model.role.selected",{role:"planner"}),runtimeActivityIdentity("model.role.selected",{role:"router"}));
assert.notEqual(runtimeActivityIdentity("model.role.selected",{role:"router"}),runtimeActivityIdentity("model.role.fallback",{role:"router"}));
assert.notEqual(runtimeActivityIdentity("tools.selection",{decision:"accepted"}),runtimeActivityIdentity("tools.selection",{decision:"low_confidence"}));
assert.equal(RunWorkspace.eventCategory("tools.selection"),"tools");
assert.equal(RunWorkspace.phaseForEvent("tools.selection"),"decide");
assert.equal(RunWorkspace.phaseForEvent("model.role.selected",{role:"planner"}),"decide");
assert.equal(RunWorkspace.phaseForEvent("model.role.fallback",{role:"critic"}),"verify");
const projection = RunWorkspace.createProjection();
RunWorkspace.observe(projection,{event_type:"task.completed_with_issues"});
RunWorkspace.observe(projection,{event_type:"model.role.fallback",payload:{role:"critic"}});
assert.equal(projection.phase,"completed_with_issues");
''', extra)


if __name__ == "__main__":
    unittest.main()
