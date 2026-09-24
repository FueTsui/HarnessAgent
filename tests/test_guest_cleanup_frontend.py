"""Root guest cleanup controls, confirmed scope, and partial filesystem results."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GuestCleanupFrontendTests(unittest.TestCase):
    def run_js(self, body):
        setup = r'''
const assert=require('node:assert/strict'), fs=require('node:fs');
const source=fs.readFileSync('frontend/static/admin.js','utf8');
function extract(name) {
 const start=source.search(new RegExp('(?:async )?function '+name+'\\('));
 const tail=source.slice(start), end=tail.slice(1).search(/\n(?:async )?function |\nconst tabModuleKey/);
 return end<0?tail:tail.slice(0,end+1);
}
const guest={id:1,username:'temporary-session',role:'guest',is_guest:true,is_active:true};
const member={id:2,username:'visitor_registered',role:'user',is_active:true};
const state={activeResource:'users',currentTab:'users',resourceRows:[guest,member],resourceRequest:0,resourceQuery:'',resourceFilter:'all'};
const resources={users:{title:'用户',endpoint:'/api/v1/users',clearLabel:'清理全部访客'},audit:{endpoint:'/api/v1/audit',clearLabel:'清空日志'}};
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{disabled:false,hidden:false,dataset:{},classList:{toggle(){}},isConnected:true});return elements.get(id);};
let role='root', confirmed=true, requests=[], prompts=[], notices=[], results=[], refreshed=[];
const Auth={role:()=>role};
const confirm=message=>{prompts.push(message);return confirmed;};
const showToast=message=>notices.push(message);
const showResult=(...args)=>results.push(args);
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g, char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const statusBadge=()=>'';
const resourceName=item=>item.username;
const currentResourceItem=id=>state.resourceRows.find(item=>String(item.id)===String(id));
let api=async(url,options)=>{requests.push([url,options]);return {deleted_users:1,cleanup_warnings:[]};};
let loadResource=async key=>{refreshed.push(key);state.resourceRows=[member];};
for(const name of ['isGuestUser','reportGuestCleanup','clearGuestUsers','clearResource','handleResourceAction','renderResourceTable']) eval(extract(name));
'''
        result = subprocess.run(['node', '-'], cwd=ROOT, text=True, encoding='utf-8',
                                input=setup + '\n(async()=>{\n' + body
                                + '\n})().catch(error=>{console.error(error);process.exitCode=1;});',
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_guest_identity_and_destructive_labels_do_not_use_username_prefixes(self):
        self.run_js(r'''
assert.equal(isGuestUser(guest),true);
assert.equal(isGuestUser(member),false);
assert.equal(isGuestUser({...member,is_guest:true}),false,'server role remains authoritative');
const rows=renderResourceTable([guest,member],'users').split('<tr>');
const guestRow=rows.find(row=>row.includes('temporary-session'));
const memberRow=rows.find(row=>row.includes('visitor_registered'));
assert(guestRow.includes('访客'));
assert(guestRow.includes('删除访客及数据'));
assert(!guestRow.includes('data-resource-action="edit"'));
assert(memberRow.includes('普通用户'));
assert(memberRow.includes('data-resource-action="edit"'));
assert(!memberRow.includes('删除访客及数据'));
role='admin';
assert(!renderResourceTable([guest],'users').includes('data-resource-action'));
''')

    def test_bulk_cleanup_requires_root_guest_rows_and_explicit_confirmation(self):
        self.run_js(r'''
role='admin'; await clearResource();
assert.equal(requests.length,0); assert.equal(prompts.length,0);
role='root'; state.resourceRows=[member]; await clearResource();
assert.equal(requests.length,0);
state.resourceRows=[guest,member]; confirmed=false; await clearResource();
assert.equal(requests.length,0);
for(const term of ['全部访客','聊天记录','任务','附件','私人模型','不可撤销']) assert(prompts[0].includes(term));
confirmed=true; await clearResource();
assert.deepEqual(requests,[['/api/v1/users/guests',{method:'DELETE'}]]);
assert.deepEqual(refreshed,['users']);
assert.equal(state.guestCleanupPending,false);
assert.equal($('resource-clear').disabled,true,'no remaining guest cannot be cleared again');
assert(notices.some(message=>message.includes('1 个访客')));
''')

    def test_cleanup_failure_preserves_rows_and_surfaces_backend_reason(self):
        self.run_js(r'''
api=async()=>{throw new Error('访客仍有运行中的任务，请先停止后重试');};
await clearResource();
assert.equal(state.resourceRows.length,2);
assert.equal(refreshed.length,0);
assert.equal(state.guestCleanupPending,false);
assert.equal($('resource-clear').disabled,false);
assert(notices[0].includes('访客仍有运行中的任务，请先停止后重试'));
''')

    def test_filesystem_warnings_report_committed_account_deletion_separately(self):
        self.run_js(r'''
api=async()=>({deleted_users:1,cleanup_warnings:['附件文件被占用：upload-1']});
await clearResource();
assert(notices.some(message=>message.includes('已删除1 个访客账号')));
assert(!notices.some(message=>message.includes('清理访客失败')));
assert.equal(results[0][0],'访客清理结果');
assert.equal(results[0][2],'附件文件被占用：upload-1');
assert.deepEqual(refreshed,['users']);
''')

    def test_inflight_cleanup_cannot_repeat_or_reopen_page_after_navigation(self):
        self.run_js(r'''
let resolve;
api=()=>new Promise(done=>{resolve=done;requests.push('delete');});
const pending=clearResource();
await clearResource();
assert.equal(requests.length,1);
state.currentTab=state.activeResource='audit';
$('resource-clear').disabled=true;
resolve({deleted_users:1,cleanup_warnings:[]}); await pending;
assert.equal(refreshed.length,0);
assert.equal($('resource-clear').disabled,true,'do not mutate another module toolbar');
''')

    def test_single_guest_delete_has_cascade_confirmation_and_member_path_is_unchanged(self):
        self.run_js(r'''
loadResource=async key=>refreshed.push(key);
const button={disabled:false,isConnected:true,dataset:{resourceAction:'delete',id:'1'}};
await handleResourceAction({target:{closest:()=>button}});
assert.equal(requests[0][0],'/api/v1/users/1');
assert(prompts[0].includes('删除访客'));
for(const term of ['聊天记录','任务','附件','私人模型']) assert(prompts[0].includes(term));
button.dataset.id='2';
await handleResourceAction({target:{closest:()=>button}});
assert.equal(requests[1][0],'/api/v1/users/2');
assert(prompts[1].includes('确认删除用户'));
assert(prompts[1].includes('其他关联业务数据仍需先转移或删除'));
assert(!prompts[1].includes('删除访客'));
''')

    def test_toolbar_visibility_and_availability_follow_actual_guest_role(self):
        self.run_js(r'''
eval(extract('loadResource'));
const renderResourceCollection=()=>{};
api=async()=>[guest,member];
await loadResource('users');
assert.equal($('resource-clear').hidden,false);
assert.equal($('resource-clear').disabled,false);
assert.equal($('resource-clear-label').textContent,'清理全部访客');
role='admin'; await loadResource('users');
assert.equal($('resource-clear').hidden,true);
role='root'; api=async()=>[member]; await loadResource('users');
assert.equal($('resource-clear').disabled,true);
''')


if __name__ == '__main__':
    unittest.main()
