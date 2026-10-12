import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import SourceError, sidequest


class SideQuestTests(unittest.TestCase):
    def test_policy_is_recorded_and_source_is_page_only(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/sidequest-policy.json').read_text())
        self.assertIn('/search/', fixture['robots']['disallow'])
        self.assertIn('scraping', fixture['terms']['prohibited_activities_i'])
        self.assertTrue(sidequest.sources()[0]['page_only'])

    @patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
    def test_unknown_listing_never_claims_free_or_downloadable(self, _urlopen):
        source = sidequest.sources()[0]
        entry = sidequest.details(source, '123')
        self.assertEqual(entry['page'], 'https://sidequestvr.com/app/123')
        self.assertFalse(entry['downloadable'])
        self.assertIsNone(entry['free'])
        self.assertIsNone(entry['vr'])
        self.assertEqual(entry['images']['screenshots'], [])
        with self.assertRaisesRegex(SourceError, 'page-only'):
            sidequest.download(source, '123')
        self.assertEqual(sidequest.search(source, 'open saber'), [])
        self.assertEqual(sidequest.search(source, 'open saber', 0), [])

    def test_invalid_ids(self):
        for value in ('../123', '1?paid=false', '１', '', '1/2', '1' * 13):
            with self.assertRaises(SourceError):
                sidequest.details(sidequest.sources()[0], value)
