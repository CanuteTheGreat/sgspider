"""Regression tests for the speed-up work (Oct 2026): album skip index, cookie
handoff from the browser to the HTTP downloader, bounded parallel downloads,
429/5xx backoff with Retry-After, atomic (no partial) writes, and the
condition-based feed loading.

Everything runs against a local fixture server (real headless Chromium for the
pages, real HTTP for the images); nothing talks to the real site.
Run with:  python -m unittest discover -s tests -v
"""

import collections
import configparser
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

import sgspider  # noqa: E402


def make_jpeg(seed: int, size: int = 24000) -> bytes:
    body = bytes((seed * 31 + i * 7) % 251 for i in range(size - 4))
    return b"\xff\xd8\xff\xe0" + body


PLACEHOLDER = make_jpeg(999, 15000)
IMAGES = {f"img{n}.jpg": make_jpeg(n) for n in range(1, 13)}
SESSION_COOKIE = "sgsess=ok"


class Fixture:
    """Mutable server-side state shared by the handler and the tests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits = collections.Counter()
        self.inflight = 0
        self.max_inflight = 0
        self.image_delay = 0.0
        self.fail_plan = {}  # name -> list of (status, headers) to serve before success


FX = Fixture()


def album_html(names, base):
    items = "".join(
        f'<li class="photo-container"><a href="{base}/cloudfront/photos/{n}?Signature=abc">'
        f'<img src="{base}/thumb.jpg"></a></li>'
        for n in names
    )
    return f"<!doctype html><html><body><ul>{items}</ul></body></html>"


FEED_HTML = """<!doctype html><html><body>
<div id="albums">
  <a href="/girls/alpha/album/1/first/">a</a>
  <a href="/girls/beta/album/2/second/">b</a>
</div>
<button id="load-more">Load more</button>
<script>
  var batches = [
    ['/girls/gamma/album/3/third/', '/girls/delta/album/4/fourth/'],
    ['/girls/eps/album/5/fifth/']
  ];
  document.getElementById('load-more').addEventListener('click', function () {
    var batch = batches.shift();
    setTimeout(function () {   // content arrives asynchronously, like the real feed
      (batch || []).forEach(function (h) {
        var a = document.createElement('a'); a.href = h; a.textContent = h;
        document.getElementById('albums').appendChild(a);
      });
      if (!batches.length) document.getElementById('load-more').remove();
    }, 400);
  });
</script></body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, body=b"", ctype="text/html", headers=None, length=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        with FX.lock:
            FX.hits[path] += 1
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if path.startswith("/photos/sg/recent/"):
            return self._send(200, FEED_HTML.encode())
        if path.startswith("/girls/"):
            names = sorted(IMAGES)[:6] if "/album/1/" in path else sorted(IMAGES)[6:]
            return self._send(200, album_html(names, base).encode())
        if path.startswith("/cloudfront/photos/"):
            return self._image(path.rsplit("/", 1)[-1])
        return self._send(404, b"nope")

    def _image(self, name):
        with FX.lock:
            FX.inflight += 1
            FX.max_inflight = max(FX.max_inflight, FX.inflight)
            plan = FX.fail_plan.get(name)
            step = plan.pop(0) if plan else None
        try:
            if FX.image_delay:
                time.sleep(FX.image_delay)
            if step is not None:
                status, headers = step
                if status == "truncate":
                    body = IMAGES[name][:5000]
                    self.close_connection = True  # drop the socket mid-body
                    return self._send(200, body, "image/jpeg", length=len(IMAGES[name]))
                return self._send(status, b"busy", headers=headers)
            if name not in IMAGES:
                return self._send(404, b"missing")
            if SESSION_COOKIE not in (self.headers.get("Cookie") or ""):
                return self._send(200, PLACEHOLDER, "image/jpeg")  # what the CDN serves logged-out
            return self._send(200, IMAGES[name], "image/jpeg")
        finally:
            with FX.lock:
                FX.inflight -= 1

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError)):
            super().handle_error(request, client_address)  # clients hanging up is expected


class SpeedupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.pw = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()
        cls.server.shutdown()

    def setUp(self):
        FX.hits.clear()
        FX.max_inflight = 0
        FX.image_delay = 0.0
        FX.fail_plan = {}
        self.tmp = Path(tempfile.mkdtemp(prefix="sgspider-test-"))
        s = sgspider.SGSpider()
        s.base_url = self.base
        s.download_dir = self.tmp / "dl"
        s.album_index_file = self.tmp / "albums.json"
        s.state_file = self.tmp / "state.json"
        s.max_retries = 3
        s.retry_base_delay = 0.05
        s.feed_wait_timeout = 2000
        s.album_wait_timeout = 3000
        creds = configparser.ConfigParser(interpolation=None)
        creds.read_dict({"main": {"username": "tester", "password": "pw%x"}})
        s.credentials = creds
        s.start_browser(self.pw)
        self.spider = s

    def tearDown(self):
        self.spider.stop_browser()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def log_in_browser(self):
        """Simulate a logged-in browser: the session cookie lives in the Playwright context."""
        name, value = SESSION_COOKIE.split("=")
        self.spider.context.add_cookies([{"name": name, "value": value, "url": self.base + "/"}])
        self.spider.sync_cookies()

    def album_url(self, n=1):
        return f"{self.base}/girls/alpha/album/{n}/first/" if n == 1 else f"{self.base}/girls/beta/album/2/second/"

    def files_in(self, d):
        return sorted(p.name for p in Path(d).iterdir()) if Path(d).exists() else []

    # -- cookie handoff
    def test_downloader_reuses_browser_cookies(self):
        self.log_in_browser()
        target = self.tmp / "one" / "img1.jpg"
        ok, placeholder = self.spider.download_image(f"{self.base}/cloudfront/photos/img1.jpg", target)
        self.assertEqual((ok, placeholder), (True, False))
        self.assertEqual(target.read_bytes(), IMAGES["img1.jpg"])

    def test_missing_cookie_is_detected_as_placeholder_and_not_saved(self):
        self.assertTrue(self.spider.capture_placeholder_hash(f"{self.base}/cloudfront/photos/img1.jpg"))
        self.spider.sync_cookies()  # browser has no session cookie
        target = self.tmp / "one" / "img2.jpg"
        ok, placeholder = self.spider.download_image(f"{self.base}/cloudfront/photos/img2.jpg", target)
        self.assertEqual((ok, placeholder), (False, True))
        self.assertEqual(self.files_in(target.parent), [])

    def test_cookies_refreshed_after_relogin(self):
        self.spider.sync_cookies()
        self.spider.http_session()  # thread session created with the logged-out cookies
        self.log_in_browser()  # e.g. a re-login set a new session cookie
        sess = self.spider.http_session()
        self.assertIn("sgsess", sess.cookies.get_dict())

    # -- backoff / errors
    def test_429_honours_retry_after_then_succeeds(self):
        self.log_in_browser()
        FX.fail_plan["img3.jpg"] = [(429, {"Retry-After": "1"})]
        t = time.time()
        ok, _ = self.spider.download_image(f"{self.base}/cloudfront/photos/img3.jpg", self.tmp / "a" / "img3.jpg")
        self.assertTrue(ok)
        self.assertGreaterEqual(time.time() - t, 0.95, "Retry-After: 1 must be waited out")
        self.assertEqual(FX.hits["/cloudfront/photos/img3.jpg"], 2)

    def test_5xx_retried_with_backoff(self):
        self.log_in_browser()
        FX.fail_plan["img4.jpg"] = [(503, {}), (502, {})]
        ok, _ = self.spider.download_image(f"{self.base}/cloudfront/photos/img4.jpg", self.tmp / "a" / "img4.jpg")
        self.assertTrue(ok)
        self.assertEqual(FX.hits["/cloudfront/photos/img4.jpg"], 3)

    def test_404_not_retried(self):
        self.log_in_browser()
        ok, _ = self.spider.download_image(f"{self.base}/cloudfront/photos/nothere.jpg", self.tmp / "a" / "x.jpg")
        self.assertFalse(ok)
        self.assertEqual(FX.hits["/cloudfront/photos/nothere.jpg"], 1)

    def test_truncated_body_leaves_no_partial_file(self):
        self.log_in_browser()
        self.spider.max_retries = 1
        FX.fail_plan["img5.jpg"] = [("truncate", {})]
        target = self.tmp / "a" / "img5.jpg"
        ok, _ = self.spider.download_image(f"{self.base}/cloudfront/photos/img5.jpg", target)
        self.assertFalse(ok)
        self.assertEqual(self.files_in(target.parent), [])  # neither img5.jpg nor a part file

    def test_parse_retry_after(self):
        self.assertEqual(sgspider.parse_retry_after("7"), 7.0)
        self.assertIsNone(sgspider.parse_retry_after(None))
        self.assertIsNone(sgspider.parse_retry_after("soon"))
        now = time.time()
        self.assertAlmostEqual(sgspider.parse_retry_after(formatdate(now + 10, usegmt=True), now=now), 10.0, delta=1)

    # -- album processing, concurrency, skip index
    def test_process_album_parallel_then_skips_without_opening(self):
        self.log_in_browser()
        self.spider.download_concurrency = 4
        FX.image_delay = 0.2
        url = self.album_url(1)
        count, auth_failure, opened = self.spider.process_album(url)
        self.assertEqual((count, auth_failure, opened), (6, False, True))
        album_dir = self.spider.download_dir / "alpha" / "first"
        self.assertEqual(self.files_in(album_dir), sorted(IMAGES)[:6])
        for name in sorted(IMAGES)[:6]:
            self.assertEqual((album_dir / name).read_bytes(), IMAGES[name])
        self.assertGreater(FX.max_inflight, 1, "downloads should overlap")
        self.assertLessEqual(FX.max_inflight, 4, "concurrency must stay bounded")

        page_hits = FX.hits["/girls/alpha/album/1/first/"]
        image_hits = sum(v for k, v in FX.hits.items() if k.startswith("/cloudfront/"))
        count, auth_failure, opened = self.spider.process_album(url)
        self.assertEqual((count, opened), (0, False))
        self.assertEqual(FX.hits["/girls/alpha/album/1/first/"], page_hits, "completed album must not be reopened")
        self.assertEqual(sum(v for k, v in FX.hits.items() if k.startswith("/cloudfront/")), image_hits)

    def test_skip_index_rechecks_disk(self):
        self.log_in_browser()
        url = self.album_url(1)
        self.spider.process_album(url)
        album_dir = self.spider.download_dir / "alpha" / "first"
        (album_dir / "img2.jpg").unlink()  # someone removed a file
        count, _, opened = self.spider.process_album(url)
        self.assertEqual((count, opened), (1, True))
        self.assertEqual((album_dir / "img2.jpg").read_bytes(), IMAGES["img2.jpg"])

    def test_existing_files_skipped_by_listing_and_bad_ones_replaced(self):
        self.log_in_browser()
        self.spider.skip_completed_albums = False
        self.spider.placeholder_hash = sgspider.hashlib.sha256(PLACEHOLDER).hexdigest()
        self.spider.placeholder_size = len(PLACEHOLDER)  # known already: no capture request
        album_dir = self.spider.download_dir / "alpha" / "first"
        album_dir.mkdir(parents=True)
        (album_dir / "img1.jpg").write_bytes(IMAGES["img1.jpg"])   # good: kept
        (album_dir / "img2.jpg").write_bytes(b"\xff\xd8 tiny")      # broken download: replaced
        (album_dir / f"{sgspider.PART_PREFIX}img3.jpg").write_bytes(b"partial")  # leftover: removed
        count, _, _ = self.spider.process_album(self.album_url(1))
        self.assertEqual(count, 5)
        self.assertEqual(FX.hits["/cloudfront/photos/img1.jpg"], 0)
        self.assertEqual((album_dir / "img2.jpg").read_bytes(), IMAGES["img2.jpg"])
        self.assertEqual(self.files_in(album_dir), sorted(IMAGES)[:6])

    def test_placeholder_responses_flag_auth_failure_and_album_not_indexed(self):
        self.spider.sync_cookies()  # not logged in -> CDN hands out placeholders
        count, auth_failure, _ = self.spider.process_album(self.album_url(1))
        self.assertEqual((count, auth_failure), (0, True))
        self.assertEqual(self.spider.album_index, {})
        self.assertEqual(self.files_in(self.spider.download_dir / "alpha" / "first"), [])

    def test_failed_album_not_marked_complete(self):
        self.log_in_browser()
        self.spider.max_retries = 1
        FX.fail_plan["img2.jpg"] = [(503, {})]
        self.spider.process_album(self.album_url(1))
        self.assertEqual(self.spider.album_index, {})

    # -- feed
    def test_feed_collects_albums_via_condition_waits(self):
        albums = self.spider.collect_album_urls()
        self.assertEqual(len(albums), 5, albums)
        self.assertTrue(all("/album/" in a for a in albums))

    def test_album_delay_defaults_to_zero(self):
        self.assertEqual((self.spider.album_delay_min, self.spider.album_delay_max), (0.0, 0.0))
        t = time.time()
        self.spider.album_delay()
        self.assertLess(time.time() - t, 0.05)


if __name__ == "__main__":
    unittest.main()
