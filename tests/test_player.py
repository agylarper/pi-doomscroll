"""Unit tests for bin/doomscrolld.py: the cache, the feed mix, colours.

Run with `python3 -m unittest discover -s tests -p 'test_*.py'`.
"""
import importlib.util
import os
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("doomscrolld", os.path.join(HERE, "..", "bin", "doomscrolld.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def item(handle, n):
    return {"id": f"{handle}-{n}", "author": handle, "url": "", "desc": "", "duration": 10}


class TrimTest(unittest.TestCase):
    def test_never_removes_the_playing_or_wanted_or_fresh_file(self):
        folder = tempfile.mkdtemp()
        downloads = d.Downloader(folder, lambda *_: None)
        old = time.time() - 3600 * 24 * 365  # yt-dlp used to stamp upload dates
        for n in range(d.KEEP_VIDEOS + 10):
            path = os.path.join(folder, f"v{n}.mp4")
            open(path, "w").close()
            os.utime(path, (old + n, old + n))
        playing = os.path.join(folder, "v0.mp4")  # the oldest on disk
        wanted = {"id": "v1"}
        fresh = os.path.join(folder, "v2.mp4")
        os.utime(fresh, None)  # just downloaded
        downloads.in_use = {playing}
        downloads.wanted = [wanted]
        downloads._trim()
        self.assertTrue(os.path.exists(playing))
        self.assertTrue(os.path.exists(downloads.path(wanted)))
        self.assertTrue(os.path.exists(fresh))
        left = [n for n in os.listdir(folder) if n.endswith(".mp4")]
        self.assertLessEqual(len(left), d.KEEP_VIDEOS + 3)


class PlaybackEndTest(unittest.TestCase):
    def test_video_end_stops_on_current_item_until_user_swipes(self):
        from unittest.mock import Mock
        daemon = object.__new__(d.Daemon)
        daemon.lock = __import__('threading').RLock()
        daemon.player = Mock(epoch=7, pos=3.0)
        daemon.index = 2
        daemon._goto = Mock()
        daemon.publish = Mock()
        daemon._ended(7, True)
        daemon._goto.assert_not_called()
        daemon.player.stop.assert_called_once_with()
        self.assertEqual(daemon.player.pos, 0.0)
        daemon.publish.assert_called_once_with()

class DownloadFailureTest(unittest.TestCase):
    def test_reports_ytdlp_failure_instead_of_silently_discarding_it(self):
        from unittest.mock import patch
        folder = tempfile.mkdtemp()
        downloads = d.Downloader(folder, lambda *_: None)
        result = type('Result', (), {'returncode': 1, 'stderr': 'TikTok blocked this request'})()
        with patch.object(d.subprocess, 'run', return_value=result):
            self.assertFalse(downloads._download({'id': 'broken', 'url': 'https://example.com'}))
        self.assertTrue(any('TikTok blocked this request' in error for error in d.RECENT_ERRORS))

class ReadyTest(unittest.TestCase):
    def test_a_missing_file_is_not_ready_and_is_fetched_again(self):
        folder = tempfile.mkdtemp()
        downloads = d.Downloader(folder, lambda *_: None)
        attempts = []
        downloads._download = lambda item: attempts.append(item["id"]) or False  # no network
        video = {"id": "gone"}
        downloads.status["gone"] = "done"  # marked done, then trimmed away
        self.assertFalse(downloads.ready(video))
        downloads.want([video])
        deadline = time.time() + 5
        while not attempts and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(attempts, ["gone"])  # queued and fetched again

    def test_wanting_a_file_refreshes_it(self):
        folder = tempfile.mkdtemp()
        downloads = d.Downloader(folder, lambda *_: None)
        video = {"id": "kept"}
        path = downloads.path(video)
        open(path, "w").close()
        os.utime(path, (1, 1))
        downloads._probe = lambda _p: {"audio": True, "duration": 10}
        downloads.want([video])
        self.assertGreater(os.path.getmtime(path), time.time() - 60)
        self.assertTrue(downloads.ready(video))


class FeedTest(unittest.TestCase):
    def feed(self):
        feed = d.Feed(tempfile.mkdtemp(), [], lambda: None)
        feed.mixed = ["a", "b", "c", "d", "e", "f", "g"]
        feed.creators = list(feed.mixed)
        feed.fetchable = set()
        return feed

    def test_waits_for_variety_while_creators_load(self):
        feed = self.feed()
        feed.loading = set("bcdefg")
        feed._add("a", [item("a", n) for n in range(10)])
        feed.ensure(5, need=2)
        self.assertEqual(len(feed.items), 2)  # holds back rather than queue five of one creator

    def test_never_twice_in_a_row_once_loaded(self):
        feed = self.feed()
        for handle in feed.mixed:
            feed._add(handle, [item(handle, n) for n in range(5)])
        feed.ensure(20)
        authors = [i["author"] for i in feed.items]
        self.assertEqual(len(authors), 20)
        self.assertTrue(all(x != y for x, y in zip(authors, authors[1:])))

    def test_sample_covers_every_category(self):
        picked = set(d.sample_pool())
        for handles in d.POOL.values():
            self.assertTrue(picked & set(handles))


class ColourTest(unittest.TestCase):
    def test_hex_forms(self):
        self.assertEqual(d.normalise_hex("#1E1E2E"), "#1e1e2e")
        self.assertEqual(d.normalise_hex("1e1e2e"), "#1e1e2e")
        self.assertEqual(d.normalise_hex("  #101010  # comment"), "#101010")
        self.assertIsNone(d.normalise_hex("#"))
        self.assertIsNone(d.normalise_hex("red"))

    def test_override_wins(self):
        os.environ["DOOMSCROLL_BACKGROUND"] = "#123456"
        try:
            self.assertEqual(d.terminal_background(), "#123456")
        finally:
            del os.environ["DOOMSCROLL_BACKGROUND"]


if __name__ == "__main__":
    unittest.main()
