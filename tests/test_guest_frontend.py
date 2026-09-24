"""Frontend session and personal credential boundaries, executed in JavaScript."""
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON = (ROOT / "frontend/static/common.js").read_text(encoding="utf-8")
SESSION_SOURCE = COMMON[:COMMON.index("async function downloadAuthenticated")]
APP = (ROOT / "frontend/static/app.js").read_text(encoding="utf-8")


class GuestFrontendTests(unittest.TestCase):
    def run_js(self, scenario, extra=""):
        harness = r'''
const assert = require("node:assert/strict");
const storage = new Map([["gca_username","forged-root"],["gca_role","root"],["gca_modules",'["providers"]']]);
global.localStorage = {getItem(key){return storage.get(key) ?? null;},setItem(){throw new Error("Identity must not persist");},removeItem(key){storage.delete(key);}};
global.location = {href:"/",reload(){this.reloaded = true;}};
const reply = (status, data) => ({status, ok:status >= 200 && status < 300, statusText:"Request failed", json:async()=>data});
const guest = {id:12,username:"visitor_fixture",role:"guest",is_guest:true,modules:[]};
const member = {id:18,username:"member",role:"user",is_guest:false,modules:[]};
'''
        result = subprocess.run(
            ["node", "-"], cwd=ROOT, text=True, encoding="utf-8",
            input=harness + SESSION_SOURCE + extra + "\n(async () => {\n" + scenario
            + "\n})().catch(error => { console.error(error); process.exitCode = 1; });",
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_cached_identity_cannot_grant_access(self):
        self.run_js(r'''
assert.equal(Auth.username(), null);
assert.equal(Auth.role(), null);
assert.equal(Auth.canModule("providers"), false);
assert.equal(Auth.canAccessSettings(), false);
assert.equal(storage.has("gca_username"), false);
global.fetch = async (url, options) => { assert.equal(options.credentials,"same-origin"); return reply(200,member); };
await Auth.ensureSession();
assert.equal(Auth.username(),"member");
assert.equal(Auth.canModule("providers"),false);
assert.equal(Auth.canAccessSettings(),true);
assert.equal(storage.size,0);
''')

    def test_unauthenticated_session_redirects_once_without_bootstrap(self):
        self.run_js(r'''
const calls=[];
global.fetch = async (url,options) => {
  calls.push({url,options});
  return url.endsWith("/me") ? reply(401,{detail:"Expired"}) : reply(200,guest);
};
const results = await Promise.allSettled([Auth.ensureSession(),Auth.ensureSession()]);
assert.ok(results.every(result=>result.status === "rejected"));
assert.deepEqual(calls.map(call=>call.url),["/api/v1/auth/me"]);
assert.ok(calls.every(call=>call.options.credentials === "same-origin"));
assert.equal(Auth.user,null);
assert.equal(Auth.canAccessSettings(),false);
assert.equal(Auth.canModule("providers"),false);
assert.equal(location.href,"/login");
''')

    def test_session_service_failure_never_creates_another_guest(self):
        self.run_js(r'''
const calls=[];
global.fetch=async url=>{calls.push(url);return reply(503,{});};
await assert.rejects(Auth.ensureSession(),/无法读取浏览器会话/);
assert.deepEqual(calls,["/api/v1/auth/me"]);
assert.equal(Auth.pending,null);
assert.equal(Auth.user,null);
''')

    def test_login_401_keeps_guest_and_stays_on_login_page(self):
        self.run_js(r'''
Auth.save(guest);
location.href="/login";
global.fetch=async (url,options)=>{
  assert.equal(options.credentials,"same-origin");
  assert.equal(JSON.parse(options.body).username,"member");
  return reply(401,{detail:"用户名或密码错误"});
};
await assert.rejects(api("/api/v1/auth/login",{method:"POST",json:{username:"member",password:"incorrect"}}),error=>error.status===401 && error.message==="用户名或密码错误");
assert.equal(location.href,"/login");
assert.equal(Auth.user.id,guest.id);
''')

    def test_admin_rechecks_session_and_redirects_guests(self):
        self.run_js(r'''
Auth.save({...member,role:"root"});
let requests=0;
global.fetch=async()=>{requests++;return reply(200,guest);};
assert.equal(await Auth.requireLogin(),null);
assert.equal(requests,1);
assert.equal(location.href,"/login");
assert.equal(Auth.isGuest(),true);
''')

    def test_admin_does_not_initialize_privileged_ui_before_identity(self):
        admin = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        initializer = admin[admin.index("async function initializeAdmin()"):admin.index("initializeAdmin().catch")]
        self.run_js(r'''
global.fetch=async()=>reply(200,guest);
global.initTopbar=()=>{throw new Error("Guest must never initialize admin UI");};
await initializeAdmin();
assert.equal(location.href,"/login");
''', initializer)

    def test_changed_browser_identity_requires_fresh_page_before_cached_view(self):
        self.run_js(r'''
Auth.save(guest);
global.fetch=async()=>reply(200,member);
assert.equal(await Auth.verifyCurrentSession(),false);
assert.equal(location.reloaded,true);
''')


    def test_unavailable_default_cannot_start_a_paid_request(self):
        extra = APP[APP.index("async function prepareComposerSubmission"):APP.index("function renderAgentMenu")]
        self.run_js(r'''
const ChatWorkspace=require("./frontend/static/chat-workspace.js");
global.ChatWorkspace=ChatWorkspace;
assert.equal(ChatWorkspace.modelAvailable({provider_id:null,available:false,model:""}),false);
assert.equal(ChatWorkspace.modelAvailable({provider_id:null,model:""}),false);
assert.equal(ChatWorkspace.modelAvailable({provider_id:3,available:true,model:"test"}),true);
global.state={agentId:7,composerSelection:ChatWorkspace.newComposerSelection()};
global.loadPreferences=async()=>({approval_policy:"ask"});
global.fetchComposerModels=async()=>[{provider_id:null,available:false,model:""}];
global.renderComposerControls=()=>{};
const context={};
await assert.rejects(prepareComposerSubmission(context),/暂无可用模型/);
assert.equal(context.runtimeOptions,undefined);
''', extra)

    def test_guest_concrete_default_is_available_and_frozen_as_explicit_provider(self):
        extra = APP[APP.index("async function fetchComposerModels"):APP.index("async function loadModels")]
        extra += APP[APP.index("async function prepareComposerSubmission"):APP.index("function renderAgentMenu")]
        self.run_js(r'''
const ChatWorkspace=require("./frontend/static/chat-workspace.js");
global.ChatWorkspace=ChatWorkspace;
const model={provider_id:17,name:"公开对话模型",model:"fixture-public",available:true,reasoning_supported:true,reasoning_efforts:["low","high"]};
let directory={available:true,default:model,items:[model]};
api=async()=>directory;
global.state={agentId:2,composerSelection:ChatWorkspace.newComposerSelection()};
global.loadPreferences=async()=>({approval_policy:"ask"});
global.renderComposerControls=()=>{};
let models=await fetchComposerModels(2);
assert.deepEqual(models.map(item=>item.provider_id),[null,17]);
assert.equal(models[0].automatic_provider_id,17);
assert.equal(ChatWorkspace.modelAvailable(models[0]),true);
assert.equal(ChatWorkspace.reconcileComposerSelection(state.composerSelection,models).reason,"");
let context={};
assert.equal((await prepareComposerSubmission(context)).provider_id,17);
assert.equal(Object.isFrozen(context.runtimeOptions),true);
directory={default:{provider_id:null,model:"registered-agent"},items:[model]};
assert.equal((await prepareComposerSubmission({})).provider_id,null);
state.composerSelection.provider_id=17;
assert.equal((await prepareComposerSubmission({})).provider_id,17);
directory={available:false,default:{provider_id:null,available:false,model:""},items:[]};
state.composerSelection=ChatWorkspace.newComposerSelection();
await assert.rejects(prepareComposerSubmission({}),/暂无可用模型/);
''', extra)

    def test_no_model_disables_send_but_keeps_configuration_and_stop_available(self):
        extra = APP[APP.index("function setSendState()"):APP.index("function emptyComposerContext()")]
        self.run_js(r'''
global.ChatWorkspace=require("./frontend/static/chat-workspace.js");
global.state={sessionReady:true,agentId:2,submitting:false,modelsLoading:false,
  models:[{provider_id:null,available:false,model:""}],composerSelection:ChatWorkspace.newComposerSelection(),
  attachments:[],runningJob:null,selectedDatasets:new Set(),selectedTemplates:new Set(),selectedSkills:new Set(),selectedMcp:new Set(),selectedAgentCalls:new Set()};
const controls={};
global.$=id=>controls[id] ||= {querySelectorAll:()=>[]};
global.query={value:"发送测试"};
global.send={classList:{toggle(){}},setAttribute(){}};
global.icon=()=>"";
global.hasComposerOverrides=()=>false;
setSendState();
assert.equal(send.disabled,true);
assert.equal(send.title,"暂无可用模型，请联系管理员");
assert.equal(controls["model-btn"].disabled,false);
assert.equal(query.disabled,false);
state.models=[{provider_id:null,available:true,model:"public-fixture"}];
setSendState();
assert.equal(send.disabled,false);
state.models=[{provider_id:null,available:false,model:""}];
state.runningJob="active-turn";
query.value="";
setSendState();
assert.equal(send.disabled,false);
assert.equal(send.title,"停止生成");
''', extra)

    def test_login_return_path_accepts_only_named_internal_pages(self):
        script = (ROOT / "frontend/static/login.js").read_text(encoding="utf-8")
        helper = script[:script.index("const loginNext")]
        self.run_js(r'''
for (const path of ["/admin","/"]) {
  assert.equal(loginDestination(`?next=${encodeURIComponent(path)}`),path);
}
for (const path of ["/models","//outside.test","https://outside.test/admin","javascript:alert(1)",
  "/\\outside.test","/admin/../outside","/admin?next=//outside.test","%2F%2Foutside.test"," /admin","/admin#outside"]) {
  assert.equal(loginDestination(`?next=${encodeURIComponent(path)}`),"/");
}
assert.equal(loginDestination(""),"/");
assert.equal(loginDestination("?next="),"/");
''', helper)
        self.assertEqual(script.count("location.href = loginNext;"), 2)


if __name__ == "__main__":
    unittest.main()
