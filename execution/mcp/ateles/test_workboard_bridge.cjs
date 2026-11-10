// Exercise the actual bundled view script against a minimal, synthetic host.
// This is bridge/DOM behavior evidence, not a real host rendering assertion.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.handlers={};this.value='';this.style={};this.hidden=false;this.disabled=false;this.textContent=''; }
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=[...values];this.textContent='';}
  addEventListener(name,fn){this.handlers[name]=fn;}
  allText(){return this.textContent+this.children.map(c=>c.allText()).join(' ');}
}
const ids=Object.fromEntries(['count','status','primary','notice','search','filter','rows','refresh'].map(id=>[id,new Element(id)]));
ids.filter.value='all';
const sent=[];let receive;
const parent={postMessage(message){sent.push(message);}};
const document={getElementById:id=>ids[id],createElement:tag=>new Element(tag),documentElement:{style:{},scrollHeight:500}};
vm.runInNewContext(script,{document,window:{parent,addEventListener(name,fn){if(name==='message')receive=fn;}},Map,Promise,setTimeout,clearTimeout,console});
const deliver=(message,source=parent)=>receive({source,data:{jsonrpc:'2.0',...message}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const init=sent.find(m=>m.method==='ui/initialize');assert(init);assert.equal(init.params.protocolVersion,'2026-01-26');
  deliver({id:init.id,result:{hostCapabilities:{serverTools:{}},hostContext:{theme:'dark'}}});await settle();
  assert(sent.some(m=>m.method==='ui/notifications/initialized'));
  deliver({method:'ui/notifications/tool-input',params:{arguments:{session_key:'synthetic:inbox',display_plan_id:'ent_plan'}}});
  const payload={observed_at:'2026-01-01T12:00:00Z',rows:[{priority:'display-plan',work:'Primary planning record',state:'Recorded state: active',next_action:'Read the current plan'},
    {priority:'recorded',work:'<img src=x onerror=alert(1)>',state:'Recorded state: open',next_action:'Review the task',entity_id:'ent_task',read_gaps:[],planning_ascent:[{entity_type:'plan',title:'Existing plan'}]}],read_gaps:[]};
  deliver({method:'ui/notifications/tool-result',params:{structuredContent:payload}});
  assert(ids.primary.allText().includes('Primary planning record'));
  assert(ids.rows.allText().includes('<img src=x onerror=alert(1)>'));
  assert(ids.rows.allText().includes('Existing plan'));
  assert(!ids.rows.children.some(c=>c.tagName==='img'));
  const previous=ids.rows.allText();deliver({method:'ui/notifications/tool-result',params:{structuredContent:{rows:[]}}},{});
  assert.equal(ids.rows.allText(),previous,'messages from another frame must be ignored');
  ids.search.value='no match';ids.search.handlers.input();assert(ids.rows.allText().includes('No work matches'));
  ids.search.value='';ids.filter.value='operator-needed';ids.filter.handlers.change();assert(ids.rows.allText().includes('No work matches'));
  ids.filter.value='all';ids.filter.handlers.change();
  const refresh=ids.refresh.handlers.click();
  const call=sent.find(m=>m.method==='tools/call');assert(call);assert.equal(call.params.name,'get_session_workboard');assert.equal(call.params.arguments.include_history,false);
  deliver({id:call.id,result:{structuredContent:{error:'Synthetic read unavailable'},isError:true}});await refresh;
  assert(ids.notice.allText().includes('Existing rows are stale'));
  assert(ids.rows.allText().includes('Review the task'),'failed refresh retains clearly stale evidence');
  assert(sent.filter(m=>m.method==='tools/call').every(m=>m.params.name==='get_session_workboard'));
  console.log('actual view bridge: initialization, safe text, parent confinement, filters, refresh and stale error passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
