// Real Control Center logic with a minimal DOM; no network, browser or headset.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const cc = require('../ui/control-center.js');

const sample = () => ({time:Date.now()/1000, hostname:'Test Frame', battery:{percent:99,status:'Discharging',watts:0,tempC:33,health:'Good'},
  power:{type:'C PD [PD_PPS]',watts:18}, disk:{home:{total:256*1073741824,free:128*1073741824}},
  memory:{total:16*1073741824,available:6*1073741824}, wifi:{ssid:'Test LAN',signal:80},
  performance:{compositorFps:90,appFps:45,frameMs:11.11,gpuMs:3,cpuPercent:12,gpuMHz:600},
  volume:{level:.4,muted:false}, os:{version:'0.4.1',build:'test-build'},services:{steamvr:true}, thermalAlerts:[],games:[],flatpaks:[]});
function fixture() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {id,hidden:false,disabled:false,textContent:'',innerHTML:'',value:'',dataset:{},
      classList:{toggle(){}},querySelectorAll(){return [];},setAttribute(key,value){this[key]=value;},addEventListener(key,callback){this[key]=callback;},focus(){this.focused=true;}});
    return elements.get(id);
  }
  const cards = cc.catalog.map(item=>{const e=element(item.id);e.dataset.ccCard=item.id;return e;});
  const buttons = cc.categories.map(([id])=>{const e=element('category-'+id);e.dataset.ccCategory=id;e.querySelector=()=>element('count-'+id);return e;});
  const metrics = Object.keys(cc.metrics(null)).map(id=>{const e=element('metric-'+id);e.dataset.ccMetric=id;return e;});
  const page = element('controlCenter');
  page.querySelectorAll = selector => selector === '[data-cc-category]' ? buttons : selector === '[data-cc-metric]' ? metrics : selector === '[data-cc-mac-link]' ? [element('macLink')] : [];
  page.querySelector = selector => selector === '[href="#connection"]' ? element('connectionLink') : cards.find(e=>selector === `[data-cc-card="${e.dataset.ccCard}"]`);
  return {document:{getElementById:element},element,cards,buttons};
}
function mounted(options = {}) {
  const f = fixture(), calls = [];
  let context = {online:true,generation:1,device:'frame-a'}, refreshes = 0, opened = 0;
  const ui = cc.mount({document:f.document,platform:'darwin',api:async(...args)=>{calls.push(args);return {message:'Volume updated'};},
    refresh:async()=>{refreshes++;},getContext:()=>context,openConnection:()=>opened++,...options});
  return {...f,ui,calls,get context(){return context;},set context(v){context=v;},get refreshes(){return refreshes;},get opened(){return opened;}};
}
const pageSource = fs.readFileSync(require.resolve('../ui/index.html'),'utf8');
const deferred = () => { let resolve, reject; const promise=new Promise((ok,bad)=>{resolve=ok;reject=bad;});return {promise,resolve,reject}; };
const flush = async () => { for(let i=0;i<12;i++)await Promise.resolve(); };
// Execute the actual shared refresh/deduplication, host callback and mount
// bootstrap, including the real Home-to-Control status handoff. Only unrelated
// rendering helpers, timers and transport are stubbed.
function sharedPage({productionApi=false} = {}) {
  const f=fixture(), requests=[], writes=[], host=deferred(), job=deferred();
  const context={document:f.document,FrameControlCenter:cc,window:{frameApp:{setUpConnection(){}}},
    Date,setInterval,clearInterval,setTimeout:()=>1,clearTimeout(){},$:f.element,QUICK:[],installing:new Set(),
    battery(){},setVolume(){},gb:String,dur:String,esc:String,meter(){},toast(){},
    runJob(title,id){context.installing.add(id);return job.promise;},
    api:(path,body,device)=>{
      if(path==='/api/host')return host.promise;
      if(path==='/api/status'){const request=deferred();requests.push(request);return request.promise;}
      writes.push({path,body,device});return Promise.resolve({message:'Volume updated'});
    },renderPerformance(){},setConn(){},setOnline(ok){vm.runInContext(`online = ${ok}`,context);},log(){},failed(){},
    watchConnection(){context.watched=true;}};
  vm.createContext(context);
  const stateDeclaration=pageSource.match(/let state = null[^\n]*/)[0];
  vm.runInContext(stateDeclaration+'\nlet online = true, devGen = 1; const HOST = {computer:"computer"}; window.connDevice="frame-a";',context);
  const refreshStart=pageSource.indexOf('let refreshing = null'),refreshEnd=pageSource.indexOf('// ---- our VR HUD',refreshStart);
  const renderStart=pageSource.indexOf('function render(s) {'),renderEnd=pageSource.indexOf('// ---- view ----',renderStart);
  vm.runInContext(pageSource.slice(renderStart,renderEnd)+pageSource.slice(refreshStart,refreshEnd),context);
  if(productionApi) {
    // Keep api/onConnection/setOnline intact: defer fetch itself, not api(), so
    // rejection classification and the real switch/reset notifications run.
    context.fetch=(path,opts)=>{
      if(path==='/api/host')return host.promise.then(data=>({ok:true,status:200,json:async()=>data}));
      const request=deferred();request.path=path;request.opts=opts;requests.push(request);return request.promise;
    };
    Object.assign(context,{renderPill(){},renderConnDlg(){},renderBanner(){},viaText(){},netLabel(){},
      resetShots(){},loadDevices(){},renderDevices(){},renderTests(){},reloadAll(){},checkContactPrompt(){}});
    vm.runInContext(`const UI_KEY = 'test-only';
      const link = {s:null,device:'frame-a',phase:'connected',rev:0,net:null};
      const disp = {}, gm = {seq:0}, cat = {}, wi = {};
      let viewGen = 0, lastImg = null, lastShot = null, androidApps = [], titlesSeq = 0, page = 'control';`,context);
    const apiStart=pageSource.indexOf('const HEADSET_FREE ='),apiEnd=pageSource.indexOf('// What a panel shows',apiStart);
    const onlineStart=pageSource.indexOf('function setOnline('),onlineEnd=pageSource.indexOf('function reloadAll()',onlineStart);
    const connectionStart=pageSource.indexOf('function onConnection('),connectionEnd=pageSource.indexOf('// Read the event stream',connectionStart);
    vm.runInContext(pageSource.slice(apiStart,apiEnd)+pageSource.slice(onlineStart,onlineEnd)+pageSource.slice(connectionStart,connectionEnd),context);
  }
  const installStart=pageSource.indexOf('function installFlatpak(id) {'),installEnd=pageSource.indexOf('$("fpInstall").onclick',installStart);
  vm.runInContext(pageSource.slice(installStart,installEnd),context);
  const hostStart=pageSource.lastIndexOf('api("/api/host").then(h =>'),hostEnd=pageSource.indexOf('</script>',hostStart);
  vm.runInContext(pageSource.slice(hostStart,hostEnd),context);
  const bootstrapStart=pageSource.indexOf('window.frameControlCenter = FrameControlCenter.mount('),bootstrapEnd=pageSource.indexOf('</script>',bootstrapStart);
  return {...f,context,requests,writes,host,refresh:()=>vm.runInContext('refresh()',context),
    switchHeadset:()=>vm.runInContext('onConnection({now:Date.now()/1000,phase:"connected",devices_rev:0,device:{id:"frame-b",name:"Second Frame"}})',context),
    installFlatpak:()=>vm.runInContext('installFlatpak("test.app")',context),
    mount(){vm.runInContext(pageSource.slice(bootstrapStart,bootstrapEnd),context);return context.window.frameControlCenter;}};
}
const response = data => ({ok:true,status:200,json:async()=>data});
for(const rejection of ['transport','body'])for(const delayedMount of [false,true]) {
  test(`production ${rejection} rejection from A preserves B ${delayedMount ? 'before delayed mount' : 'after mount'}`, async t => {
    const f=sharedPage({productionApi:true});
    if(!delayedMount){const ui=f.mount();t.after(()=>ui.destroy());}
    const old=f.refresh(),body=deferred();
    assert.equal(f.requests[0].path,'/api/status');
    if(rejection==='body') {
      f.requests[0].resolve({ok:true,status:200,json:()=>body.promise});await flush();
    }
    f.switchHeadset();
    assert.equal(f.requests.length,2,'real connected B callback starts its own status fetch');
    const next=sample();next.hostname='Second Frame';next.battery.percent=20;next.volume.level=.2;
    f.requests[1].resolve(response(next));await flush();
    const receipt=vm.runInContext('stateReceivedAt',f.context);
    assert.equal(vm.runInContext('stateRead.valid && online && devGen === 2',f.context),true);
    if(!delayedMount)assert.equal(f.element('metric-battery').textContent,'20%');
    (rejection==='body' ? body : f.requests[0]).reject(Error('Old A request failed'));await old;
    assert.equal(vm.runInContext('stateRead.valid',f.context),true,'A must not invalidate B');
    assert.equal(vm.runInContext('stateRead.error',f.context),null);
    assert.equal(vm.runInContext('stateReceivedAt',f.context),receipt);
    if(delayedMount){const ui=f.mount();t.after(()=>ui.destroy());}
    const assertB=()=>{
      assert.equal(f.element('ccDeviceName').textContent,'Second Frame');
      assert.equal(f.element('metric-battery').textContent,'20%');
      assert.equal(f.element('ccVolume').value,20);
      for(const id of ['ccVolume','ccApplyVolume','ccMute'])assert.equal(f.element(id).disabled,false);
      assert.doesNotMatch(f.element('ccSampleState').textContent,/No live sample|local server|Old A/);
    };
    assertB();vm.runInContext('render(state)',f.context);assertB();
    assert.equal(f.requests.length,2,'preservation and redraw need no recovery poll');
    // An ordinary failure on B must still invalidate the current sample.
    const failed=f.refresh();
    if(rejection==='body')f.requests[2].resolve({ok:true,status:200,json:async()=>{throw Error('B body failed');}});
    else f.requests[2].reject(Error('B transport failed'));
    await failed;
    assert.equal(vm.runInContext('stateRead.valid',f.context),false);
    assert.equal(f.element('metric-battery').textContent,'Unavailable');
    for(const id of ['ccVolume','ccApplyVolume','ccMute'])assert.equal(f.element(id).disabled,true);
    assert.equal(f.element('ccStatusLabel').textContent,'No live sample');
  });
}
test('production headset-free API keeps transport errors and responses across a switch', async t => {
  for(const failure of [false,true]) {
    const f=sharedPage({productionApi:true}),ui=f.mount();t.after(()=>ui.destroy());
    const pending=vm.runInContext('api("/api/devices")',f.context);
    const result=pending.then(value=>({value}),error=>({error}));
    f.switchHeadset();f.requests[1].resolve(response(sample()));await flush();
    if(failure)f.requests[0].reject(Error('Device list transport failed'));
    else f.requests[0].resolve(response({devices:['frame-b']}));
    const settled=await result;
    if(failure){assert.equal(settled.error.stale,undefined);assert.match(settled.error.message,/local server isn't running/);}
    else assert.deepEqual(settled.value,{devices:['frame-b']});
    assert.equal(vm.runInContext('stateRead.valid',f.context),true);
  }
});
test('search finds settings by aliases, all words, category and guidance', () => {
  const find = (q,category='all')=>cc.catalog.filter(item=>cc.matches(item,q,category)).map(item=>item.id);
  assert.ok(find('DPI').includes('display'));
  assert.ok(find('sleep inactivity').includes('power'));
  assert.ok(find('MOTION SICKNESS').includes('playspace'));
  assert.ok(find('font','apps').includes('display'));
  assert.deepEqual(find('font','vr'),[]);
  assert.deepEqual(find('<script>alert(1)</script>'),[]);
  assert.ok(find('   ').length > 20);
});
test('metrics use real fields, preserve zero, and distinguish unavailable values', () => {
  const m = cc.metrics(sample());
  assert.equal(m.battery,'99%');
  assert.equal(m.power,'0 W');
  assert.match(m.batteryDetail,/not charging/);
  assert.equal(m.fps,'90'); assert.equal(m.appFps,'45');
  assert.equal(m.storage,'128 GiB'); assert.equal(m.memory,'10 GiB');
  assert.equal(m.thermal,'No hot / critical trip reported');
  assert.equal(cc.metrics(null).fps,'Unavailable');
  assert.equal(cc.metrics({performance:{appFps:null,gpuMHz:0,cpuPercent:0}}).appFps,'Unavailable');
  assert.equal(cc.metrics({performance:{cpuPercent:0}}).cpu,'0%');
  assert.equal(cc.metrics({battery:{percent:0,status:'Discharging'}}).battery,'0%');
  assert.match(cc.metrics({thermalAlerts:[{zone:'battery',tempC:45,limitC:44}]}).thermal,/battery 45 °C ≥ 44 °C/);
});
test('audio controller validates before any API call and rejects offline or expired readings', async () => {
  let context={online:true,fresh:true,sample:sample(),device:'frame-a',generation:1},calls=[];
  const controller=cc.createController({api:async(...args)=>{calls.push(args);return {};},getContext:()=>context});
  for (const change of [{level:-.1},{level:1.1},{level:NaN},{level:Infinity},{level:'0.5'},{level:null},{muted:'false'},{unknown:true},{}]) {
    await assert.rejects(controller.audio(change),/valid mute state/);
  }
  context.online=false;await assert.rejects(controller.audio({level:.5}),/Connect and refresh/);
  context.online=true;context.fresh=false;await assert.rejects(controller.audio({muted:true}),/Connect and refresh/);
  assert.equal(calls.length,0);
});
test('audio uses the captured device, prevents duplicate writes, and ignores switched-headset success', async () => {
  let context={online:true,fresh:true,sample:sample(),device:'frame-a',generation:1},complete,calls=[];
  const controller=cc.createController({api:(...args)=>{calls.push(args);return new Promise(r=>complete=r);},getContext:()=>context});
  const pending=controller.audio({level:.7});
  await assert.rejects(controller.audio({muted:true}),/still in progress/);
  assert.deepEqual(calls,[['/api/volume',{level:.7},'frame-a']]);
  context={...context,device:'frame-b',generation:2};complete({message:'Done'});
  assert.equal(await pending,null);assert.equal(controller.busy,false);
});
test('controller surfaces real API errors and allows a deliberate retry', async () => {
  let calls=0;
  const controller=cc.createController({api:async()=>{if(++calls===1)throw Error('SSH connection lost');return {message:'Done'};},
    getContext:()=>({online:true,fresh:true,sample:sample(),device:'frame-a',generation:1})});
  await assert.rejects(controller.audio({muted:true}),/SSH connection lost/);
  assert.equal(controller.busy,false);
  assert.deepEqual(await controller.audio({muted:true}),{message:'Done'});
});
test('mounted controls only write on Apply or Mute, show readback, and reset on switch', async t => {
  const f=mounted();t.after(()=>f.ui.destroy());
  assert.equal(f.element('ccApplyVolume').disabled,true);
  f.ui.status(sample());
  assert.equal(f.element('ccApplyVolume').disabled,false);
  assert.equal(f.element('ccVolume').value,40);
  f.element('ccVolume').value='65';f.element('ccVolume').oninput();
  assert.equal(f.calls.length,0);
  assert.equal(f.element('ccVolumeValue').textContent,'65%');
  f.ui.status(sample()); // a poll must not overwrite an unapplied value
  assert.equal(f.element('ccVolume').value,'65');
  await f.element('ccApplyVolume').onclick();
  assert.deepEqual(f.calls,[['/api/volume',{level:.65},'frame-a']]);
  assert.equal(f.refreshes,1);
  await f.element('ccMute').onclick();
  assert.deepEqual(f.calls[1],['/api/volume',{muted:true},'frame-a']);
  f.context={online:null,generation:2,device:'frame-b'};f.ui.reset();
  assert.equal(f.element('ccApplyVolume').disabled,true);
  assert.equal(f.element('metric-battery').textContent,'Unavailable');
  assert.equal(f.element('ccAudioResult').textContent,'');
});
test('offline/errors clear live readings, disable audio and keep companion navigation usable', async t => {
  const f=mounted();t.after(()=>f.ui.destroy());f.ui.status(sample());
  f.context.online=false;f.ui.connection(false);
  assert.equal(f.element('metric-fps').textContent,'Unavailable');
  assert.equal(f.element('ccVolume').disabled,true);
  await f.element('ccMute').onclick();
  assert.equal(f.calls.length,0);
  assert.match(f.element('ccAudioResult').textContent,/Connect and refresh/);
  assert.equal(f.cards.find(c=>c.dataset.ccCard==='privacy').hidden,false);
  f.ui.unavailable(Error('Local server is unavailable'));
  assert.match(f.element('ccSampleState').textContent,/Local server is unavailable/);
  f.context.online=true;f.ui.status(sample());
  assert.equal(f.element('ccVolume').disabled,false);
});
test('mounted audio failure is visible and never reports success', async t => {
  const f=mounted({api:async()=>{throw Error('Permission denied');}});t.after(()=>f.ui.destroy());f.ui.status(sample());
  await f.element('ccApplyVolume').onclick();
  assert.match(f.element('ccAudioResult').textContent,/Couldn’t apply audio: Permission denied/);
  assert.equal(f.element('ccApplyVolume').disabled,false);
  assert.equal(f.refreshes,0);
});
test('audio confirmation depends on fresh readback rather than command acknowledgment', async t => {
  let expected=.65, mode='confirmed';
  const f=mounted({refresh:async()=>{
    if(mode==='offline')return f.ui.unavailable(Error('Headset unreachable'));
    const reading=sample();reading.volume.level=mode==='different' ? .64 : expected;f.ui.status(reading);
  }});t.after(()=>f.ui.destroy());f.ui.status(sample());
  f.element('ccVolume').value='65';await f.element('ccApplyVolume').onclick();
  assert.match(f.element('ccAudioResult').textContent,/Confirmed: 65%, unmuted/);
  mode='different';f.element('ccVolume').value='65';await f.element('ccApplyVolume').onclick();
  assert.match(f.element('ccAudioResult').textContent,/reported output differs: 64%/);
  mode='offline';await f.element('ccApplyVolume').onclick();
  assert.match(f.element('ccAudioResult').textContent,/command sent; current output could not be confirmed/);
  assert.equal(f.element('ccApplyVolume').disabled,true);
});
test('pending audio never overwrites the new headset with old success or error', async t => {
  let complete;
  const f=mounted({api:()=>new Promise(resolve=>{complete=resolve;})});t.after(()=>f.ui.destroy());f.ui.status(sample());
  const pending=f.element('ccMute').onclick();
  assert.equal(f.element('ccApplyVolume').disabled,true);
  f.context={online:true,generation:2,device:'frame-b'};f.ui.reset();
  const newSample=sample();newSample.hostname='Second Frame';newSample.volume.muted=true;f.ui.status(newSample);
  complete({message:'Old headset muted'});await pending;
  assert.equal(f.element('ccDeviceName').textContent,'Second Frame');
  assert.equal(f.element('ccAudioResult').textContent,'');
  assert.equal(f.refreshes,0);
  assert.equal(f.element('ccMute').textContent,'Unmute');
  f.ui.unavailable({stale:true,message:'Switched headsets'});
  assert.equal(f.element('metric-battery').textContent,'99%');
  assert.equal(f.element('ccMute').disabled,false);
});
test('pending old-headset volume cannot become a draft on the new headset', async t => {
  for(const failure of [false,true]) {
    const request=deferred(),f=mounted({api:()=>request.promise});t.after(()=>f.ui.destroy());
    f.ui.status(sample());f.element('ccVolume').value='65';f.element('ccVolume').oninput();
    const pending=f.element('ccApplyVolume').onclick();
    f.context={online:true,generation:2,device:'frame-b'};f.ui.reset();
    const next=sample();next.volume.level=.2;f.ui.status(next);
    assert.equal(f.element('ccApplyVolume').disabled,true);
    if(failure)request.reject(Error('Old headset failed'));else request.resolve({message:'Done'});
    await pending;
    assert.equal(f.element('ccVolume').value,20);
    assert.equal(f.element('ccVolumeValue').textContent,'20%');
    assert.equal(f.element('ccAudioReadback').textContent,'Reported 20% · unmuted');
    assert.equal(f.element('ccApplyVolume').disabled,false);
    assert.equal(f.element('ccAudioResult').textContent,'');
  }
});
test('production host response works before and after mounting with the actual bridge shape', async t => {
  for(const early of [true,false]) {
    const f=sharedPage();let ui;
    if(!early){ui=f.mount();t.after(()=>ui.destroy());assert.equal(f.element('macLink').hidden,true);}
    f.host.resolve({os:'macOS',mobile:false});await flush();
    if(early){ui=f.mount();t.after(()=>ui.destroy());}
    assert.equal(f.context.watched,true);
    assert.equal(f.element('macLink').hidden,false);
    f.element('ccSearch').value='mac';f.element('ccSearch').input();
    assert.equal(f.element('macLink').hidden,false);
    ui.host({os:'SteamOS',mobile:true});assert.equal(f.element('macLink').hidden,true);
  }
});
test('production shared refresh drains a pre-command request then samples after acknowledgment', async t => {
  const f=sharedPage(),ui=f.mount();t.after(()=>ui.destroy());ui.status(sample());
  const prior=f.refresh();assert.equal(f.requests.length,1);
  assert.equal(f.refresh(),prior); // the ordinary poll/manual deduplication still holds
  f.element('ccVolume').value='65';f.element('ccVolume').oninput();
  const command=f.element('ccApplyVolume').onclick();await flush();
  assert.equal(f.writes.length,1);assert.equal(f.requests.length,1);
  f.requests[0].resolve(sample());await prior;await flush();
  assert.equal(f.requests.length,2,'readback must issue a real post-command status request');
  assert.doesNotMatch(f.element('ccAudioResult').textContent,/Confirmed|reported output differs/);
  assert.equal(f.element('ccApplyVolume').disabled,true);
  const after=sample();after.volume.level=.65;f.requests[1].resolve(after);await command;
  assert.match(f.element('ccAudioResult').textContent,/Confirmed: 65%, unmuted/);
  assert.equal(f.element('ccApplyVolume').disabled,false);
});
test('production audio readback survives an old poll failure and treats its own failed read as unconfirmed', async t => {
  for(const failedReadback of [false,true]) {
    const f=sharedPage(),ui=f.mount();t.after(()=>ui.destroy());ui.status(sample());
    const prior=f.refresh();f.element('ccVolume').value='65';f.element('ccVolume').oninput();
    const command=f.element('ccApplyVolume').onclick();await flush();
    f.requests[0].reject(Error('Pre-command poll failed'));await prior;await flush();
    assert.equal(f.requests.length,2);
    assert.equal(f.element('ccApplyVolume').disabled,true);
    if(failedReadback)f.requests[1].reject(Error('Readback unavailable'));
    else {const after=sample();after.volume.level=.64;f.requests[1].resolve(after);}
    await command;
    assert.match(f.element('ccAudioResult').textContent,failedReadback ? /command sent; current output could not be confirmed/ : /reported output differs: 64%/);
    assert.equal(f.element('ccApplyVolume').disabled,failedReadback);
  }
});
test('production Flatpak redraw cannot revive a failed status; only a fresh successful poll recovers', async t => {
  const f=sharedPage(),ui=f.mount();t.after(()=>ui.destroy());
  const first=f.refresh(),reading=sample();f.requests[0].resolve(reading);await first;
  const receipt=vm.runInContext('stateReceivedAt',f.context);
  assert.equal(f.element('ccApplyVolume').disabled,false);
  const failed=f.refresh();f.requests[1].reject(Error('Synthetic status parsing error'));await failed;
  assert.equal(f.element('metric-battery').textContent,'Unavailable');
  assert.equal(f.element('ccApplyVolume').disabled,true);
  f.installFlatpak(); // actual production render(state), with job held pending
  assert.equal(f.requests.length,2,'redraw must not start a replacement reading');
  assert.equal(vm.runInContext('stateReceivedAt',f.context),receipt);
  assert.equal(f.element('metric-battery').textContent,'Unavailable');
  assert.equal(f.element('ccStatusLabel').textContent,'No live sample');
  assert.match(f.element('ccSampleState').textContent,/Synthetic status parsing error/);
  for(const id of ['ccVolume','ccApplyVolume','ccMute'])assert.equal(f.element(id).disabled,true);
  const fresh=f.refresh();
  assert.equal(f.element('ccApplyVolume').disabled,true,'starting a poll does not clear failure');
  const next=sample();next.battery.percent=98;next.volume.level=.2;
  f.requests[2].resolve(next);await fresh;
  assert.equal(f.element('metric-battery').textContent,'98%');
  assert.equal(f.element('ccVolume').value,20);
  assert.equal(f.element('ccApplyVolume').disabled,false);
  assert.doesNotMatch(f.element('ccSampleState').textContent,/Synthetic status parsing error/);
});
test('delayed production bootstrap retains failed-read state and fresh-poll recovery', async t => {
  for(const priorSuccess of [true,false]) {
    const f=sharedPage();
    if(priorSuccess){const first=f.refresh();f.requests[0].resolve(sample());await first;}
    const failed=f.refresh();f.requests.at(-1).reject(Error('Status failed before mount'));await failed;
    const calls=f.requests.length,ui=f.mount();t.after(()=>ui.destroy());
    assert.equal(f.requests.length,calls);
    assert.equal(f.element('metric-battery').textContent,'Unavailable');
    assert.equal(f.element('ccStatusLabel').textContent,'No live sample');
    assert.match(f.element('ccSampleState').textContent,/Status failed before mount/);
    for(const id of ['ccVolume','ccApplyVolume','ccMute'])assert.equal(f.element(id).disabled,true);
    const fresh=f.refresh();assert.equal(f.element('ccApplyVolume').disabled,true);
    f.requests.at(-1).resolve(sample());await fresh;
    assert.equal(f.element('metric-battery').textContent,'99%');
    assert.equal(f.element('ccApplyVolume').disabled,false);
    assert.doesNotMatch(f.element('ccSampleState').textContent,/Status failed before mount/);
  }
});
test('same-object replay preserves invalidation and receipt age after a failed reading', t => {
  const f=mounted();t.after(()=>f.ui.destroy());const reading=sample();
  f.ui.status(reading);f.ui.unavailable(Error('Status failure'));
  f.ui.status(reading,Date.now()+1000);
  assert.equal(f.element('metric-battery').textContent,'Unavailable');
  assert.equal(f.element('ccApplyVolume').disabled,true);
  assert.match(f.element('ccSampleState').textContent,/Status failure/);
  f.ui.status(sample());assert.equal(f.element('ccApplyVolume').disabled,false);
});
test('delayed production bootstrap preserves client receipt age despite headset clock skew', async t => {
  const original=Date.now;let now=1000000;Date.now=()=>now;t.after(()=>{Date.now=original;});
  const f=sharedPage(),prior=f.refresh();const reading=sample();reading.time-=3600;
  f.requests[0].resolve(reading);await prior;
  now+=91000;const ui=f.mount();t.after(()=>ui.destroy());
  assert.equal(f.element('metric-battery').textContent,'Unavailable');
  assert.equal(f.element('ccApplyVolume').disabled,true);
  assert.match(f.element('ccSampleState').textContent,/expired/);
  const fresh=f.refresh();f.requests[1].resolve(sample());await fresh;
  assert.equal(f.element('ccApplyVolume').disabled,false);
  assert.equal(f.element('metric-battery').textContent,'99%');
});
test('sample replay cannot renew receipt age; fresh receipts tolerate missing or skewed headset time', t => {
  const original=Date.now;let now=1000000;Date.now=()=>now;t.after(()=>{Date.now=original;});
  const f=mounted();t.after(()=>f.ui.destroy());const reading=sample();f.ui.status(reading);
  now+=91000;f.ui.status(reading);
  assert.equal(f.element('ccApplyVolume').disabled,true);
  assert.equal(f.element('metric-battery').textContent,'Unavailable');
  for(const time of [undefined,null,'invalid',NaN,Infinity,now/1000-3600,now/1000+3600]) {
    f.ui.status({...sample(),time});
    assert.equal(f.element('ccApplyVolume').disabled,false);
    assert.doesNotMatch(f.element('ccSampleState').textContent,/Invalid Date/);
  }
  for(const receipt of [null,NaN,Infinity,now+1000,now-91000]) {
    f.ui.status(sample(),receipt);assert.equal(f.element('ccApplyVolume').disabled,true);
  }
});
test('mute-only readback preserves an unapplied volume until Apply sends it', async t => {
  let reading=sample();const f=mounted({api:async(path,change,device)=>{
    f.calls.push([path,change,device]);reading={...reading,volume:{...reading.volume,...change}};return {message:'Done'};
  },refresh:async()=>f.ui.status({...reading})});t.after(()=>f.ui.destroy());f.ui.status(reading);
  f.element('ccVolume').value='65';f.element('ccVolume').oninput();
  await f.element('ccMute').onclick();
  assert.deepEqual(f.calls[0],['/api/volume',{muted:true},'frame-a']);
  assert.equal(f.element('ccVolume').value,'65');assert.equal(f.element('ccVolumeValue').textContent,'65%');
  assert.match(f.element('ccAudioReadback').textContent,/Reported 40% · muted/);
  assert.match(f.element('ccAudioResult').textContent,/Confirmed: 40%, muted/);
  await f.element('ccApplyVolume').onclick();
  assert.deepEqual(f.calls[1],['/api/volume',{level:.65},'frame-a']);
  assert.equal(f.element('ccVolume').value,65);
  assert.match(f.element('ccAudioResult').textContent,/Confirmed: 65%, muted/);
});
test('live samples expire even when the status poll is paused', t => {
  const original=Date.now;let now=100000;Date.now=()=>now;t.after(()=>{Date.now=original;});
  const f=mounted();t.after(()=>f.ui.destroy());f.ui.status(sample());
  now+=91000;
  // A connection update must also re-check freshness before enabling a control.
  f.ui.connection(true);
  assert.equal(f.element('ccApplyVolume').disabled,true);
});
test('mounted filters show empty state, counts, reset and host-dependent features', t => {
  const f=mounted();t.after(()=>f.ui.destroy());
  f.element('ccSearch').value='dpi';f.element('ccSearch').input();
  assert.equal(f.cards.find(c=>c.dataset.ccCard==='display').hidden,false);
  assert.equal(f.element('ccResultCount').textContent,'1 section');
  f.element('ccSearch').value='nothing-matches-this';f.element('ccSearch').input();
  assert.equal(f.element('ccEmpty').hidden,false);
  f.element('ccResetFilter').onclick();assert.equal(f.element('ccEmpty').hidden,true);
  f.ui.host({os:'Windows'});assert.equal(f.element('macLink').hidden,true);
  assert.equal(f.cards.find(c=>c.dataset.ccCard==='mac').hidden,false);
  f.ui.host({os:'macOS'});assert.equal(f.element('macLink').hidden,false);
  f.ui.host({os:'SteamOS',mobile:true});assert.equal(f.element('macLink').hidden,true);
  f.element('connectionLink').onclick({preventDefault(){}});assert.equal(f.opened,1);
});
test('every linked workflow selects its real page and section, preserving shortcuts', () => {
  const html=fs.readFileSync(require.resolve('../ui/index.html'),'utf8');
  const start=html.indexOf('const PAGES ='),end=html.indexOf('window.addEventListener("hashchange", showPage);',start);
  const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
  let activePage,scrolled;
  const nodes=['home','games','android','tools','devices','control'].map(page=>({dataset:{page},classList:{toggle(key,on){if(on)activePage=page;}}}));
  const sections=new Map();
  const context={location:{hash:'#control'},document:{querySelectorAll:s=>s==='.page'?nodes:[],title:''},
    window:{scrollTo(){}},$:id=>{
      if(!ids.has(id))return null;
      if(!sections.has(id))sections.set(id,{hidden:false,scrollIntoView(){scrolled=id;}});
      return sections.get(id);
    },live:false,toggleLive(){},loadMacView(){},loadPanelSwitcher(){}};
  const androidStart=html.indexOf('function showAndroidTab(');
  const androidEnd=html.indexOf("document.querySelectorAll('[data-store-tab]').forEach(button => button.onclick",androidStart);
  vm.createContext(context);vm.runInContext(html.slice(androidStart,androidEnd)+html.slice(start,end)+'\nPAGES.push("devices", "control");',context);
  vm.runInContext('showPage()',context);assert.equal(activePage,'control');
  const expected={view:'home',shots:'home',vrTools:'home',comfort:'home',pad:'home',library:'games',getgames:'games',sideloaded:'games',android:'android',display:'android',androidLibrary:'android',appStore:'android',
    power:'tools',transfer:'tools',media:'tools',apps:'tools',artworkSettings:'tools',privacy:'tools',panels:'tools',macview:'tools',devices:'devices'};
  for(const item of cc.catalog) for(const [href] of item.links||[]) {
    if(!href.startsWith('#')||href==='#connection')continue;
    const id=href.slice(1);assert.ok(ids.has(id)||['android','devices'].includes(id),href);
    context.location.hash=href;scrolled=null;vm.runInContext('showPage()',context);
    assert.equal(activePage,expected[id],href);
    if(!['android','devices'].includes(id))assert.equal(scrolled,id,href);
    if(id==='display'||id==='androidLibrary') { assert.equal(sections.get('androidLibrary').hidden,false);assert.equal(sections.get('appStore').hidden,true); }
    if(id==='appStore') { assert.equal(sections.get('appStore').hidden,false);assert.equal(sections.get('androidLibrary').hidden,true); }
  }
  assert.equal(vm.runInContext('PAGES[4]',context),'devices');
  assert.equal(vm.runInContext('PAGES[5]',context),'control');
});
