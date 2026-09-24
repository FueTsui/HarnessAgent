"""Exercise actual MCP form/card functions in Node with a small DOM adapter."""
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function_source(source, name):
    start = source.index(f"function {name}(")
    ends = [value for value in (source.find("\nfunction ", start + 1), source.find("\nasync function ", start + 1)) if value >= 0]
    return source[start:min(ends) if ends else len(source)]


class McpAdminUiTests(unittest.TestCase):
    def test_transport_form_preservation_validation_and_secret_free_cards(self):
        source = (ROOT / "frontend/static/admin.js").read_text(encoding="utf-8")
        start = source.index("const resources = {")
        resources = source[start:source.index("\n};", start) + 3]
        functions = "\n".join(function_source(source, name) for name in (
            "fieldValue", "fieldOptions", "renderKeyValueRows", "renderResourceRows", "renderField",
            "bindResourceForm", "collectResourcePayload", "meta", "statusBadge", "cardActions", "resourceCard",
            "showResult", "mcpTestResultText",
        ))
        harness = r'''
const assert = require("node:assert/strict");
let role = "root";
const Auth = {role:()=>role};
const state = {activeResource:"mcp",editingResource:null,providerPresets:{},userModules:[]};
const inputs = new Map(), wraps = new Map();
const $ = id => inputs.get(id);
const document = {querySelector:()=>null,querySelectorAll:()=>[]};
const escapeHtml = value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
const icon = ()=>"<svg></svg>";
const formRoot = {
  querySelector(selector) {
    const name = selector.match(/data-field(?:-wrap)?=["']([^"']+)/)?.[1];
    return selector.includes("data-field-wrap") ? wraps.get(name) : inputs.get(`resource-field-${name}`);
  }, querySelectorAll:()=>[],
};
function setup(item) {
  inputs.clear();wraps.clear();state.editingResource=item;
  for(const field of resources.mcp.fields) {
    const value=fieldValue(field,item);
    const input={value:field.type==="json-array"?JSON.stringify(value):String(value ?? ""),checked:!!value,required:!!field.required,disabled:false};
    input.querySelectorAll=selector=>selector===".repeat-row"?Object.entries(value||{}).map(([key,val])=>({querySelector:part=>({value:part.includes("key")?key:String(val)})})):[];
    const wrap={hidden:false,querySelectorAll:()=>[input]};
    input.closest=()=>wrap;
    inputs.set(`resource-field-${field.name}`,input);wraps.set(field.name,wrap);
  }
  bindResourceForm(formRoot);
}
'''
        scenario = r'''
const transportField=resources.mcp.fields.find(field=>field.name==="transport");
assert.ok(fieldOptions(transportField).some(([value])=>value==="stdio"));
role="admin";
assert.ok(!fieldOptions(transportField).some(([value])=>value==="stdio"));
role="root";
const original={id:4,name:"Local connection",description:"Example",transport:"http",url:"https://example.com/mcp",headers:{Authorization:"old-header"},command:"C:/Program Files/Python/python.exe",args:["C:/MCP server/main.py","two words"],env:{TOKEN:"********"},cwd:"C:/MCP server",enabled:true,risk_policy:"auto",is_public:false};
setup(original);
assert.equal(wraps.get("command").hidden,true);
assert.equal($("resource-field-command").disabled,true);
$("resource-field-args").value="invalid json hidden from HTTP";
let payload=collectResourcePayload();
assert.equal(payload.url,original.url);
assert.ok(!("command" in payload));
assert.ok(!("env" in payload));
$("resource-field-args").value=JSON.stringify(original.args);
$("resource-field-transport").value="stdio";
$("resource-field-transport").onchange();
assert.equal(wraps.get("url").hidden,true);
assert.equal($("resource-field-url").disabled,true);
assert.equal($("resource-field-url").required,false);
assert.equal($("resource-field-command").required,true);
payload=collectResourcePayload();
assert.equal(payload.command,original.command);
assert.deepEqual(payload.args,original.args);
assert.equal(payload.env.TOKEN,"********");
assert.ok(!("url" in payload));
assert.ok(!("headers" in payload));
$("resource-field-transport").value="http";$("resource-field-transport").onchange();
payload=collectResourcePayload();
assert.equal(payload.headers.Authorization,"old-header");
assert.equal(payload.url,original.url);
$("resource-field-transport").value="stdio";$("resource-field-transport").onchange();
assert.deepEqual(collectResourcePayload().args,original.args);
$("resource-field-command").value="";
assert.throws(()=>collectResourcePayload(),/必填/);
$("resource-field-command").value=original.command;
$("resource-field-args").value='["ok", 4]';
assert.throws(()=>collectResourcePayload(),/字符串数组/);
$("resource-field-args").value='["unterminated"';
assert.throws(()=>collectResourcePayload(),/JSON/);
$("resource-field-args").value='[]';
role="admin";
assert.throws(()=>collectResourcePayload(),/仅 root/);
const shared={...original,transport:"stdio",can_manage:false,stdio_authorized:true,env:{TOKEN:"never-show-value"},args:["never-show-argument"]};
let card=resourceCard(shared,"mcp");
assert.match(card,/root 共享的本地连接/);
assert.doesNotMatch(card,/never-show-value|never-show-argument|Program Files/);
assert.doesNotMatch(card,/data-resource-action="(?:edit|test|delete)"/);
role="root";
card=resourceCard({...shared,can_manage:true},"mcp");
assert.match(card,/Program Files/);
assert.doesNotMatch(card,/never-show-value|never-show-argument/);
assert.match(resourceCard({...shared,stdio_authorized:false},"mcp"),/未获启动授权/);
const envField=resources.mcp.fields.find(field=>field.name==="env");
assert.match(renderField(envField,original,true),/添加环境变量/);
assert.match(renderField(envField,original,true),/type="password"/);
assert.match(renderKeyValueRows({},false,"env"),/环境变量名/);
const maliciousName='<img src=x onerror=alert(1)>';
const directory=mcpTestResultText({tools:[{name:maliciousName,description:"fixture env preserved"},{name:"second"}]});
assert.match(directory,/fixture env preserved/);
assert.match(directory,/2\. second/);
assert.match(mcpTestResultText({tools:[]}),/未提供任何工具/);
for(const id of ["result-title","result-copy","result-value"]) inputs.set(id,{});
let resultOpened=false;
inputs.set("result-dialog",{showModal:()=>{resultOpened=true;}});
showResult("工具目录","连接成功",directory);
assert.equal($("result-value").textContent,directory);
assert.ok(!("innerHTML" in $("result-value")));
assert.ok(resultOpened);
console.log("MCP form and card behavior verified");
'''
        result = subprocess.run(["node", "-"], input=harness + resources + functions + scenario,
                                text=True, encoding="utf-8", capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
