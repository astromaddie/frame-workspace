"""The install queue (server.queue_job): Android, Quest and Meta installs run one after another."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'ui'))
import server  # noqa: E402


def wait(job, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        j = server.job_status(f'id={job}')
        if j['done']:
            return j
        time.sleep(0.01)
    raise AssertionError(f'job {job} never finished')


class InstallQueue(unittest.TestCase):
    def setUp(self):
        self.assertEqual(server._queue, [])

    def blocker(self):
        """A queued install that runs until released."""
        go, started = threading.Event(), threading.Event()

        def work():
            started.set()
            go.wait(5)
            return {'message': 'first done'}
        job = server.queue_job('Install first', work)['job']
        self.assertTrue(started.wait(5))
        self.addCleanup(go.set)
        return job, go

    def test_runs_in_order_one_at_a_time(self):
        order, running = [], []

        def work(name):
            def run(report):
                running.append(name)
                self.assertEqual(len(running), 1, 'two installs ran at once')
                report('Copying', 50)
                time.sleep(0.02)
                order.append(name)
                running.remove(name)
                return {'message': f'{name} done'}
            return run
        first, go = self.blocker()
        started = [server.queue_job(f'Install {n}', work(n), progress=True) for n in ('a', 'b', 'c')]
        self.assertEqual([s['queued'] for s in started], [1, 2, 3])
        self.assertIn('queued, 3 before it', started[2]['message'])
        q = server.install_queue()['jobs']
        self.assertEqual([j['label'] for j in q], ['Install first', 'Install a', 'Install b', 'Install c'])
        self.assertEqual([j['queued'] for j in q], [False, True, True, True])
        self.assertEqual(q[1]['stage'], 'Queued: 1 install before it')
        self.assertEqual(q[3]['stage'], 'Queued: 3 installs before it')
        go.set()
        self.assertEqual(wait(first)['message'], 'first done')
        for s, n in zip(started, 'abc'):
            j = wait(s['job'])
            self.assertEqual((j['message'], j['error'], j['queued']), (f'{n} done', None, False))
        self.assertEqual(order, ['a', 'b', 'c'])
        self.assertEqual(server.install_queue(), {'jobs': []})

    def test_a_failure_does_not_stop_the_rest(self):
        def fail():
            raise server.Failure('no room on the Frame')
        bad = server.queue_job('Install bad', fail)['job']
        good = server.queue_job('Install good', lambda: {'message': 'ok'})['job']
        self.assertEqual(wait(bad)['error'], 'no room on the Frame')
        self.assertEqual(wait(good)['message'], 'ok')

    def test_a_waiting_install_can_be_removed_but_not_a_running_one(self):
        ran = []
        first, go = self.blocker()
        second = server.queue_job('Install second', lambda: ran.append(2) or {})['job']
        third = server.queue_job('Install third', lambda: ran.append(3) or {})['job']
        self.assertIn('Removed Install second', server.queue_action({'action': 'remove', 'job': second})['message'])
        j = server.job_status(f'id={second}')
        self.assertTrue(j['done'] and j['cancelled'])
        self.assertEqual(server.job_status(f'id={third}')['stage'], 'Queued: 1 install before it')
        for body, status in (({'action': 'remove', 'job': first}, 409), ({'action': 'remove', 'job': second}, 409),
                             ({'action': 'nope', 'job': third}, 400)):
            with self.assertRaises(server.Failure) as e:
                server.queue_action(body)
            self.assertEqual(e.exception.status, status)
        go.set()
        wait(third)
        self.assertEqual(ran, [3])

    def test_runs_only_on_the_headset_it_was_queued_for(self):
        class Link:
            device = 'frame-a'

            def active_device(self):
                return {'id': self.device}
        link = Link()
        with patch.object(server, 'LINK', link):
            first, go = self.blocker()
            ran = []
            later = server.queue_job('Install later', lambda: ran.append(1) or {})['job']
            link.device = 'frame-b'
            go.set()
            self.assertIn('switched headsets', wait(later)['error'])
        self.assertEqual(ran, [])

    def test_installs_from_each_place_are_queued(self):
        with patch.object(server, 'ensure_master'), patch.object(server, 'queue_job', return_value={'job': 'j'}) as q:
            server.quest({'action': 'import', 'package': 'com.example.game'})
            server.meta({'action': 'install', 'app': '123'})
            with patch.object(server.frame_catalog, 'app'):
                server.android({'action': 'install', 'package': 'com.example.game'})
            server.android({'action': 'install', 'package': 'com.example.game', 'url': 'https://f-droid.org/x.apk'})
            with patch.object(server.apk_search, 'resolve', return_value=(None, {'enabled': True})):
                server.source_install({'source': 'fdroid', 'id': 'com.example.game'})
        self.assertEqual(q.call_count, 5)


if __name__ == '__main__':
    unittest.main()
