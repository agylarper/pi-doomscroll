import os
import sys
import tempfile
import unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bin'))
import personal

class PersonalTest(unittest.TestCase):
    def test_only_canonical_tiktok_video_links_are_accepted_in_order(self):
        links = [
            'https://www.tiktok.com/@alice/video/123?lang=en',
            '/@bob/video/456',
            'https://www.tiktok.com/@alice/video/123',
            'https://evil.test/@alice/video/789',
            'https://www.tiktok.com/@alice',
            'https://www.tiktok.com/@alice/video/not-a-number',
        ]
        self.assertEqual(personal.video_items(links), [
            {'id': '123', 'author': 'alice', 'url': 'https://www.tiktok.com/@alice/video/123', 'desc': '', 'duration': None},
            {'id': '456', 'author': 'bob', 'url': 'https://www.tiktok.com/@bob/video/456', 'desc': '', 'duration': None},
        ])

    def test_personal_feed_preserves_order_and_deduplicates_batches(self):
        feed = personal.PersonalFeed(tempfile.mkdtemp(), lambda: None)
        feed.add_links(['/@b/video/2', '/@a/video/1'])
        feed.add_links(['/@b/video/2', '/@c/video/3'])
        self.assertEqual([feed.get(i)['id'] for i in range(3)], ['2', '1', '3'])
        feed.drop(0)
        feed.add_links(['/@b/video/2'])
        self.assertEqual([i['id'] for i in feed.items], ['1', '3'])

    def test_fetch_is_demand_driven_and_stops_on_failure(self):
        feed = personal.PersonalFeed(tempfile.mkdtemp(), lambda: None)
        feed.ensure(5)
        self.assertTrue(feed.wake.is_set())
        self.assertEqual(feed.target, 5)
        feed.wake.clear()
        feed.failed['personal'] = 'Login required'
        feed.ensure(10)
        self.assertFalse(feed.wake.is_set())

    def test_logout_refuses_a_busy_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            with personal.profile_lock(folder):
                with self.assertRaisesRegex(RuntimeError, 'in use'):
                    personal.logout(folder)
