"""Opt-in browser-backed For You feed. Playwright is never imported in guest mode."""
import contextlib
import fcntl
import json
import os
import re
import shutil
import threading
from urllib.parse import urljoin, urlsplit

PROFILE = os.path.expanduser(os.path.join(os.environ.get('XDG_CONFIG_HOME') or '~/.config', 'doomscroll', 'tiktok'))
FOR_YOU = 'https://www.tiktok.com/foryou'

@contextlib.contextmanager
def profile_lock(profile):
    os.makedirs(profile, mode=0o700, exist_ok=True)
    os.chmod(profile, 0o700)
    # Keep the lock outside the profile so logout cannot unlink an active lock.
    with open(profile + '.lock', 'a') as lock:
        os.chmod(profile + '.lock', 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('TikTok browser profile is in use. Close login/personal viewers first.') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

def playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise RuntimeError('Personal mode needs Playwright. See README optional login setup.') from None

def logout(profile=PROFILE):
    with profile_lock(profile):
        shutil.rmtree(profile)

def login():
    with profile_lock(PROFILE), playwright()() as pw:
        context = pw.chromium.launch_persistent_context(PROFILE, headless=False)
        from playwright.sync_api import Error
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto('https://www.tiktok.com/login', wait_until='domcontentloaded')
            print('Log in directly in TikTok. Close the browser window when finished.')
            while context.pages:
                try:
                    context.pages[0].wait_for_timeout(500)
                except Error:
                    if context.pages:
                        raise
                    break
        finally:
            context.close()

def video_items(links):
    items = []
    seen = set()
    for link in links:
        parsed = urlsplit(urljoin(FOR_YOU, link))
        if parsed.scheme != 'https' or parsed.netloc != 'www.tiktok.com':
            continue
        match = re.fullmatch(r'/@([\w.-]+)/video/(\d+)/?', parsed.path)
        if not match or match[2] in seen:
            continue
        seen.add(match[2])
        items.append({'id': match[2], 'author': match[1],
                      'url': f'https://www.tiktok.com/@{match[1]}/video/{match[2]}',
                      'desc': '', 'duration': None})
    return items

class PersonalFeed:
    def __init__(self, cache, on_change):
        self.on_change = on_change
        self.lock = threading.RLock()
        self.items = []
        self.known = set()
        self.creators = ['personal']
        self.initial = []
        self.loading = set()
        self.failed = {}
        self.seen = []
        self.seen_path = os.path.join(cache, 'personal-seen.json')
        self.target = 0
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.thread = None

    def start(self, mix=True):
        self.loading.add('personal')
        self.target = 8
        self.wake.set()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def ensure(self, count, need=None):
        with self.lock:
            if not self.failed and len(self.items) < count:
                self.target = max(self.target, count)
                self.wake.set()

    def add_links(self, links):
        with self.lock:
            for item in video_items(links):
                if item['id'] not in self.known:
                    self.known.add(item['id'])
                    self.items.append(item)

    def get(self, index):
        with self.lock:
            return self.items[index] if 0 <= index < len(self.items) else None

    def drop(self, index):
        with self.lock:
            if 0 <= index < len(self.items):
                self.items.pop(index)

    def mark_seen(self, item_id):
        # Separate from guest history. No browser credentials or page contents.
        with self.lock:
            if item_id not in self.seen:
                self.seen.append(item_id)
                self.seen = self.seen[-3000:]
                with open(self.seen_path, 'w') as file:
                    json.dump(self.seen, file)

    def is_loading(self):
        return bool(self.loading)

    def set_extras(self, extras):
        raise ValueError('Creator selection is available in guest mode only.')

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        try:
            if not os.path.isdir(PROFILE):
                raise RuntimeError('No TikTok session. Run npm run login first.')
            with profile_lock(PROFILE), playwright()() as pw:
                context = pw.chromium.launch_persistent_context(PROFILE, headless=False)
                try:
                    # Stop media playback in the discovery browser, not the viewer.
                    context.add_init_script("HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };")
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(FOR_YOU, wait_until='domcontentloaded', timeout=60000)
                    cookies = context.cookies('https://www.tiktok.com')
                    if not any(c['name'] in ('sessionid', 'sessionid_ss') and c['value'] for c in cookies):
                        raise RuntimeError('TikTok session expired. Run npm run login again.')
                    while not self.closed.is_set():
                        self.wake.wait(0.5)
                        if not self.wake.is_set():
                            continue
                        self.wake.clear()
                        self.loading.add('personal')
                        self.on_change()
                        stalled = 0
                        while len(self.items) < self.target and not self.closed.is_set():
                            page.wait_for_timeout(1500)
                            before = len(self.items)
                            links = page.locator('a[href*="/video/"]:visible').evaluate_all('(links) => links.map(a => a.href)')
                            self.add_links(links)
                            stalled = stalled + 1 if len(self.items) == before else 0
                            self.on_change()
                            if stalled >= 12:
                                raise RuntimeError('For You could not load more videos. Check the browser for CAPTCHA or log in again.')
                            page.mouse.wheel(0, 800)
                        self.loading.discard('personal')
                        self.on_change()
                finally:
                    context.close()
        except Exception as error:
            # Do not expose Playwright diagnostics, cookies, or authenticated URLs.
            message = str(error) if isinstance(error, RuntimeError) else 'TikTok browser failed. Check Playwright setup, close other personal viewers, then log in again.'
            self.failed['personal'] = message
            self.loading.discard('personal')
            if not self.closed.is_set():
                self.on_change()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['login', 'logout'])
    args = parser.parse_args()
    try:
        if args.action == 'login':
            login()
        else:
            logout()
            print('Local TikTok browser session removed (guest remains available).')
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else 'TikTok browser failed. Check optional Playwright setup and try again.')
        raise SystemExit(1)
