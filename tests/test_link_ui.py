"""The connection pill, the offline banner and the Devices tab's selection: the page's own
functions from ui/index.html, run in Node against a minimal DOM (#87)."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = (ROOT / "ui/index.html").read_text(encoding="utf-8")


def between(start, end):
    return PAGE[PAGE.index(start):PAGE.index(end)]


SETUP = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {id, textContent: '', innerHTML: '', title: '', hidden: true, value: ''});
  return elements.get(id);
};
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const serverNow = () => 1000;
const activeStage = s => (s.stages || []).find(x => x.state === 'active');
const ago = () => 'a minute ago';
const openTrust = () => {};
const window = {};
let online = false;
const link = {live: true, s: null};
const dv = {list: [], sel: null, data: null, found: 'x', editing: 'y'};
let rendered = 0;
const renderDevices = () => { rendered++; };
let refuse = false;
const act = async (label, fn) => refuse ? undefined : fn();
const api = async () => ({ok: true});
const STAGES = [{id: 'find', label: 'Finding the headset', state: 'done'},
                {id: 'identity', label: "Checking the headset's identity", state: 'failed'}];
const changed = {phase: 'failed', retry_at: 1012, device: {id: 'd1', name: 'Office Frame'}, network: {name: 'Home'},
  stages: STAGES, probes: [{host: 'frame.local', detail: 'SSH identity changed'}],
  error: {stage: 'identity', short: 'SSH identity changed', hostkey: {state: 'changed'},
          message: 'This address answered with a different SSH identity from the one saved for Office Frame. '
                 + 'If not, a different device may be answering here.'}};
'''


@unittest.skipUnless(shutil.which("node"), "Node runs the page's own functions")
class LinkUI(unittest.TestCase):
    def run_page(self, checks):
        code = (between("function netLabel(s) {", "function renderConnDlg() {")
                + between("async function useDevice(id) {", "async function devAction(")
                + between("function deviceStatus(d) {", "function renderDevices() {"))
        script = SETUP + code + "\n(async () => {\n" + checks + "\n})().catch(e => { console.error(e); process.exitCode = 1; });"
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_pill_leads_with_the_reason(self):
        # The header pill cut off "Offline: checking the headset's identity failed · retry in…":
        # the reason comes first, in a few words, and only the countdown gets cut.
        self.run_page(r'''
  link.s = changed;
  renderPill();
  assert.match($('conn').innerHTML, /<b>Office Frame<\/b><span>SSH identity changed · retry in 12 s<\/span>/);
  assert.equal($('conn').title, 'Office Frame is offline: SSH identity changed · retry in 12 s. Click for details.');
  // Without a short reason: the stage that failed.
  link.s = {...changed, retry_at: null, error: {stage: 'identity', message: 'Something.'}};
  renderPill();
  assert.match($('conn').innerHTML, /<span>Checking the headset&#39;s identity failed<\/span>/);
''')

    def test_the_banner_never_doubles_a_full_stop(self):
        # "…may be answering here.." and "…(Developer Mode must be on).." (#87).
        self.run_page(r'''
  link.s = changed;
  renderBanner();
  assert.equal($('offDetail').textContent, 'Office Frame on Home. Tried frame.local: SSH identity changed. Trying again in 12 s.');
  link.s = {...changed, probes: [
    {host: 'frame.local', detail: "The Frame didn't accept this computer's SSH key. Set Up Connection pairs this computer with it again (Developer Mode must be on)."},
    {host: '100.64.0.2', detail: 'No answer'}]};
  renderBanner();
  const text = $('offDetail').textContent;
  assert.ok(!/\.\./.test(text), text);
  assert.ok(text.includes("frame.local: the Frame didn't accept"), text);
  assert.ok(text.includes('must be on); 100.64.0.2: no answer. Trying'), text);
  assert.equal(asClause('SSH identity changed.'), 'SSH identity changed');  // abbreviations keep their case
  assert.equal(asClause('Answered in 3 ms'), 'answered in 3 ms');
''')

    def test_the_devices_list_keeps_the_whole_message_within_reach(self):
        self.run_page(r'''
  link.s = changed;
  const [dot, text, full] = deviceStatus({active: true, addresses: []});
  assert.equal(dot, 'off');
  assert.equal(text, 'SSH identity changed');
  assert.equal(full, changed.error.message);  // the row's tooltip
''')

    def test_using_a_headset_selects_it(self):
        # After "Use this headset" on Office Frame, the last headset's details stayed on show.
        self.run_page(r'''
  dv.list = [{id: 'd0', name: 'Living Room Frame', active: true}, {id: 'd1', name: 'Office Frame', active: false}];
  dv.sel = 'd0';
  await useDevice('d1');
  assert.equal(dv.sel, 'd1');
  assert.equal(dv.found, null);
  assert.equal(dv.editing, null);
  assert.equal(rendered, 1);
  // Refused (an install is running): the selection stays, and the header shows the one in use.
  dv.list = [{id: 'd0', name: 'Living Room Frame', active: true}, {id: 'd2', name: 'Spare', active: false}];
  dv.sel = 'd0'; refuse = true;
  await useDevice('d2');
  assert.equal(dv.sel, 'd0');
  assert.equal($('devSel').value, 'd0');
''')


class HeaderLayout(unittest.TestCase):
    def test_the_devices_tab_explanation_wraps(self):
        # It was cut to one line with no way to read the rest.
        self.assertIn(".dev-problem .s { white-space: normal; }", PAGE)
        self.assertEqual(len(re.findall(r'<div class="item dev-problem" role="alert">', PAGE)), 2)

    def test_the_header_buttons_dont_give_way(self):
        # Refresh overhung the right edge at 1280 px: the pill (and the headset picker) shrink, not the buttons.
        header = between("<header>", "</header>")
        end = header[header.index('<div class="hdr-end">'):]
        for el in ('id="conn"', 'id="devSel"', 'id="battChip"', 'id="reportBtn"', 'id="refreshAll"'):
            self.assertIn(el, end)
        self.assertIn(".hdr-end > :not(.pill, .devsel) { flex: none; }", PAGE)
        self.assertRegex(PAGE, r"\.hdr-end \{[^}]*min-width: 0;[^}]*margin-left: auto;")


if __name__ == "__main__":
    unittest.main()
