"""My Meta library, Experimental (ui/frame_meta.py), against a fake Meta (GraphQL answers and
download server built from the shapes the public tools document) and a fake Frame. Offline:
nothing here reaches Meta, a headset or the compatibility database."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import frame_android as android  # noqa: E402
import frame_meta as meta  # noqa: E402
import frame_quest as quest  # noqa: E402
import frame_telemetry  # noqa: E402
import test_quest  # noqa: E402  (its APK builder; importing a module adds no tests here)

PKG = test_quest.PKG
TOKEN = 'OCAtestToken1234567890abcdefGHIJ-_.~xyz'
OTHER_TOKEN = 'FRLAnotherToken0987654321zyxwvuts'
APK = test_quest.apk_bytes(files=test_quest.OPENXR, version_code=100, version_name='1.0')
OBB = b'obb data ' * 100
PUBLIC = {'157.240.22.35'}
REAL_SEND = meta._send  # before any test replaces it with a fake Meta


def entitlements(apk=APK, obb=OBB, platform_sdk=False):
    binary = {'id': '9001', 'package_name': PKG, 'version': '1.0', 'version_code': 100, 'size': str(len(apk)),
              'obb_binary': {'id': '9002', 'file_name': 'game-data.obb', 'size': len(obb)},
              'asset_files': {'nodes': [{'id': '9003', 'file_name': 'level1.pak', 'size': 3, 'is_required': True},
                                        {'id': '9004', 'file_name': 'dlc.pak', 'size': 3, 'is_required': False}]}}
    return {'data': {'viewer': {'user': {'active_entitlements': {'nodes': [
        {'active_state': 'ACTIVE', 'grant_reason': 'PURCHASE',
         'item': {'id': '111', 'display_name': 'Quest Game', 'platform': 'ANDROID_6DOF', 'latest_supported_binary': binary}},
        # Details come from a second query (only the binary id in the list).
        {'active_state': 'ACTIVE', 'item': {'id': '222', 'display_name': 'Another Game', 'platform': 'ANDROID_6DOF',
                                            'latest_supported_binary': {'id': '9100'}}},
        {'active_state': 'ACTIVE', 'item': {'id': '333', 'display_name': 'A Rift Game', 'platform': 'PC'}},
        {'active_state': 'ACTIVE', 'expiration_time': 1000, 'item': {'id': '444', 'display_name': 'Expired',
                                                                       'platform': 'ANDROID_6DOF'}},
        {'active_state': 'INACTIVE', 'item': {'id': '555', 'display_name': 'Refunded', 'platform': 'ANDROID_6DOF'}},
    ]}}}}}


class Body(io.BytesIO):
    pass


class FakeMeta:
    """graph.oculus.com and securecdn.oculus.com (which redirects to a CDN host), in memory."""

    def __init__(self, test, token=TOKEN, apk=APK, obb=OBB, redirect=True):
        self.token, self.redirect, self.requests = token, redirect, []
        self.files = {'9001': apk, '9002': obb, '9003': b'pak', '9004': b'dlc'}
        self.entitlements = entitlements(apk, obb)
        self.details = {'9100': {'data': {'node': {'id': '9100', 'package_name': 'org.example.another', 'version': '2',
                                                   'version_code': '7', 'size': 10, 'obb_binary': None,
                                                   'asset_files': {'edges': []}}}}}
        self.addresses = {}  # host -> addresses its (fake) DNS gives
        self.hold = None  # an Event the "who am I" answer waits for
        self.cdn_headers = {}
        self.location = 'https://scontent.oculuscdn.com/v/{id}?sig=abc'
        self.lookups = []
        for p in (patch.object(meta, '_send', side_effect=self.send),
                  patch.object(meta.socket, 'getaddrinfo', side_effect=self.getaddrinfo)):
            p.start()
            test.addCleanup(p.stop)

    def getaddrinfo(self, host, port, *args, **kwargs):
        self.lookups.append(host)
        return [(10 if ':' in a else 2, 1, 6, '', (a, port)) for a in sorted(self.addresses.get(host, PUBLIC))]

    def answer(self, status, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, dict(headers or {}), Body(raw)

    def send(self, url, data=None, headers=None, timeout=30):
        self.requests.append({'url': url, 'data': data, 'headers': dict(headers or {})})
        p = urllib.parse.urlsplit(url)
        if p.hostname == 'graph.oculus.com':
            form = dict(urllib.parse.parse_qsl(data.decode()))
            if form.get('access_token') != self.token:
                return self.answer(400, {'error': {'type': 'OAuthException', 'code': 190, 'message':
                                                   f"Invalid OAuth access token {form.get('access_token')}"}})
            doc = form['doc_id']
            if doc == meta.DOC_VIEWER:
                if self.hold:
                    self.hold.wait(5)
                return self.answer(200, {'data': {'viewer': {'user': {'id': '1234', 'alias': 'player_one'}}}})
            if doc == meta.DOC_ENTITLEMENTS:
                return self.answer(200, self.entitlements)
            if doc == meta.DOC_BINARY:
                return self.answer(200, self.details[json.loads(form['variables'])['binaryID']])
            return self.answer(200, {'errors': [{'message': 'unknown doc'}]})
        q = dict(urllib.parse.parse_qsl(p.query))
        if p.hostname == 'securecdn.oculus.com':
            if q.get('access_token') != self.token:
                return self.answer(401, b'')
            if q['id'] not in self.files:
                return self.answer(404, b'')
            if self.redirect:
                return self.answer(302, b'', {'Location': self.location.format(id=q['id'], token=self.token)})
            return self.answer(200, self.files[q['id']], {'Content-Length': str(len(self.files[q['id']]))})
        if p.hostname == 'scontent.oculuscdn.com':
            body = self.files[p.path.rsplit('/', 1)[-1]]
            headers = dict({'Content-Length': str(len(body))}, **self.cdn_headers)
            return self.answer(200, body, {k: v for k, v in headers.items() if v is not None})
        return self.answer(404, b'')

    def downloads(self):
        return [r for r in self.requests if 'oculus.com/binaries' in r['url'] or 'oculuscdn' in r['url']]


def connected(test, **kw):
    fake = FakeMeta(test, **kw)
    meta.connect(TOKEN)
    test.addCleanup(meta.disconnect)
    return fake


class Tokens(unittest.TestCase):
    def tearDown(self):
        meta.disconnect()

    def test_token_format(self):
        self.assertEqual(meta.check_token(f'  {TOKEN}\n'), TOKEN)
        self.assertEqual(meta.check_token('Bearer ' + OTHER_TOKEN), OTHER_TOKEN)
        for bad, why in (('', 'Paste'), ('hello world', 'unusual'), ('ABCDEFGHIJKLMNOPQRSTUVWX', 'OC or FRL'),
                         ('OC' + '%7C' * 10, 'URL-encoded'), ('OC|752908224809889|', 'app token'),
                         ('OC123456789012345', 'incomplete')):
            with self.assertRaisesRegex(meta.MetaError, why):
                meta.check_token(bad)

    def test_connect_checks_then_stores_owner_only(self):
        fake = FakeMeta(self)
        result = meta.connect(TOKEN)
        self.assertTrue(result['connected'])
        self.assertEqual(result['user'], {'name': 'player_one'})
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn(TOKEN, json.dumps(meta.status()))
        path = meta.account_path()
        self.assertEqual(json.loads(path.read_text())['token'], TOKEN)
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse([p for p in path.parent.iterdir() if p.name.startswith('.meta-account-')])  # no temp left
        # The token went to Meta's API host only, over HTTPS, in the POST body (not the address).
        request = fake.requests[0]
        self.assertTrue(request['url'].startswith('https://graph.oculus.com/'))
        self.assertNotIn(TOKEN, request['url'])
        self.assertIn(urllib.parse.quote(TOKEN, safe=''), request['data'].decode())
        self.assertEqual(dict(urllib.parse.parse_qsl(request['data'].decode()))['doc_id'], meta.DOC_VIEWER)

    def test_rejected_token_is_not_stored_or_echoed(self):
        FakeMeta(self, token=OTHER_TOKEN)
        with self.assertRaises(meta.TokenRejected) as e:
            meta.connect(TOKEN)
        self.assertNotIn(TOKEN, str(e.exception))
        self.assertFalse(meta.account_path().exists())
        self.assertFalse(meta.status()['connected'])

    def test_disconnect_deletes_the_file(self):
        FakeMeta(self)
        meta.connect(TOKEN)
        meta.disconnect()
        self.assertFalse(meta.account_path().exists())
        with self.assertRaisesRegex(meta.MetaError, 'Connect your Meta account first'):
            meta.library()
        meta.disconnect()  # twice is fine

    def test_disconnect_wins_over_a_connect_in_flight(self):
        fake = FakeMeta(self)
        fake.hold = threading.Event()
        errors = []

        def connect():
            try:
                meta.connect(TOKEN)
            except meta.MetaError as e:
                errors.append(e)
        thread = threading.Thread(target=connect)
        thread.start()
        for _ in range(500):
            if fake.requests:
                break
            threading.Event().wait(0.01)
        meta.disconnect()  # while Meta is still answering "who am I"
        fake.hold.set()
        thread.join(10)
        self.assertEqual(len(errors), 1)
        self.assertIn('disconnected', str(errors[0]))
        self.assertFalse(meta.account_path().exists())
        self.assertFalse(meta.status()['connected'])


class Redaction(unittest.TestCase):
    def setUp(self):
        FakeMeta(self)
        meta.connect(TOKEN)
        self.addCleanup(meta.disconnect)

    def test_every_form_of_the_token(self):
        def encoded(text, fmt):
            return ''.join(fmt % ord(c) for c in text)
        forms = [TOKEN, TOKEN.upper(), TOKEN.lower(), TOKEN.swapcase(), urllib.parse.quote(TOKEN, safe=''),
                 urllib.parse.quote_plus(TOKEN)]
        # Fully percent-encoded, of the token recased too, with either case of hex digits, and mixed.
        for text in (TOKEN, TOKEN.lower(), TOKEN.upper(), TOKEN.swapcase()):
            forms += [encoded(text, '%%%02X'), encoded(text, '%%%02x')]
        forms.append(''.join(c if i % 2 else '%%%02x' % ord(c.lower()) for i, c in enumerate(TOKEN)))
        # Percent-encoded twice (%25XX), as an error quoting an encoded URL shows it: recased too.
        once = encoded(TOKEN, '%%%02X')
        forms += [urllib.parse.quote(once, safe=''), urllib.parse.quote(urllib.parse.quote(TOKEN, safe=''), safe='')]
        for text in (TOKEN, TOKEN.lower(), TOKEN.swapcase()):
            forms += [encoded(text, '%%25%02X'), encoded(text, '%%25%02x')]
        forms.append(''.join(('%%25%02x' if i % 3 == 0 else '%%%02X' if i % 3 == 1 else '%s') % (ord(c) if i % 3 < 2 else c)
                             for i, c in enumerate(TOKEN.swapcase())))
        for form in forms:
            out = meta.redact(f'GET https://example.invalid/x?id=1&tok={form} failed')
            self.assertNotIn(form, out)
            self.assertIn(meta.REDACTED, out)
            plain = urllib.parse.unquote(urllib.parse.unquote(out)).lower()
            self.assertNotIn(TOKEN[2:12].lower(), plain)
            self.assertNotIn(TOKEN[-10:].lower(), plain)  # the whole credential, not just its start
        # Any start of it of 8+ characters (as a cut elsewhere leaves), however written.
        for n in (8, 12, 20):
            for prefix in (TOKEN[:n], TOKEN[:n].lower(), encoded(TOKEN[:n].lower(), '%%%02x'),
                           encoded(TOKEN[:n].swapcase(), '%%25%02X'), encoded(TOKEN[:n], '%%25%02x')):
                self.assertNotIn(prefix, meta.redact(f'cut: {prefix}', token=TOKEN))

    def test_token_shaped_values_that_are_not_stored(self):
        for text in (f'access_token={OTHER_TOKEN}&x=1', f'"access_token": "{OTHER_TOKEN}"',
                     f'Authorization: Bearer {OTHER_TOKEN}', f'OAuth {OTHER_TOKEN}', f'error near {OTHER_TOKEN} here',
                     f'ACCESS%5FTOKEN%3D{OTHER_TOKEN}'):
            self.assertNotIn(OTHER_TOKEN[3:15], meta.redact(text), text)
        self.assertEqual(meta.redact('com.oculus.systemactivities is missing'), 'com.oculus.systemactivities is missing')
        self.assertEqual(meta.redact(None), '')

    def test_redacted_before_truncation(self):
        for cut in range(1, len(TOKEN) + 1):
            text = 'x' * 50 + TOKEN + ' tail'
            out = meta.redact(text, limit=50 + cut)
            self.assertLessEqual(len(out), 50 + cut)
            self.assertNotIn(TOKEN[:6], out)
        self.assertEqual(meta.redact('short', limit=100), 'short')


class Hosts(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMeta(self)

    def test_only_plain_https(self):
        for url in ('http://securecdn.oculus.com/x', 'https://user:pw@securecdn.oculus.com/x',
                    'https://securecdn.oculus.com:8443/x', 'ftp://securecdn.oculus.com/x'):
            with self.assertRaisesRegex(meta.MetaError, 'plain HTTPS'):
                meta.check_url(url)
        self.assertEqual(meta.check_url('https://scontent.oculuscdn.com/v/1'), 'https://scontent.oculuscdn.com/v/1')
        self.assertEqual(self.fake.lookups, [])  # the URL check never looks anything up

    def test_public_addresses_only(self):
        for ip in ('10.0.0.5', '127.0.0.1', '169.254.169.254', '100.64.0.1', '192.0.0.8', '0.0.0.0',
                   '224.0.0.1', '::1', '::', 'fd00::1', 'fe80::1', 'ff02::1', 'fec0::1', '64:ff9b::a00:1',
                   '::ffff:10.0.0.1', '2002:a00:1::1', '2001::1', '2001:db8::1', 'not an ip'):
            self.assertFalse(meta.public_address(ip), ip)
        for ip in ('157.240.22.35', '8.8.8.8', '2a03:2880:f12f:83:face:b00c:0:25de'):
            self.assertTrue(meta.public_address(ip), ip)
        self.fake.addresses['cdn.example.com'] = {'157.240.22.35', 'ff02::1'}
        with self.assertRaisesRegex(meta.MetaError, 'private or local'):
            meta.resolve_public('cdn.example.com')
        self.assertEqual(meta.resolve_public('scontent.oculuscdn.com'), [('157.240.22.35', 443)])

    def test_connects_only_to_the_checked_address(self):
        self.fake.addresses['scontent.oculuscdn.com'] = {'157.240.22.35', '157.240.22.36'}
        tried = []

        def connect(host, address, timeout, deadline):
            tried.append((host, address, deadline))
            if address[0].endswith('35'):
                raise OSError('unreachable')
            return 'connection'
        with patch.object(meta, '_connect', side_effect=connect):
            self.assertEqual(meta._public_connection('scontent.oculuscdn.com', 60), 'connection')
        self.assertEqual([a for _, a, _ in tried], [('157.240.22.35', 443), ('157.240.22.36', 443)])
        self.assertEqual(len({d for _, _, d in tried}), 1)  # one shared deadline
        self.assertEqual(self.fake.lookups, ['scontent.oculuscdn.com'])  # resolved once
        self.fake.addresses['evil.example'] = {'10.1.1.1'}
        with patch.object(meta, '_connect') as never:
            with self.assertRaises(meta.MetaError):
                meta._public_connection('evil.example', 60)
        never.assert_not_called()

    def test_send_uses_the_checked_connection_and_closes_it(self):
        calls = []

        class Conn:
            def request(self, method, target, body=None, headers=None):
                calls.append((method, target, body, headers))

            def getresponse(self):
                r = Body(b'{}')
                r.status, r.headers = 200, {}
                return r

            def close(self):
                calls.append('closed')
        with patch.object(meta, '_public_connection', return_value=Conn()) as connect:
            status, _, body = REAL_SEND('https://graph.oculus.com/graphql?x=1', data=b'a=1')
            with body:
                self.assertEqual(body.read(), b'{}')
        self.assertEqual(status, 200)
        connect.assert_called_once_with('graph.oculus.com', meta.TIMEOUT)
        self.assertEqual(calls[0][:3], ('POST', '/graphql?x=1', b'a=1'))
        self.assertEqual(calls[-1], 'closed')
        with self.assertRaisesRegex(meta.MetaError, 'plain HTTPS'):
            REAL_SEND('http://graph.oculus.com/graphql')

    def test_redirects_off_meta_never_carry_the_token(self):
        start = f'https://securecdn.oculus.com/binaries/download/?id=1&access_token={TOKEN}'
        self.assertEqual(meta.next_hop(start, 'https://scontent.oculuscdn.com/v/1?sig=x', TOKEN),
                         'https://scontent.oculuscdn.com/v/1?sig=x')
        same = meta.next_hop(start, f'/binaries/download/?id=2&access_token={TOKEN}', TOKEN)
        self.assertIn(TOKEN, same)  # still Meta's download host
        quoted = urllib.parse.quote(TOKEN, safe='')
        lower_hex = ''.join('%%%02x' % ord(c) for c in TOKEN.lower())
        for target in (f'https://cdn.example/v/1?sig={TOKEN}', f'https://cdn.example/{TOKEN}/file',
                       f'https://cdn.example/v/1#{TOKEN}', f'https://cdn.example/v/1?sig={quoted}',
                       f'https://cdn.example/{lower_hex}', f'https://cdn.example/v?s={TOKEN.swapcase()}',
                       f'https://{TOKEN[:12].lower()}.cdn.example/v', f'https://cdn.example/v?x={TOKEN[:9]}',
                       'https://cdn.example/v?ACCESS_TOKEN=other'):
            with self.assertRaisesRegex(meta.MetaError, 'carrying your token'):
                meta.next_hop(start, target, TOKEN)
        self.assertEqual(self.fake.lookups, [])  # refused before any lookup (a token in a host name)
        with self.assertRaisesRegex(meta.MetaError, 'plain HTTPS'):
            meta.next_hop(start, 'http://evil.example/steal', TOKEN)

    def test_download_redirect_with_the_token_is_refused_before_connecting(self):
        meta.connect(TOKEN)
        self.addCleanup(meta.disconnect)
        token, generation = meta._session()
        work = meta.cache_root() / 'refuse'
        work.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__('shutil').rmtree(work, ignore_errors=True))
        self.fake.location = 'https://cdn.example/{token}/v/{id}'
        with self.assertRaisesRegex(meta.MetaError, 'carrying your token'):
            meta.fetch('9002', work / 'x.obb', meta.MAX_DATA_FILE, token, generation)
        self.assertFalse([r for r in self.fake.requests if 'cdn.example' in r['url']])
        self.assertEqual(list(work.iterdir()), [])

    def test_download_redirect_sends_no_token_to_the_cdn(self):
        meta.connect(TOKEN)
        self.addCleanup(meta.disconnect)
        token, generation = meta._session()
        dest = Path(meta.cache_root()) / 'redirect-test'
        dest.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: [p.unlink() for p in dest.iterdir()] and dest.rmdir())
        got = meta.fetch('9002', dest / 'x.obb', meta.MAX_DATA_FILE, token, generation, expected=len(OBB))
        self.assertEqual(Path(got['path']).read_bytes(), OBB)
        cdn = [r for r in self.fake.requests if 'oculuscdn' in r['url']]
        self.assertEqual(len(cdn), 1)
        self.assertNotIn(TOKEN, json.dumps(cdn[0]))
        self.assertNotIn('access_token', cdn[0]['url'].lower())
        self.assertNotIn('Authorization', cdn[0]['headers'])

    def test_graphql_redirect_not_followed(self):
        with patch.object(meta, '_send', return_value=(302, {'Location': 'https://evil.example/'}, Body(b''))):
            with self.assertRaisesRegex(meta.MetaError, 'redirected'):
                meta.graphql(TOKEN, meta.DOC_VIEWER)

    def test_network_errors_are_redacted(self):
        error = OSError(f'connection to https://graph.oculus.com/?access_token={TOKEN} reset')
        with patch.object(meta, '_send', side_effect=error):
            with self.assertRaises(meta.MetaError) as e:
                meta.graphql(TOKEN, meta.DOC_VIEWER)
        self.assertNotIn(TOKEN, str(e.exception))
        self.assertIsNone(e.exception.__cause__)
        self.assertTrue(e.exception.__suppress_context__)


class Library(unittest.TestCase):
    def test_owned_quest_apps_with_download_facts(self):
        fake = connected(self)
        apps = {a['id']: a for a in meta.library()}
        self.assertEqual(set(apps), {'111', '222'})  # no Rift app, nothing expired or inactive
        game = apps['111']
        self.assertEqual((game['title'], game['package'], game['version'], game['version_code'], game['size']),
                         ('Quest Game', PKG, '1.0', 100, len(APK)))
        self.assertEqual(game['binary_id'], '9001')
        self.assertEqual(game['obb'], {'id': '9002', 'name': 'game-data.obb', 'size': len(OBB), 'sha256': None,
                                       'required': True})
        self.assertEqual([(f['id'], f['required']) for f in game['assets']], [('9003', True), ('9004', False)])
        other = apps['222']  # filled in from the binary query
        self.assertEqual((other['package'], other['version_code'], other['size']), ('org.example.another', 7, 10))
        queries = len(fake.requests)
        meta.library()
        self.assertEqual(len(fake.requests), queries)  # kept in memory
        public = meta._public(game)
        self.assertEqual(public['data_files'], 1)  # the OBB; level1.pak isn't a standard OBB name
        self.assertEqual(public['skipped_files'], ['level1.pak'])
        self.assertNotIn(TOKEN, json.dumps(public))
        meta.disconnect()
        with self.assertRaises(meta.MetaError):
            meta.library()

    def test_binary_query_when_the_list_doesnt_say_what_data_there_is(self):
        fake = connected(self)
        binary = fake.entitlements['data']['viewer']['user']['active_entitlements']['nodes'][0]['item'][
            'latest_supported_binary']
        fake.details['9001'] = {'data': {'node': dict(binary)}}
        for key in ('obb_binary', 'asset_files'):
            binary.pop(key)
        game = next(a for a in meta.library(refresh=True) if a['id'] == '111')
        self.assertEqual(game['obb']['id'], '9002')  # from the binary query
        asked = [json.loads(dict(urllib.parse.parse_qsl(r['data'].decode()))['variables']).get('binaryID')
                 for r in fake.requests if r['data'] and meta.DOC_BINARY in r['data'].decode()]
        self.assertIn('9001', asked)
        with patch.object(meta, 'MAX_DETAILS', 0):  # beyond the cap: filled in when chosen
            game = next(a for a in meta.library(refresh=True) if a['id'] == '111')
            self.assertIsNone(game['obb'])
            self.assertEqual(meta.owned('111')['obb']['id'], '9002')

    def test_cli(self):
        FakeMeta(self)
        self.addCleanup(meta.disconnect)
        out = io.StringIO()
        with patch.object(sys, 'argv', ['frame_meta.py', 'connect', '--token-stdin']), \
                patch.object(sys, 'stdin', io.StringIO(TOKEN + '\n')), patch('sys.stdout', out), \
                patch('sys.stderr', io.StringIO()):
            meta.main()
        self.assertTrue(json.loads(out.getvalue())['connected'])
        out = io.StringIO()
        with patch.object(sys, 'argv', ['frame_meta.py', 'list']), patch('sys.stdout', out), \
                patch.object(meta, 'with_verdicts', side_effect=lambda apps: apps):
            meta.main()
        listed = json.loads(out.getvalue())
        self.assertEqual({a['id'] for a in listed}, {'111', '222'})
        self.assertNotIn(TOKEN, out.getvalue())
        with patch.object(sys, 'argv', ['frame_meta.py', 'connect', TOKEN]):
            with self.assertRaises(SystemExit) as e:
                meta.main()
        self.assertNotIn(TOKEN, str(e.exception.code))  # a token on the command line is refused, not echoed

    def test_only_owned_apps_download(self):
        fake = connected(self)
        with self.assertRaisesRegex(meta.MetaError, "isn't in your Meta library"):
            meta.owned('999')
        with self.assertRaisesRegex(meta.MetaError, 'invalid'):
            meta.owned('../1')
        with self.assertRaisesRegex(meta.MetaError, "isn't in your Meta library"):
            meta.download('333')  # a Rift app the account owns isn't a Quest app
        self.assertEqual(fake.downloads(), [])

    def test_expired_token_while_listing(self):
        fake = connected(self)
        fake.token = OTHER_TOKEN  # Meta signed it out
        with self.assertRaises(meta.TokenRejected) as e:
            meta.library(refresh=True)
        self.assertIn('get a new one', str(e.exception))


class Downloads(unittest.TestCase):
    def test_apk_and_obb_into_the_cache(self):
        fake = connected(self)
        r = meta.download('111')
        self.addCleanup(lambda: __import__('shutil').rmtree(r['folder'], ignore_errors=True))
        self.assertEqual(Path(r['apk']).read_bytes(), APK)
        self.assertEqual([Path(p).name for p in r['obb']], [f'main.100.{PKG}.obb'])
        self.assertEqual(Path(r['obb'][0]).read_bytes(), OBB)
        self.assertEqual(r['skipped'], ['level1.pak'])
        self.assertTrue(r['folder'].startswith(str(meta.cache_root())))
        ids = [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(d['url']).query)).get('id')
               for d in fake.downloads() if 'securecdn' in d['url']]
        self.assertEqual(ids, ['9001', '9002'])  # never the optional DLC, nor the non-OBB asset

    def test_declared_size_over_the_limit_refused_before_downloading(self):
        fake = connected(self)
        token, generation = meta._session()
        work = meta.cache_root() / 'limits'
        work.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__('shutil').rmtree(work, ignore_errors=True))
        before = len(fake.requests)
        with self.assertRaisesRegex(meta.MetaError, 'more than Frame Control downloads'):
            meta.fetch('9002', work / 'a.obb', 100, token, generation, expected=1000)
        self.assertEqual(len(fake.requests), before)
        fake.entitlements['data']['viewer']['user']['active_entitlements']['nodes'][0]['item'][
            'latest_supported_binary']['obb_binary']['size'] = meta.MAX_TOTAL + 1
        meta.library(refresh=True)
        with patch.object(meta.shutil, 'disk_usage', return_value=type('U', (), {'free': 10**15})()):
            with self.assertRaisesRegex(meta.MetaError, 'more than Frame Control downloads'):
                meta.download('111')
        with patch.object(meta.shutil, 'disk_usage', return_value=type('U', (), {'free': 10})()):
            with self.assertRaisesRegex(meta.MetaError, 'GB free on this computer'):
                meta.download('111')

    def test_declared_and_actual_sizes(self):
        fake = connected(self)
        token, generation = meta._session()
        work = meta.cache_root() / 'sizes'
        work.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__('shutil').rmtree(work, ignore_errors=True))
        with self.assertRaisesRegex(meta.MetaError, 'not the size'):
            meta.fetch('9002', work / 'a.obb', 50, token, generation)  # Content-Length over the limit
        fake.cdn_headers = {'Content-Length': None}  # no length given: the limit stops the stream
        with self.assertRaisesRegex(meta.MetaError, 'larger than'):
            meta.fetch('9002', work / 'a.obb', 50, token, generation)
        fake.cdn_headers = {'Content-Length': '5'}
        with self.assertRaisesRegex(meta.MetaError, 'not the size Meta said'):
            meta.fetch('9002', work / 'b.obb', 10**6, token, generation, expected=len(OBB))
        fake.cdn_headers = {'Content-Length': None}
        with self.assertRaisesRegex(meta.MetaError, 'incomplete'):
            meta.fetch('9003', work / 'c.obb', 10**6, token, generation, expected=10)
        with self.assertRaisesRegex(meta.MetaError, 'checksum'):
            meta.fetch('9003', work / 'd.obb', 10**6, token, generation, expected=3, sha256='0' * 64)
        self.assertEqual(list(work.iterdir()), [])  # nothing left behind, not even .part files

    def test_disconnect_stops_a_download(self):
        connected(self)
        token, generation = meta._session()
        work = meta.cache_root() / 'stop'
        work.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__('shutil').rmtree(work, ignore_errors=True))
        meta.disconnect()
        with self.assertRaisesRegex(meta.MetaError, 'disconnected'):
            meta.fetch('9002', work / 'a.obb', 10**6, token, generation)
        self.assertEqual(list(work.iterdir()), [])

    def test_one_budget_for_every_file_including_unknown_sizes(self):
        # Sizes Meta doesn't give count as they arrive: three files of unknown size that each fit
        # the per-file limit still can't add up past the install's budget.
        fake = connected(self)
        node = fake.entitlements['data']['viewer']['user']['active_entitlements']['nodes'][0]['item'][
            'latest_supported_binary']
        node.pop('size')
        node['obb_binary'].pop('size')
        node['obb_binary']['file_name'] = f'main.100.{PKG}.obb'
        node['asset_files']['nodes'][0].update(file_name=f'patch.100.{PKG}.obb')
        node['asset_files']['nodes'][0].pop('size')
        fake.files['9003'] = OBB
        fake.cdn_headers = {'Content-Length': None}
        meta.library(refresh=True)
        budget = len(APK) + len(OBB) + len(OBB) // 2  # the second OBB overflows half way
        with patch.object(meta, 'MAX_TOTAL', budget):
            with self.assertRaisesRegex(meta.MetaError, 'past the'):
                meta.download('111')
        self.assertEqual([p for p in meta.cache_root().iterdir() if p.name.startswith(PKG)], [])  # all removed
        with patch.object(meta, 'MAX_TOTAL', len(APK) + 2 * len(OBB)):
            r = meta.download('111')  # exactly enough
        self.addCleanup(lambda: __import__('shutil').rmtree(r['folder'], ignore_errors=True))
        self.assertEqual(len(r['obb']), 2)

    def test_not_an_apk(self):
        connected(self, apk=b'<html>sign in</html>')
        with self.assertRaisesRegex(meta.MetaError, 'not an APK'):
            meta.install('111')
        self.assertEqual(list(meta.cache_root().iterdir()) if meta.cache_root().is_dir() else [], [])


class Install(unittest.TestCase):
    """The handoff to Import from my Quest's checks, install and two-step game data."""
    META = dict(test_quest.Import.META)

    def setUp(self):
        self.order, self.running, self.installed = [], False, None

        def install(path, source=None, **kw):
            self.order.append(('install', Path(path).name, source))
            self.assertTrue(android._install_lock._is_owned())
            self.assertEqual(Path(path).read_bytes()[:2], b'PK')
            return dict(self.META)

        def add_data(pkg, checked, obb, folder, report, warnings=(), source=''):
            self.order.append(('data', [Path(p).name for p in obb], [Path(p).read_bytes() for p in obb]))
            self.running = False
            return {'message': 'Added the game data.', 'obb': [{'name': Path(p).name} for p in obb], 'warnings': list(warnings),
                    'app': dict(self.META), 'needs_game_data': False, 'closed': True, 'split': False}

        def launch(pkg):
            self.order.append(('launch',))
            self.running = True

        for target, name, effect in ((android, 'install', install), (android, 'launch', launch),
                                     (android, 'read_meta', lambda pkg: self.installed),
                                     (quest, '_running', lambda pkg: self.running),
                                     (quest, '_sleep', lambda s: None),
                                     (quest, 'add_game_data', add_data)):
            p = patch.object(target, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def cache_left(self):
        root = meta.cache_root()
        return [p for p in root.iterdir() if p.name.startswith(PKG)] if root.is_dir() else []

    def test_install_then_add_game_data_on_the_second_install(self):
        connected(self)
        result = meta.install('111')
        self.assertEqual(self.order, [('install', 'base.apk', 'Meta Horizon Store')])
        self.assertTrue(result['needs_game_data'])
        self.assertIn('choose Install again', result['message'])
        self.assertIn('level1.pak', result['message'])  # not copied, and said so
        self.assertEqual(result['source'], 'meta-store')
        self.assertEqual(self.cache_left(), [])  # nothing kept after the install
        # Second Install with the game open: the OBB, under its standard name, then closed.
        self.running, self.installed = True, dict(self.META)
        result = meta.install('111')
        self.assertEqual(self.order[-1], ('data', [f'main.100.{PKG}.obb'], [OBB]))
        self.assertEqual(self.cache_left(), [])

    def test_install_and_test_adds_the_data_itself_then_tests(self):
        connected(self)
        with patch.object(meta, 'run_test', side_effect=lambda pkg, **kw: self.order.append(('test', pkg)) or
                          {'message': 'Test: Quest Game runs.', 'outcome': 'runs'}):
            result = meta.install('111', test=True)
        self.assertEqual([o[0] for o in self.order], ['install', 'launch', 'data', 'test'])
        self.assertFalse(result['needs_game_data'])
        self.assertEqual(result['test']['outcome'], 'runs')
        self.assertIn('Test: Quest Game runs.', result['message'])

    def test_licence_check_installs_with_the_warning(self):
        apk = test_quest.apk_bytes(files=dict(test_quest.OPENXR, **{'lib/arm64-v8a/libovrplatformloader.so': b''}),
                                   version_code=100, version_name='1.0')
        connected(self, apk=apk)
        result = meta.install('111')
        self.assertEqual(self.order[0][0], 'install')
        self.assertTrue(result['platform_sdk'])
        self.assertIn("Uses Meta's licence check", result['message'])
        self.assertIn('may quit, hang, or run without online features', result['message'])

    def test_still_refused_32_bit_and_newer_android(self):
        connected(self, apk=test_quest.apk_bytes(files={'lib/armeabi-v7a/libgame.so': b''}, version_code=100))
        with self.assertRaisesRegex(quest.QuestError, '32-bit'):
            meta.install('111')
        meta.disconnect()
        connected(self)
        real = android.apk_info
        with patch.object(android, 'apk_info', side_effect=lambda p: dict(real(p), min_sdk=31)):
            with self.assertRaisesRegex(android.FrameError, 'API 31'):
                meta.install('111')
        self.assertEqual(self.order, [])
        self.assertEqual(self.cache_left(), [])

    def test_version_mismatch_and_one_at_a_time(self):
        connected(self, apk=test_quest.apk_bytes(files=test_quest.OPENXR, version_code=99))
        with self.assertRaisesRegex(meta.MetaError, 'version code 100, but the APK it sent is 99'):
            meta.install('111')
        with meta._install_lock:
            with self.assertRaisesRegex(meta.MetaError, 'Another My Meta library install'):
                meta.install('111')


LOGS = {
    'runs': 'I FrameXrCompat: created instance\nI Unity: XR_SESSION_STATE_FOCUSED\n',
    'issues': 'I Unity: XR_SESSION_STATE_FOCUSED\nE Adreno: GL_INVALID_OPERATION in glDrawArrays\n',
    'licence': 'I Unity: ovr_Entitlement_GetIsViewerEntitled\nE Unity: Entitlement check failed, quitting\n',
    'crash': ('E AndroidRuntime: FATAL EXCEPTION: main\n'
              'E AndroidRuntime: java.lang.NullPointerException: ClipboardManager.addPrimaryClipChangedListener\n'),
    'native': 'F libc: Fatal signal 11 (SIGSEGV), code 1 in tid 4242 (UnityMain)\n',
    'anr': 'E ActivityManager: ANR in com.example.questgame\n',
    'extension': 'E OpenXR: xrCreateInstance: XR_ERROR_EXTENSION_NOT_PRESENT XR_FB_passthrough\n',
    'device': 'E Unity: Failed to launch SystemActivities\n',
    'gpu-crash': 'E zink: DEVICE LOST\nF libc: Fatal signal 6 (SIGABRT) in libgallium_dri.so\n',
    'java': ('E AndroidRuntime: FATAL EXCEPTION: main\n'
             f'E AndroidRuntime: java.lang.IllegalStateException: bad session {TOKEN} for user 1234\n'),
    'licence-ok': ('I Unity: ovr_Entitlement_GetIsViewerEntitled called\n'
                   'I Unity: IsUserEntitledToApplication: true\nI Unity: entitlement check passed\n'),
}
LOGS['extension-session'] = LOGS['extension'] + 'I Unity: XR_SESSION_STATE_FOCUSED\n'
LOGS['licence-ok-session'] = LOGS['licence-ok'] + LOGS['runs']


class Classify(unittest.TestCase):
    def test_outcomes_from_logs(self):
        cases = [
            ('runs', True, 'runs', 'runs', '', ''),
            ('issues', True, 'runs-with-issues', 'runs', 'Graphics errors', ''),
            ('licence', False, 'quits', 'crashes', "Meta's licence check failed", ''),
            # Open with a failed check but no sign of its VR scene: not enough to call it stuck.
            ('licence', True, 'inconclusive', None, "isn't recorded", ''),
            ('crash', False, 'crashes', 'crashes', 'clipboard service', ''),
            ('native', False, 'crashes', 'crashes', 'native crash signal', 'SIGSEGV'),
            ('java', False, 'crashes', 'crashes', 'Java exception', 'java.lang.IllegalStateException'),
            ('anr', True, 'hangs', 'crashes', 'stopped responding', ''),
            ('extension', True, 'inconclusive', None, 'OpenXR feature', 'XR_FB_passthrough'),
            ('extension-session', True, 'runs-with-issues', 'runs', 'OpenXR feature', 'XR_FB_passthrough'),
            ('device', False, 'quits', 'crashes', 'Meta Quest', ''),
            ('gpu-crash', False, 'crashes', 'crashes', 'Graphics errors', ''),
            ('licence-ok', True, 'inconclusive', None, "can't tell whether it runs", ''),
            ('licence-ok-session', True, 'runs', 'runs', '', ''),
        ]
        for log, alive, outcome, result, cause, detail in cases:
            v = meta.classify(LOGS[log], alive)
            self.assertEqual((v['outcome'], v['result']), (outcome, result), log)
            self.assertIn(cause, v['cause'], log)
            self.assertEqual(v['detail'], detail, log)
            # Causes are fixed texts, never log text.
            self.assertTrue(v['cause'] == '' or v['cause'].startswith(tuple(meta.CAUSES.values())), log)
        self.assertNotIn('licence', meta.classify(LOGS['licence-ok-session'], True)['signals']['matched'])
        quiet = meta.classify('I Unity: loading\n', True)
        self.assertEqual((quiet['outcome'], quiet['result']), ('inconclusive', None))
        gone = meta.classify('', False, instance_up=False)
        self.assertEqual(gone['outcome'], 'quits')
        self.assertIn('stopped before its log could be read', gone['cause'])
        never = meta.classify('', False, instance_up=False, started=False)
        self.assertEqual((never['outcome'], never['result']), ('instance-failed', 'instance_failed'))
        self.assertTrue(meta.classify(LOGS['runs'], True)['signals']['xr_session'])
        self.assertEqual(meta.classify(LOGS['licence'], False)['signals']['matched'], ['licence'])


class RunTest(unittest.TestCase):
    META = dict(test_quest.Import.META)

    def setUp(self):
        self.reports, self.alive, self.log, self.running = [], True, LOGS['runs'], False
        ctr = f"lepton-steamlaunch-{self.META['instance']}"

        def ssh(cmd, input=None, timeout=120):
            if 'pidof' in cmd:
                return '4242\n' if self.alive else ''
            if 'logcat' in cmd:
                return self.log
            raise AssertionError(cmd)

        def launch(pkg):
            self.running = True
        for target, name, effect in ((android, '_meta_or_fail', lambda pkg: dict(self.META)),
                                     (android, 'launch', launch), (android, 'ssh', ssh),
                                     (android, 'running_instances', lambda strict=False: {ctr: '5555'} if self.running else {}),
                                     (quest, '_running', lambda pkg: self.running),
                                     (quest, '_close', lambda pkg: (setattr(self, 'running', False), (True, None))[1]),
                                     (quest, '_sleep', lambda s: None)):
            p = patch.object(target, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)
        import frame_catalog
        p = patch.object(frame_catalog, 'add_report', side_effect=lambda *a, **kw: self.reports.append((a, kw)) or
                         {'id': 'r1'})
        p.start()
        self.addCleanup(p.stop)

    def test_runs_and_report_carries_results_only(self):
        connected(self)
        self.log = LOGS['runs'] + f'I Game: debug url https://graph.oculus.com/?access_token={TOKEN}\n'
        v = meta.run_test(PKG, wait=20)
        self.assertEqual(v['outcome'], 'runs')
        self.assertEqual(v['signals']['seconds'], 20)
        (args, kwargs), = self.reports
        self.assertEqual(args, (PKG, '1.0'))
        self.assertEqual(kwargs['source'], 'meta-store')
        self.assertEqual(kwargs['result'], 'runs')
        self.assertEqual(kwargs['via'], 'probe')
        self.assertIn('version code 100', kwargs['notes'])
        self.assertEqual(set(kwargs), {'result', 'notes', 'via', 'label', 'source'})
        payload = json.dumps([args, kwargs])
        self.assertNotIn(TOKEN, payload)
        self.assertNotIn(str(meta.cache_root()), payload)
        self.assertNotIn('.apk', payload)
        self.assertNotIn('.obb', payload)

    def test_quit_with_licence_check_is_reported_as_it_happened(self):
        connected(self)
        self.alive, self.log = False, LOGS['licence']
        v = meta.run_test(PKG, wait=30)
        self.assertEqual((v['outcome'], v['result']), ('quits', 'crashes'))
        self.assertIn("Meta's licence check failed", v['message'])
        self.assertEqual(self.reports[0][1]['result'], 'crashes')
        self.assertIn('Meta library test: quits', self.reports[0][1]['notes'])

    def test_instance_never_starts(self):
        connected(self)
        with patch.object(android, 'launch'):  # nothing comes up
            v = meta.run_test(PKG, wait=5)
        self.assertEqual(v['outcome'], 'instance-failed')
        self.assertEqual(self.reports[0][1]['result'], 'instance_failed')

    def test_hang_is_closed_and_no_report_when_asked(self):
        connected(self)
        self.log = LOGS['anr']
        v = meta.run_test(PKG, wait=5, report_result=False)
        self.assertEqual(v['outcome'], 'hangs')
        self.assertFalse(self.running)
        self.assertEqual(self.reports, [])

    def test_crash_text_never_reaches_reports_or_messages(self):
        connected(self)
        self.alive, self.log = False, LOGS['java']
        v = meta.run_test(PKG, wait=10)
        self.assertEqual((v['outcome'], v['cause_key']), ('crashes', 'crash-java'))
        self.assertIn('(java.lang.IllegalStateException)', v['message'])
        (args, kwargs), = self.reports
        everything = json.dumps([v, args, kwargs])
        for fragment in (TOKEN, TOKEN[:8], 'bad session', 'user 1234'):
            self.assertNotIn(fragment, everything)
        self.assertNotIn('IllegalStateException', kwargs['notes'])  # reports: fixed texts only
        self.assertIn(meta.CAUSES['crash-java'].rstrip('.'), kwargs['notes'])

    def test_long_token_in_a_library_name_is_never_shown(self):
        connected(self)
        long_token = TOKEN + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2W3x4Y5z6'
        meta.disconnect()
        fake = FakeMeta(self, token=long_token)
        meta.connect(long_token)
        self.alive = False
        for line in (f'E linker: dlopen failed: library "{long_token}.so" not found',
                     f'E linker: dlopen failed: library "lib{long_token[-40:]}.so" not found',
                     'E linker: dlopen failed: library "libZq9Xw3Rt7Yu1Io5Pa8Sd2Fg6.so" not found'):
            self.log = line + '\n'
            self.reports.clear()
            v = meta.run_test(PKG, wait=5)
            self.assertEqual(v['cause_key'], 'library')
            self.assertEqual(v['detail'], '', line)
            everything = json.dumps([v, self.reports])
            for fragment in (long_token[-30:], long_token[-60:-30], long_token[:8], 'Zq9Xw3Rt7Yu1Io5Pa8Sd2Fg6'):
                self.assertNotIn(fragment, everything)
        self.log = 'E linker: dlopen failed: library "libfoo_engine.so" not found\n'
        self.assertEqual(meta.run_test(PKG, wait=5)['detail'], 'libfoo_engine.so')  # real names still shown
        self.assertEqual(fake.token, long_token)

    def test_inconclusive_is_not_reported_or_closed(self):
        connected(self)
        self.log = 'I Unity: ovr_Entitlement_GetIsViewerEntitled called\nI Unity: loading\n'
        v = meta.run_test(PKG, wait=10)
        self.assertEqual((v['outcome'], v['result']), ('inconclusive', None))
        self.assertEqual(self.reports, [])
        self.assertIn('skipped', v['report'])
        self.assertTrue(self.running)  # left open: nothing says it is stuck

    def test_must_close_a_running_game_before_testing(self):
        connected(self)
        self.running = True
        launched = []
        with patch.object(quest, '_close', return_value=(False, 'it is still running')), \
                patch.object(android, 'launch', side_effect=launched.append):
            with self.assertRaisesRegex(meta.MetaError, "couldn't close it .*wasn't tested.*Close it from Steam"):
                meta.run_test(PKG, wait=5)
        self.assertEqual(launched, [])
        self.assertEqual(self.reports, [])

    def test_failed_hang_cleanup_is_said(self):
        connected(self)
        self.log = LOGS['anr']
        with patch.object(quest, '_close', side_effect=[(False, 'Steam is not answering')]):
            v = meta.run_test(PKG, wait=5)
        self.assertEqual(v['outcome'], 'hangs')
        self.assertFalse(v['closed'])
        self.assertIn('the game may still be running; close it from Steam', v['message'])
        self.assertEqual(self.reports[0][1]['result'], 'crashes')

    def test_sharing_follows_the_privacy_setting(self):
        sent = []
        with patch.object(frame_telemetry, 'enabled', return_value=True), \
                patch.object(frame_telemetry, 'capture', side_effect=lambda e, p, level: sent.append(p) or True):
            frame_telemetry.compat_report({'id': 'x', 'package': PKG, 'result': 'runs', 'source': 'meta-store',
                                           'notes': f'access_token={TOKEN}'})
        self.assertEqual(sent[0]['source'], 'meta-store')
        with patch.object(frame_telemetry, 'enabled', return_value=False), \
                patch.object(frame_telemetry, 'capture') as capture:
            self.assertFalse(frame_telemetry.compat_report({'id': 'y', 'package': PKG, 'result': 'runs'}))
        capture.assert_not_called()


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def test_requests_and_answers_never_carry_the_token(self):
        s = self.server
        FakeMeta(self)
        self.addCleanup(meta.disconnect)
        for body in ({'action': 'connect', 'token': TOKEN}, {'action': 'connect', 'acknowledged': True},
                     {'action': 'install', 'app': 'abc'}, {'action': 'install', 'app': '111', 'test': 'yes'},
                     {'action': 'test', 'package': '../x'}, {'action': 'nope'}):
            with self.assertRaises(s.Failure) as e:
                s.meta(body)
            self.assertEqual(e.exception.status, 400, body)
        result = s.meta({'action': 'connect', 'token': TOKEN, 'acknowledged': True})
        self.assertTrue(result['connected'])
        status = s.meta_status('library=1')
        self.assertEqual({a['id'] for a in status['apps']}, {'111', '222'})
        self.assertIn('sharing', status)
        self.assertNotIn(TOKEN, json.dumps([result, status]))
        with patch.object(s, 'ensure_master'), patch.object(s, 'start_job', return_value={'job': 'j'}) as job:
            self.assertEqual(s.meta({'action': 'install', 'app': '111', 'test': True}), {'job': 'j'})
            work = job.call_args.args[1]
            with patch.object(meta, 'install', side_effect=KeyError(f'access_token={TOKEN}')):
                with self.assertRaises(meta.MetaError) as e:
                    work(lambda *a: None)
            self.assertNotIn(TOKEN, str(e.exception))
        with self.assertRaises(s.Failure) as e:
            s.meta({'action': 'connect', 'token': OTHER_TOKEN, 'acknowledged': True})
        self.assertNotIn(OTHER_TOKEN, str(e.exception))
        self.assertEqual(e.exception.status, 400)
        self.assertFalse(s.meta({'action': 'disconnect'})['connected'])


if __name__ == '__main__':
    unittest.main()
