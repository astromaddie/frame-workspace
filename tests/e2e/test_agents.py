"""Real HTTP/MCP adapter against fake-Frame SSH; no model service needed."""
import json
from pathlib import Path
import sys

import harness
from harness import api, ok, finished, ssh

harness.require()

sys.path.insert(0, str(harness.ROOT / 'ui'))
import frame_mcp


class Agents(harness.FrameTestCase):
    def client(self):
        return frame_mcp.Client('http://127.0.0.1:%d' % harness.Server.port)

    def call(self, name, args):
        return json.loads(frame_mcp.call(self.client(), name, args)['content'][0]['text'])

    def approve(self, proposal):
        ok('POST', '/api/agent/approval', {'confirmation': proposal['confirmation'], 'accept': True})
        return proposal['confirmation']

    def test_status_and_approved_install_job(self):
        self.assertIn('battery', self.call('status', {}))
        proposal = self.call('install', {'id': 'org.example.AgentTest'})
        before = api('POST', '/api/agent/call', {'name': 'install', 'arguments': {'id': 'org.example.AgentTest'}, 'confirmation': proposal['confirmation']})
        self.assertEqual(before[0], 400)
        token = self.approve(proposal)
        job = self.call('install', {'id': 'org.example.AgentTest', 'confirmation': token})
        self.assertFalse(finished(job).get('error'))
        self.assertIn('org.example.AgentTest', ssh('flatpak list --app --columns=application'))
        denied = api('POST', '/api/agent/call', {'name': 'install', 'arguments': {'id': 'org.example.AgentTest'}, 'confirmation': token})
        self.assertEqual(denied[0], 400)

    def test_approved_file_and_text(self):
        path = Path(self.path('agent-note.txt'))
        path.write_text('MCP file content\n')
        args = {'path': str(path)}
        token = self.approve(self.call('send_file', args))
        self.call('send_file', {**args, 'confirmation': token})
        self.assertEqual(ssh('cat ~/Downloads/agent-note.txt'), path.read_text())
        args = {'text': 'MCP clipboard text'}
        token = self.approve(self.call('send_text', args))
        self.call('send_text', {**args, 'confirmation': token})
        self.assertEqual(harness.state()['clipboard'], ['MCP clipboard text'])
