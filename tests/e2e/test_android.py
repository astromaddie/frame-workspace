"""An APK as its own Lepton instance (ui/frame_android.py): the shortcut goes in
through the fake Steam client's DevTools port, the launch through `steam`,
Lepton's launcher and podman."""
import unittest

import harness
from harness import HOME, exists, ok, state, upload, wait_for
from apk_fixtures import apk, manifest, resources

harness.require()

PKG = 'com.example.fakeframe'
APP_DIR = f'{HOME}/Applications/Android/{PKG}'


class AndroidApps(harness.FrameTestCase):
    def build_apk(self, min_sdk=26):
        arsc = resources({(1, '', 0): {0: 1, 1: 2}, (2, '', 640): {0: 4}})
        data = apk({'AndroidManifest.xml': manifest(PKG, 0x7f010000, 0x7f010001, min_sdk),
                    'resources.arsc': arsc, 'res/icon_hi.png': b'\x89PNG fake icon',
                    'lib/arm64-v8a/libgame.so': b''})
        path = self.path('fake-app.apk')
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_install_launch_stop_remove(self):
        status, out, _ = upload(self.build_apk(), 'apk')
        self.assertEqual(status, 200, out)
        meta = out['app']
        self.assertEqual((meta['package'], meta['label'], meta['version']), (PKG, 'App label', '2.1'))
        shortcut = next(s for s in state()['steam']['shortcuts'] if s['appid'] == meta['shortcut'])
        self.assertEqual(shortcut['name'], 'App label')
        self.assertEqual(shortcut['exe'], f'{APP_DIR}/launch.sh')
        self.assertEqual(shortcut['start_dir'], APP_DIR)
        self.assertEqual(shortcut['icon'], f'{APP_DIR}/artwork/icon.png')
        for f in ('app.apk', 'launch.sh', 'instance.id', 'meta.json', 'artwork/icon.png', 'lepton-show-flatscreen'):
            self.assertTrue(exists(f'{APP_DIR}/{f}'), f)
        self.assertEqual(meta['game_id'], (meta['shortcut'] << 32) | 0x02000000)

        ok('POST', '/api/android', {'action': 'launch', 'package': PKG})
        ctr = f"lepton-steamlaunch-{meta['instance']}"
        running = wait_for(lambda: state()['lepton'].get(ctr), 20, 'the Lepton instance')
        self.assertTrue(running['flatscreen'])
        self.assertGreaterEqual(running['port'], 5556)
        call = next(c for c in harness.calls('lepton') if 'env' in c)
        self.assertEqual(call['env']['SteamAppId'], str(meta['instance']))
        self.assertTrue(call['env']['STEAM_COMPAT_DATA_PATH'].startswith(f'{HOME}/.local/share/Steam/'))
        apps = ok('GET', '/api/android')['apps']
        self.assertEqual([(a['package'], a['running']) for a in apps], [(PKG, True)])

        ok('POST', '/api/android', {'action': 'stop', 'package': PKG})
        wait_for(lambda: ctr not in state()['lepton'], 15, 'the instance to stop')
        ok('POST', '/api/android', {'action': 'remove', 'package': PKG})
        self.assertEqual(state()['steam']['shortcuts'], [])
        self.assertFalse(exists(APP_DIR))
        self.assertFalse(exists(f"{HOME}/.local/share/Steam/steamapps/compatdata/{meta['instance']}"))

    def test_reinstall_reuses_the_shortcut(self):
        first = upload(self.build_apk(), 'apk')[1]['app']
        second = upload(self.build_apk(), 'apk')[1]['app']
        self.assertEqual(first['shortcut'], second['shortcut'])
        self.assertEqual(len(state()['steam']['shortcuts']), 1)

    def test_launch_without_lepton_installed_fails_on_the_frame(self):
        meta = upload(self.build_apk(), 'apk')[1]['app']
        harness.ctl('runtime', 'lepton', 'missing')
        ok('POST', '/api/android', {'action': 'launch', 'package': PKG})
        run = wait_for(lambda: next((r for r in state()['launches'] if r.get('appid') == meta['shortcut']
                                     and r.get('exit') is not None), None), 20, 'launch.sh to exit')
        self.assertEqual(run['exit'], 1)   # launch.sh: "Lepton isn't installed (Steam app 3056000)"
        self.assertEqual(state()['lepton'], {})


if __name__ == '__main__':
    unittest.main()
