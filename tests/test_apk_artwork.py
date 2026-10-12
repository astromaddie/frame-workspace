import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import _images, search, SourceError
import server

PNG = (Path(__file__).parent / 'fixtures/apk-search/artwork/brush-icon.png').read_bytes()


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        with _images._lock:
            _images._urls.clear()
            _images._cache.clear()

    def test_only_registered_source_images_are_fetchable(self):
        with patch.object(_images, 'fetch') as fetch:
            with self.assertRaisesRegex(SourceError, 'Unknown artwork'):
                _images.image('https://example.com/arbitrary.png')
            fetch.assert_not_called()
        entry = {'images': {'icon': 'https://example.com/icon.png', 'banner': 'file:///tmp/private',
                            'screenshots': ['https://example.com/shot.png', 'javascript:alert(1)']}}
        art = _images.artwork(entry)
        self.assertTrue(art['icon'].startswith('/source-image/'))
        self.assertIsNone(art['banner'])
        self.assertEqual(len(art['screenshots']), 1)
        with patch.object(_images, 'fetch', return_value=(PNG, 'image/png')) as fetch:
            self.assertEqual(_images.image(art['icon'].split('/')[-1]), (PNG, 'image/png'))
            _images.image(art['icon'].split('/')[-1])
            fetch.assert_called_once_with('https://example.com/icon.png')

    def test_rejects_credentials_ports_and_non_http(self):
        for url in ['file:///tmp/a.png', 'data:image/png;base64,AAAA', 'http://user:pass@example.com/a.png',
                    'http://example.com:22/a.png', 'https://example.com:bad/a.png', '//example.com/a.png']:
            self.assertIsNone(_images.register(url), url)

    def test_blocks_private_loopback_and_mixed_dns_answers(self):
        for ip in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '192.168.1.1']:
            with patch.object(socket, 'getaddrinfo', return_value=[(2,1,6,'',(ip,443))]), \
                 patch.object(socket, 'create_connection') as connect:
                with self.assertRaisesRegex(SourceError, 'Private network'):
                    _images.fetch('https://example.com/private.png')
                connect.assert_not_called()

    def test_redirect_to_private_network_is_rejected(self):
        response = Mock(status=302)
        response.getheader.return_value = 'http://127.0.0.1/secret'
        conn = Mock()
        conn.getresponse.return_value = response
        public = [(2,1,6,'',('93.184.216.34',80))]
        private = [(2,1,6,'',('127.0.0.1',80))]
        with patch.object(socket, 'getaddrinfo', side_effect=[public,private]), \
             patch.object(socket, 'create_connection') as connect, \
             patch.object(http.client, 'HTTPConnection', return_value=conn):
            with self.assertRaisesRegex(SourceError, 'Private network'):
                _images.fetch('http://example.com/a.png')
            connect.assert_called_once_with(('93.184.216.34',80),timeout=10)

    def test_non_images_and_oversized_images_are_rejected(self):
        with self.assertRaises(SourceError):
            _images.remember('https://example.com/a.svg', b'<svg onload="evil()"/>')
        with self.assertRaisesRegex(SourceError, 'too large'):
            _images.remember('https://example.com/a.png', PNG[:8] + b'x' * _images.MAX_IMAGE)

    def test_handles_are_bounded(self):
        for i in range(4100):
            _images.register('https://example.com/%d.png' % i)
        self.assertEqual(len(_images._urls),4096)

    def test_plain_language_verdict_is_evidence_based(self):
        self.assertEqual(search.verdict({})['label'], 'Not yet checked on the Frame')
        self.assertEqual(search.verdict({'min_sdk':24,'abis':[]})['label'], 'Ready to try on the Frame')
        self.assertEqual(search.verdict({'min_sdk':24,'abis':[],'frame_tested':True})['label'], 'Works on the Frame')
        self.assertIn('newer Android', search.verdict({'min_sdk':31})['label'])
        self.assertIn('Meta Quest services', search.verdict({'requires_meta_services':True})['label'])
        self.assertEqual(search.verdict({'engine':'VrApi'})['tone'],'blocked')
        self.assertNotEqual(search.verdict({'frame_tested':True,'min_sdk':31})['tone'],'works')

    def test_image_endpoint_does_not_allow_arbitrary_urls(self):
        httpd = server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        try:
            path = _images.register('https://example.com/app.png')
            _images.remember('https://example.com/app.png',PNG)
            for url, expected in [(path,200),('/source-image/unknown',404)]:
                c=http.client.HTTPConnection('127.0.0.1',httpd.server_port)
                c.request('GET',url)
                r=c.getresponse();data=r.read();c.close()
                self.assertEqual(r.status,expected)
                if expected==200:
                    self.assertEqual(data,PNG)
                    self.assertEqual(r.getheader('Content-Type'),'image/png')
        finally:
            httpd.shutdown();httpd.server_close()
