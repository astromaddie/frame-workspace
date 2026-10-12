"""Run the actual shared page's comfort renderer against a minimal DOM/bridge."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node exercises the shared page JS')
class ComfortUI(unittest.TestCase):
    def test_notification_failure_survives_poll_until_success(self):
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        code = page[page.index('let comfortBusy ='):page.index('async function pollComfort()')]
        setup = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {textContent:'', hidden:true, disabled:false, type: 'number'});
  return elements.get(id);
};
let denied = 0;
const window = {frameApp:{notify:async()=>{denied++;throw Error('permission denied');}}};
const log = ()=>{}, toast = ()=>{};
'''
        checks = r'''
(async()=>{
  const active = {id:'session-one',active:true,time:100,remaining:120,
    options:{minutes:2,breakMinutes:1,stillMinutes:1,batteryAlert:true,heatAlert:true},
    events:[{id:'event-one',kind:'battery',time:99,message:'Low battery'}]};
  renderComfort(active); // initial history must not replay even a fresh event
  assert.equal(denied,0);
  assert.equal($('comfortAnnouncement').textContent,'');
  active.events.push({id:'event-two',kind:'break',time:100,message:'Take a break'});
  renderComfort(active);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(denied,1);
  assert.equal($('comfortAnnouncement').textContent,'Take a break');
  assert.equal($('comfortNotificationStatus').hidden,false);
  assert.match($('comfortNotificationStatus').textContent,/notification settings/);
  renderComfort({...active,time:105}); // the next normal poll must not erase failure
  assert.equal($('comfortNotificationStatus').hidden,false);
  assert.equal($('sessionStart').disabled,true);
  assert.equal($('sessionMinutes').disabled,true);
  assert.equal($('sessionCancel').disabled,false);
  let requests=0;
  window.frameApp.notify=async()=>{requests++;};
  renderComfort({...active,time:106});
  assert.equal($('comfortAnnouncement').textContent,'Take a break');
  assert.equal(requests,0); // polling does not replay an already-seen event
  await localNotification('test',true);
  assert.equal($('comfortNotificationStatus').hidden,true);
  renderComfort({...active,active:false});
  assert.equal($('sessionStart').disabled,false);
  assert.equal($('sessionMinutes').disabled,false);
  assert.equal($('sessionCancel').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', setup + code + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_poll_waits_while_the_frame_is_unreachable(self):
        page = (ROOT / 'ui/index.html').read_text(encoding='utf-8')
        start = page.index('async function pollComfort()')
        code = page[start:page.index('$("comfortForm").onsubmit', start)]
        js = r'''
const assert = require('node:assert/strict');
const el = {textContent: 'Checking session…'};
const $ = () => el;
let online = false, comfortBusy = false, calls = 0, timers = 0;
const api = async () => { calls++; return {}; };
const renderComfort = () => {};
const setTimeout = () => { timers++; };
''' + code + r'''
(async () => {
  await pollComfort();
  assert.equal(calls, 0);  // no ssh attempt while the status check can't reach the Frame
  assert.match(el.textContent, /when the Frame is connected/);
  assert.equal(timers, 1);  // still scheduled, to resume when it's back
  online = true;
  await pollComfort();
  assert.equal(calls, 1);
  online = null;  // a status check that failed for another reason: keep polling
  await pollComfort();
  assert.equal(calls, 2);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', js], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
