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
const ids=Object.fromEntries(['count','status','primary','notice','search','filter','rows','refresh','planning','view-mode','keep-visible','expand','inline','presentation-note'].map(id=>[id,new Element(id)]));
ids.filter.value='all';
const sent=[];let receive;const timers=new Map();let timerId=0;
const startTimer=fn=>{timers.set(++timerId,fn);return timerId;};const stopTimer=id=>timers.delete(id);
const parent={postMessage(message){sent.push(message);}};
const documentEvents={};const document={visibilityState:'hidden',addEventListener(name,fn){documentEvents[name]=fn;},getElementById:id=>ids[id],createElement:tag=>new Element(tag),documentElement:{style:{},scrollHeight:500}};
vm.runInNewContext(script,{document,window:{parent,addEventListener(name,fn){if(name==='message')receive=fn;}},Map,Promise,setTimeout:startTimer,clearTimeout:stopTimer,console});
const deliver=(message,source=parent)=>receive({source,data:{jsonrpc:'2.0',...message}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const init=sent.find(m=>m.method==='ui/initialize');assert(init);assert.equal(init.params.protocolVersion,'2026-01-26');
  assert.equal(JSON.stringify(init.params.appCapabilities.availableDisplayModes),JSON.stringify(['inline','fullscreen','pip']));
  deliver({id:init.id,result:{hostCapabilities:{serverTools:{}},hostContext:{theme:'dark'}}});await settle();
  assert(sent.some(m=>m.method==='ui/notifications/initialized'));
  deliver({method:'ui/notifications/tool-input',params:{arguments:{session_key:'synthetic:inbox',display_plan_id:'ent_plan',include_history:true}}});
  const payload={session_key:'synthetic:inbox',display_plan_id:'ent_plan',capability_gaps:['Claimability is unknown'],history:{tasks_claimed:[{claim:'Historical work'}],artifacts:['Historical artifact']},observed_at:'2026-01-01T12:00:00Z',rows:[{priority:'display-plan',work:'Primary planning record',state:'Recorded state: active',next_action:'Read the current plan'},
    {priority:'recorded',work:'<img src=x onerror=alert(1)>',state:'Recorded state: open',next_action:'Review the task',entity_id:'ent_task',read_gaps:[],planning_ascent:[{entity_id:'ent_plan',entity_type:'plan',title:'Existing plan'}]}],read_gaps:[]};
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
  const call=sent.find(m=>m.method==='tools/call');assert(call);assert.equal(call.params.name,'get_session_workboard');assert.equal(call.params.arguments.include_history,true);
  deliver({id:call.id,result:{structuredContent:{error:'Synthetic read unavailable'},isError:true}});await refresh;
  assert(ids.notice.allText().includes('Existing rows are stale'));
  assert(ids.rows.allText().includes('Review the task'),'failed refresh retains clearly stale evidence');
  assert(sent.filter(m=>m.method==='tools/call').every(m=>m.params.name==='get_session_workboard'));
  const input=()=>deliver({method:'ui/notifications/tool-input',params:{arguments:{session_key:'synthetic:inbox',display_plan_id:'ent_plan',include_history:true}}});
  const present=data=>{input();deliver({method:'ui/notifications/tool-result',params:{structuredContent:data}});};
  present(payload);
  const treePayload={...payload,rows:[payload.rows[0],{...payload.rows[1],kind:'task',entity_id:'ent_task',work:'Scoped tree task'}],planning:{version:1,scope:'Only this session, not whole-plan completion',registry_complete:true,registry_gaps:[],membership_entries:1,unique_tasks:1,unresolved_entries:0,qualified_tasks:1,unknown_tasks:0,level_coverage:[{level:20,entity_types:['purpose'],reached_tasks:1,complete_tasks:1},{level:3,entity_types:['sprint'],reached_tasks:1,complete_tasks:1}],nodes:[{entity_id:'ent_task',entity_type:'task',title:'Scoped tree task',level:null,read_only:false,parent_id:'ent_sprint',row_indexes:[1]},{entity_id:'ent_sprint',entity_type:'sprint',title:'Small change',level:3,read_only:false,parent_id:'ent_purpose',row_indexes:[]},{entity_id:'ent_purpose',entity_type:'purpose',title:'Long direction',level:20,read_only:true,parent_id:null,row_indexes:[]}],roots:['ent_purpose'],unplanned:[]}};
  present(treePayload);
  assert(ids.rows.allText().includes('Long direction'));
  assert(ids.rows.allText().includes('Read-only reference'));
  assert(ids.rows.allText().includes('1 session tasks'));
  assert(ids.planning.allText().includes('Only this session'));
  const rootBranch=ids.rows.children[0];assert.equal(rootBranch.id,'node-ent_purpose');rootBranch.open=true;rootBranch.handlers.toggle();
  present(treePayload);assert.equal(ids.rows.children[0].open,true,'expansion is transient and retained on a same-scope refresh');
  ids.search.value='Existing plan';ids.search.handlers.input();assert(ids.rows.allText().includes('Scoped tree task'),'search includes actual planning context');
  ids.search.value='';ids.search.handlers.input();
  const treeSaved=ids.rows.allText();
  for(const malformed of [
    {...treePayload,planning:{...treePayload.planning,unique_tasks:2}},
    {...treePayload,planning:{...treePayload.planning,roots:['ent_missing']}},
    {...treePayload,planning:{...treePayload.planning,nodes:[...treePayload.planning.nodes,{...treePayload.planning.nodes[0]}]}},
    {...treePayload,planning:{...treePayload.planning,nodes:treePayload.planning.nodes.map(n=>n.entity_id==='ent_purpose'?{...n,parent_id:'ent_sprint'}:n)}},
    {...treePayload,planning:{...treePayload.planning,unplanned:[{row_index:1,reason:'Duplicate placement',unknown:false}]}},
  ]){present(malformed);assert.equal(ids.rows.allText(),treeSaved);assert(ids.notice.allText().includes('stale'));}
  const unplanned={...treePayload,planning:{...treePayload.planning,registry_complete:false,registry_gaps:['Levels are unknown'],qualified_tasks:0,unknown_tasks:1,level_coverage:[],nodes:[],roots:[],unplanned:[{row_index:1,reason:'The registry could not be read',unknown:true}]}};
  present(unplanned);assert(ids.rows.allText().includes('Unplanned'));assert(ids.rows.allText().includes('The registry could not be read'));assert(ids.planning.allText().includes('coverage is unknown'));
  present({...payload,read_gaps:Array.from({length:59},(_,i)=>`Explicit task ent_${i} could not be read as a task; its current state is unknown`)});
  assert(ids.notice.children[0].allText().includes('59 × Explicit task [record]'));
  assert(ids.notice.children[0].allText().split('could not be read').length===2,'repeated unavailable-read classes have one visible summary');
  assert(ids.notice.children[1].allText().includes('ent_58'),'full notices remain in collapsed detail');
  present(payload);
  assert.equal(ids['keep-visible'].hidden,true,'no presentation request without host capability');
  deliver({method:'ui/notifications/host-context-changed',params:{availableDisplayModes:['inline','pip','fullscreen'],displayMode:'inline'}});
  assert.equal(ids['keep-visible'].hidden,false);
  let changeMode=ids['keep-visible'].handlers.click();let modeCall=sent.filter(m=>m.method==='ui/request-display-mode').at(-1);assert.equal(modeCall.params.mode,'pip');
  deliver({id:modeCall.id,result:{mode:'inline'}});await changeMode;
  assert(ids['presentation-note'].allText().includes('host kept'));
  changeMode=ids['expand'].handlers.click();modeCall=sent.filter(m=>m.method==='ui/request-display-mode').at(-1);deliver({id:modeCall.id,result:{mode:'fullscreen'}});await changeMode;
  assert(ids['presentation-note'].allText().includes('same bound workboard'));
  assert.equal(ids.rows.allText(),previous,'presentation changes retain the same scope and data');
  deliver({method:'ui/notifications/host-context-changed',params:{availableDisplayModes:['inline'],displayMode:'inline'}});
  const modeCount=sent.filter(m=>m.method==='ui/request-display-mode').length;await ids['keep-visible'].handlers.click();assert.equal(sent.filter(m=>m.method==='ui/request-display-mode').length,modeCount);
  const beforeFocus=sent.filter(m=>m.method==='tools/call').length;documentEvents.visibilitychange();assert.equal(sent.filter(m=>m.method==='tools/call').length,beforeFocus);
  document.visibilityState='visible';documentEvents.visibilitychange();const visibleCall=sent.filter(m=>m.method==='tools/call').at(-1);assert.equal(visibleCall.params.arguments.session_key,'synthetic:inbox');deliver({id:visibleCall.id,result:{structuredContent:payload}});await settle();
  present(payload);
  assert(ids.notice.allText().includes('Claimability is unknown'));
  assert(ids.primary.allText().includes('synthetic:inbox'));
  assert(ids.rows.allText().includes('Historical work'));
  const spacedPlan='First planning step.  Keep these spaces.\n\nSecond planning step.';
  present({...payload,rows:[{...payload.rows[0],next_action:spacedPlan},payload.rows[1]]});
  const planNext=ids.primary.children.find(n=>n.textContent===spacedPlan);
  assert(planNext,'Primary text must preserve the source string, including spaces and line breaks');
  assert.equal(planNext.className,'next','Primary text must use the whitespace-preserving paragraph style');
  present(payload);
  const saved=ids.rows.allText(), time=ids.status.allText();
  for(const malformed of [null, [], 3, {...payload,rows:[null]}, {...payload,rows:[{...payload.rows[1],planning_ascent:[null]}]}, {...payload,read_gaps:[{}]}, {...payload,capability_gaps:[null]}, {...payload,history:null}, {...payload,rows:[{...payload.rows[1],read_gaps:[null]}]}, {...payload,rows:[{...payload.rows[1],native_last_activity_at:'invalid'}]}, {...payload,session_coverage:[]}, {...payload,observed_at:'invalid'}, {...payload,observed_at:'2026-02-30T00:00:00Z'}, {...payload,session_key:'synthetic:foreign'}, {...payload,display_plan_id:'ent_other'}]) {
    present(malformed);
    assert.equal(ids.rows.allText(),saved,'a rejected read retains all previous evidence');
    assert(ids.notice.allText().includes('stale'));
    assert(ids.status.allText().includes(payload.observed_at));
    assert(!ids.status.allText().includes('Reading'));
  }
  present({...payload,rows:[payload.rows[0],{...payload.rows[1],priority:'operator-needed'}]});
  assert(ids.rows.allText().includes('Needs your decision'));
  const article=ids.rows.children[0],details=article.children.find(n=>n.tagName==='details');
  assert(details.handlers.toggle); const sizeBefore=sent.length;details.handlers.toggle();assert(sent.length>sizeBefore);
  present({...payload,rows:[payload.rows[0]],read_gaps:[]});
  assert(ids.rows.allText().includes('No managed work was returned'));
  present({...payload,rows:[payload.rows[0]],read_gaps:['Coverage unknown']});
  assert(ids.rows.allText().includes('coverage is unknown'));
  present(payload);
  const pendingRefresh=ids.refresh.handlers.click();const oldCall=sent.filter(m=>m.method==='tools/call').at(-1);
  input();deliver({id:oldCall.id,result:{structuredContent:{...payload,observed_at:'2026-01-01T12:01:00Z'}}});await pendingRefresh;
  assert(ids.notice.allText().includes('stale'));
  deliver({method:'ui/notifications/tool-result',params:{structuredContent:payload}});
  deliver({method:'ui/notifications/tool-cancelled'});
  assert(ids.notice.allText().includes('stale'));
  assert(!ids.status.allText().includes('Reading'));
  present(payload);
  let run=ids.refresh.handlers.click();let latest=sent.filter(m=>m.method==='tools/call').at(-1);
  deliver({id:latest.id,result:{structuredContent:{observed_at:'2026-01-01T13:00:00Z'}}});await run;
  assert(ids.notice.allText().includes('stale'));assert(!ids.status.allText().includes('Reading'));
  run=ids.refresh.handlers.click();latest=sent.filter(m=>m.method==='tools/call').at(-1);
  deliver({method:'ui/notifications/tool-cancelled'});await run;
  const cancelledView=ids.rows.allText();deliver({id:latest.id,result:{structuredContent:{...payload,rows:[]}}});assert.equal(ids.rows.allText(),cancelledView);
  run=ids.refresh.handlers.click();const timer=[...timers.values()].at(-1);timer();await run;
  assert(ids.notice.allText().includes('stale'));assert(!ids.status.allText().includes('Reading'));
  run=ids.refresh.handlers.click();latest=sent.filter(m=>m.method==='tools/call').at(-1);
  deliver({method:'ui/notifications/tool-input',params:{arguments:{session_key:'synthetic:foreign',display_plan_id:'ent_other',include_history:true}}});await run;
  deliver({id:latest.id,result:{structuredContent:payload}});assert(ids.notice.allText().includes('stale'));
  run=ids.refresh.handlers.click();latest=sent.filter(m=>m.method==='tools/call').at(-1);deliver({method:'ui/notifications/tool-input',params:{arguments:{session_key:3,display_plan_id:'ent_plan'}}});await run;deliver({id:latest.id,result:{structuredContent:payload}});assert(ids.notice.allText().includes('stale'));
  const circular={};circular.child=circular;present({...payload,history:{tasks_claimed:[circular],artifacts:[]}});assert(ids.notice.allText().includes('stale'));
  assert(html.includes('overflow-wrap:anywhere'));
  if(process.argv[3]) {const data=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));present(data);assert(!ids.notice.allText().includes('stale'));assert(ids.rows.allText().includes('Current producer-bound task'));assert(ids.rows.allText().includes('Working now'));assert(ids.rows.allText().includes('Historical artifact'));}
  const beforeClose=sent.filter(m=>m.method==='tools/call').length;deliver({id:9999,method:'ui/resource-teardown'});assert.equal(ids.refresh.disabled,true);await ids.refresh.handlers.click();documentEvents.visibilitychange();assert.equal(sent.filter(m=>m.method==='tools/call').length,beforeClose);assert(sent.some(m=>m.id===9999&&m.result));
  console.log('actual view bridge: transactional validation, bound scope, history, coverage, decisions, cancellation and refresh passed');
})().catch(error=>{console.error(error);process.exit(1);});
