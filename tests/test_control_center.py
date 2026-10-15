"""Control Center API guards and shared-page behavior, without a real headset."""
import sandbox  # noqa: F401 (first: isolates app state, SSH and telemetry)
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import server
import frame_status


class AudioValidation(unittest.TestCase):
    def test_invalid_values_do_not_touch_device_even_with_valid_mute(self):
        invalid = [{}, {'level': None}, {'level': True}, {'level': '0.5'}, {'level': []},
                   {'level': float('nan')}, {'level': float('inf')}, {'level': -1}, {'level': 2},
                   {'muted': 'false'}, {'muted': 1}, {'muted': None}, {'action': 'mute'},
                   {'level': -1, 'muted': True}, {'level': .5, 'muted': 'yes'}]
        for body in invalid:
            with self.subTest(body=body), mock.patch.object(server, 'ssh') as ssh:
                with self.assertRaises(server.Failure) as raised:
                    server.set_volume(body)
                self.assertEqual(raised.exception.status, 400)
                ssh.assert_not_called()

    def test_supported_actions_use_wpctl_with_exact_values(self):
        for body, commands in [({'level': 0}, ['wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.00']),
                               ({'level': 1}, ['wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.00']),
                               ({'muted': False}, ['wpctl set-mute @DEFAULT_AUDIO_SINK@ 0']),
                               ({'level': .65, 'muted': True}, ['wpctl set-mute @DEFAULT_AUDIO_SINK@ 1',
                                                              'wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.65'])]:
            with self.subTest(body=body), mock.patch.object(server, 'ssh', return_value='') as ssh:
                self.assertEqual(server.set_volume(body)['message'], 'Volume updated')
                self.assertEqual([c.args[0] for c in ssh.call_args_list], commands)

    def test_device_error_propagates_without_claiming_success(self):
        with mock.patch.object(server, 'ssh', side_effect=server.Failure('Headset offline')):
            with self.assertRaisesRegex(server.Failure, 'Headset offline'):
                server.set_volume({'level': .5})


class Coverage(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node exercises production page JS')
    def test_control_center_behavior(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/control_center.cjs')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shared_assets_ship_to_iphone_and_desktop(self):
        spec = importlib.util.spec_from_file_location('frame_bundle_cc', ROOT / 'ios/scripts/make_frame_bundle.py')
        bundle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bundle)
        files = set(bundle.files())
        for name in ('control-center.js', 'control-center.css', 'artwork-settings.js'):
            self.assertIn(ROOT / 'ui' / name, files)
        config = json.loads((ROOT / 'app/package.json').read_text())
        ui = next(item for item in config['build']['extraResources'] if item['from'] == '../ui')
        self.assertIn('*.js', ui['filter'])
        self.assertIn('*.css', ui['filter'])

    def test_status_exposes_kernel_thermal_trips_without_guessing_thresholds(self):
        # Run the real aggregator with every external probe stubbed.
        names = ('read', 'process_names', 'performance', 'os_release', 'battery', 'power_source',
                 'disk', 'memory', 'max_temp', 'wifi', 'ip_addr', 'volume', 'port_listening', 'games', 'flatpaks')
        with mock.patch.multiple(frame_status, **{name: mock.DEFAULT for name in names}) as probes, \
             mock.patch.object(frame_status, 'thermal_alerts', return_value=[{'zone':'battery','tempC':45,'limitC':44}]):
            probes['read'].return_value = '120 0'
            probes['process_names'].return_value = set()
            result = frame_status.status()
        self.assertEqual(result['thermalAlerts'], [{'zone':'battery','tempC':45,'limitC':44}])
