"""Personal theme previews must not persist or save unrelated preferences early."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SETUP = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map(), storage = [], accents = [], appearances = [], patches = [];
const documentListeners = new Map(), windowListeners = new Map(), mediaListeners = new Set();
let radios = [], focused = null, failSave = false;
const saved = {revision:7,theme:'system',theme_color:'default',custom_color:'#8b5cf6',default_agent_id:null,recent_sort:'priority',approval_policy:'ask'};
class Element {
  constructor(id, attributes='') {
    this.id=id; this.value=/\bvalue="([^"]*)"/.exec(attributes)?.[1] || '';
    this.checked=/\bchecked\b/.test(attributes); this.hidden=/\bhidden\b/.test(attributes);
    this.open=false; this.disabled=false; this.textContent=''; this.dataset={}; this.attributes={};
    this.scrollTop=0; this.scrollHeight=365; this.rect={top:300,bottom:342};
    this.style={setProperty(name,value){this[name]=value;}};
  }
  focus(){focused=this;this.onfocus?.();}
  getBoundingClientRect(){return this.rect;}
  get clientHeight(){return parseFloat(this.style.maxHeight) || 360;}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  querySelector(selector){return elements.get(selector==='[type="submit"]'?'personal-save':selector.slice(1));}
  querySelectorAll(){return radios;}
  contains(target){return target===this || target===elements.get('personal-color-summary') || radios.includes(target);}
  set innerHTML(html){
    this.html=html;
    for(const match of html.matchAll(/<(?:input|select|button|details|summary|span|div|p|form|fieldset)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) elements.set(match[2],new Element(match[2],match[1]));
    for(const match of html.matchAll(/<select id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
      const options=[...match[2].matchAll(/<option value="([^"]*)"([^>]*)>/g)];
      elements.get(match[1]).value=(options.find(item=>item[2].includes('selected')) || options[0])[1];
    }
    radios=[...html.matchAll(/<input\b([^>]*name="personal-theme-color"[^>]*)>/g)].map(match=>new Element('',match[1]));
    radios.forEach((radio,index)=>radio.parentElement={offsetTop:6+index*39,offsetHeight:39});
  }
}
const host=new Element('personal-settings-content'); elements.set(host.id,host);
global.window=global;
global.innerHeight=844;
global.addEventListener=(name,fn)=>windowListeners.set(name,fn);
global.removeEventListener=(name,fn)=>{if(windowListeners.get(name)===fn)windowListeners.delete(name);};
global.document={getElementById:id=>elements.get(id),querySelector:()=>({getBoundingClientRect:()=>({bottom:68})}),addEventListener:(name,fn)=>documentListeners.set(name,fn),removeEventListener:(name,fn)=>{if(documentListeners.get(name)===fn)documentListeners.delete(name);}};
global.matchMedia=()=>({matches:false,addEventListener:(name,fn)=>mediaListeners.add(fn),removeEventListener:(name,fn)=>mediaListeners.delete(fn)});
global.escapeHtml=value=>String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
global.Theme={
  palette:[{id:'default',label:'默认',color:'#242424'},{id:'blue',label:'蓝色',color:'#2563eb'},{id:'black',label:'黑色',color:'#171717'}],
  colors:(id,custom,dark)=>({'--primary':id==='custom'?custom:(dark?'#ececec':'#242424'),'--primary-soft':dark?'#404040':'#e8e8e8','--accent-contrast':dark?'#212121':'#ffffff'}),
  setAccent:(...args)=>accents.push(args),apply:value=>appearances.push(value)
};
global.localStorage={setItem:(...args)=>storage.push(args)};
global.Auth={role:()=> 'user'}; global.openChangePasswordDialog=()=>{}; global.showToast=()=>{};
global.api=async(url,options)=>{
  if(options?.method==='PATCH') {patches.push(options.json);if(failSave)throw Error('保存失败');return {...saved,...options.json,revision:options.json.revision+1};}
  return url.endsWith('/preferences')?{...saved}:[];
};
vm.runInThisContext(fs.readFileSync('frontend/static/personal-settings.js','utf8'));
const byId=id=>elements.get(id);
const choose=id=>{radios.forEach(radio=>radio.checked=radio.value===id);radios.find(radio=>radio.checked).onchange();};
const save=()=>byId('personal-form').onsubmit({preventDefault(){},currentTarget:byId('personal-form')});
(async()=>{
await PersonalSettings.load('preferences');
assert.equal(accents.length,1);
assert.deepEqual(accents[0],['default','#8b5cf6']);
assert.equal(documentListeners.size,2);
"""


class ThemeSettingsUiTests(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run(
            ['node', '-'], cwd=ROOT, text=True, encoding='utf-8',
            input=SETUP + body + '\n})().catch(error=>{console.error(error);process.exitCode=1;});',
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_draft_color_and_appearance_only_update_local_preview(self):
        self.run_js(r"""
const storageBefore=storage.length;
choose('custom');
byId('personal-color-hex').value='#abcdef';byId('personal-color-hex').oninput();
byId('personal-theme').value='dark';byId('personal-theme').onchange();
assert.equal(byId('personal-theme-preview').style['--primary'],'#abcdef');
assert.equal(byId('personal-theme-preview').dataset.previewTheme,'dark');
assert.equal(byId('personal-custom-color').hidden,false);
assert.equal(accents.length,1);assert.equal(appearances.length,1);
assert.equal(storage.length,storageBefore);assert.equal(patches.length,0);
PersonalSettings.leave();assert.equal(documentListeners.size,0);assert.equal(mediaListeners.size,0);assert.equal(windowListeners.size,0);
""")

    def test_invalid_custom_hex_blocks_entire_save_and_focuses_error(self):
        self.run_js(r"""
choose('custom');
for(const bad of ['#12','abcdef','#abcdeg',' #abcdef','#abcdef ']) {
  byId('personal-color-hex').value=bad;
  await save();
  assert.equal(patches.length,0);assert.equal(accents.length,1);
  assert.equal(focused,byId('personal-color-hex'));
  assert.equal(byId('personal-color-hex').attributes['aria-invalid'],'true');
  assert.match(byId('personal-color-error').textContent,/#RRGGBB/);
}
""")

    def test_success_saves_both_colors_with_revision_then_applies(self):
        self.run_js(r"""
choose('custom');byId('personal-color-hex').value='#AbCdEf';byId('personal-color-hex').oninput();
await save();
assert.equal(patches.length,1);
assert.deepEqual(patches[0],{revision:7,theme_color:'custom',custom_color:'#abcdef',theme:'system',default_agent_id:null,recent_sort:'priority',approval_policy:'ask'});
assert.deepEqual(accents.at(-1),['custom','#abcdef']);
assert.equal(byId('personal-save').disabled,false);
await save();assert.equal(patches[1].revision,8);
""")

    def test_failed_save_does_not_apply_or_advance_revision(self):
        self.run_js(r"""
choose('blue');failSave=true;await save();
assert.equal(accents.length,1);assert.equal(appearances.length,1);
assert.equal(byId('personal-save-error').textContent,'保存失败');
failSave=false;await save();assert.equal(patches[1].revision,7);
assert.deepEqual(accents.at(-1),['blue','#8b5cf6']);
""")

    def test_keyboard_open_escape_and_pointer_close_keep_native_radios(self):
        self.run_js(r"""
const picker=byId('personal-color-picker'),summary=byId('personal-color-summary');
let prevented=false;
summary.onkeydown({key:'ArrowDown',preventDefault(){prevented=true;}});
assert.equal(prevented,true);assert.equal(picker.open,true);assert.equal(focused,radios[0]);
picker.onkeydown({key:'Escape',preventDefault(){},stopPropagation(){}});
assert.equal(picker.open,false);assert.equal(focused,summary);
picker.open=true;radios[1].onclick({detail:0});assert.equal(picker.open,true);
radios[1].onclick({detail:1});assert.equal(picker.open,false);
picker.open=true;documentListeners.get('pointerdown')({target:byId('personal-theme')});assert.equal(picker.open,false);
assert.match(host.html,/<fieldset class="personal-color-options" id="personal-color-options">/);
assert.match(host.html,/type="radio" name="personal-theme-color"/);
""")

    def test_dropdown_flips_and_limits_height_inside_narrow_short_viewports(self):
        self.run_js(r"""
const picker=byId('personal-color-picker'),summary=byId('personal-color-summary'),menu=byId('personal-color-options');
picker.open=true;summary.rect={top:449,bottom:491};picker.ontoggle();
assert.equal(menu.style.top,'auto');assert.equal(menu.style.bottom,'calc(100% + 6px)');
assert.equal(parseFloat(menu.style.maxHeight),360);
assert.ok(summary.rect.top-6-parseFloat(menu.style.maxHeight)>=76);
innerHeight=480;summary.rect={top:200,bottom:242};windowListeners.get('resize')();
assert.equal(menu.style.bottom,'auto');assert.equal(parseFloat(menu.style.maxHeight),224);
assert.ok(summary.rect.bottom+6+parseFloat(menu.style.maxHeight)<=innerHeight-8);
radios.at(-1).parentElement={offsetTop:330,offsetHeight:39};radios.at(-1).focus();
assert.equal(focused,radios.at(-1));assert.equal(menu.scrollTop,145);
global.visualViewport={offsetTop:120,height:320};summary.rect={top:220,bottom:262};
documentListeners.get('scroll')();
assert.equal(parseFloat(menu.style.maxHeight),164);
assert.ok(summary.rect.bottom+6+parseFloat(menu.style.maxHeight)<=432);
""")


if __name__ == '__main__':
    unittest.main()
