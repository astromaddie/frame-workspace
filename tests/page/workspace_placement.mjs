// Production functions and event routing, with a minimal DOM and authenticated API fixture.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const src = readFileSync(new URL('../../ui/index.html', import.meta.url), 'utf8');
const grab = name => src.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n}\\n`))[0];
const ids = [...src.matchAll(/id="(mv(?:Workspace|Placement|Width|Distance|Mode|Quality|Separate|Errors|Arrange|Viewer|Mouse)[^"]*)"/g)].map(m=>m[1]);
const dom = Object.fromEntries(ids.map(id=>[id, {disabled:false, dataset:{}, options:[], setAttribute(){}, parentElement:{}}]));
const document = {querySelectorAll: () => []};
let calls=[], resolve, reject;
const api = (path, body)=> {calls.push({path,body}); return new Promise((yes,no)=>{resolve=yes;reject=no;});};
const state = {screen:true, workspace:{state:'running', preset:'wide', placement:{width:2.4,distance:2}, display:{},stream:{}}};
const code = ['workspacePlacement','renderWorkspacePlacement','adjustWorkspacePlacement','renderMvErrors','renderMvViewer','renderMvWorkspace'].map(grab).join('\n');
const routeStart=src.indexOf('$("macview").onclick =');
const routeEnd=src.indexOf('\n// ---- pages:', routeStart);
const macview = {};
const p = new Function('$','api','esc','mvHealth','setTimeout','document', `let mvSeq=0, mvState, mvWorkspaceBusy=false, mvPlacementBusy=false, mvPlacementError='', mvWorkspaceActivePreset=null,mvWorkspacePendingPreset=null, mvViewerBusy=false;
async function loadMacView() {};
${code}
${src.slice(routeStart,routeEnd)}
return {set(s){mvState=s;renderMvWorkspace(s)}, busy(v){mvWorkspaceBusy=v;renderMvWorkspace(mvState)}};`)(id=>id==='macview'?macview:dom[id],api,String,()=>'',()=>{},document);
const click = (field, step)=> {
 const b=dom[`mv${field}${step>0?'Plus':'Minus'}`]; b.dataset={mv:'workspace-adjust',field:field.toLowerCase(),step:String(step)};
 return macview.onclick({target:{closest:()=>b}});
};
p.set(state);
assert.equal(dom.mvWidthValue.textContent,'2.4 m');
let task=click('Width',.2);
assert.deepEqual(calls.at(-1).body,{action:'workspace_adjust',width:2.6,distance:2});
assert.equal(dom.mvWidthValue.textContent,'2.4 m','never display unconfirmed requested value');
assert.ok(dom.mvDistancePlus.disabled && dom.mvWorkspaceStop.disabled && dom.mvWorkspaceStart.disabled);
await click('Distance',.2); assert.equal(calls.length,1,'busy ignores overlapping actions');
resolve({ready:true,placement:{width:2.6,distance:2}});await task;
assert.equal(dom.mvWidthValue.textContent,'2.6 m');
task=click('Distance',-.2);assert.equal(calls.at(-1).body.distance,1.8);
reject(new Error('owner unavailable'));await task;
assert.match(dom.mvPlacementStatus.textContent,/Placement failed: owner unavailable/);
assert.equal(dom.mvDistanceValue.textContent,'—');assert.ok(dom.mvWidthPlus.disabled);
for(const ws of [{state:'stopped'},{state:'running',placement:null},{state:'owner_unknown',placement:{width:2,distance:2}},{state:'running',placement:{width:NaN,distance:2}}]) {
 p.set({...state,workspace:ws}); const count=calls.length;await click('Width',.2);assert.equal(calls.length,count);assert.equal(dom.mvWidthValue.textContent,'—');
}
state.workspace.placement={width:7.95,distance:.45};p.set(state);
task=click('Width',.2);assert.equal(calls.at(-1).body.width,8);resolve({ready:true,placement:{width:8,distance:.45}});await task;assert.ok(dom.mvWidthPlus.disabled);
task=click('Distance',-.2);assert.equal(calls.at(-1).body.distance,.4);resolve({ready:true,placement:{width:8,distance:.4}});await task;assert.ok(dom.mvDistanceMinus.disabled);
p.busy(true);assert.ok(dom.mvWidthMinus.disabled);p.busy(false);
// Show Workspace Again reuses the shared quality menu: mirror the running
// Workspace until the user picks a quality, so reopening never downgrades it.
dom.mvQuality={value:'balanced',dataset:{},options:[{value:'sharp'},{value:'balanced'}]};
p.set({...state,workspace:{...state.workspace,quality:'sharp'}});assert.equal(dom.mvQuality.value,'sharp');
dom.mvQuality.value='balanced';dom.mvQuality.dataset.touched='1';
p.set({...state,workspace:{...state.workspace,quality:'sharp'}});assert.equal(dom.mvQuality.value,'balanced');
console.log('PASS Workspace production rendering/event routing: confirmed values, increments, boundaries, busy, error, owner gating and quality mirroring');
