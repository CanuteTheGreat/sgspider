"""Regression tests for the age-verification modal that broke login (Oct 2026).

The site started showing a fancybox "adult gate" modal on the home page. Its
full-page overlay intercepts pointer events, so clicking the Login button timed
out and every run ended with "Could not find login button".

These tests drive a real headless Chromium against a local fixture page that
reproduces the site's markup (gate + overlay + login widget), so no requests go
to the real site. Run with:  python -m unittest discover -s tests -v
"""

import configparser
import http.server
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout  # noqa: E402

import sgspider  # noqa: E402

GATE_HTML = """
<div class="fancybox-overlay" style="position:fixed;inset:0;z-index:8010;background:rgba(0,0,0,.6)"></div>
<div class="fancybox-wrap adult-gate fancybox-opened" role="dialog" aria-modal="true"
     style="position:absolute;top:224px;left:505px;width:430px;z-index:8030;background:#222">
  <div id="adult-gate" class="adult-gate-content">
    <h2 id="adult-gate-title">This is an adult website.</h2>
    <button type="button" class="button call-to-action" data-adult-confirm="" data-modal-close="">Enter - I'm 18+</button>
    <a id="adult-gate-exit" href="#">Exit</a>
  </div>
</div>
<script>
  document.querySelector('[data-adult-confirm]').addEventListener('click', function () {
    document.cookie = 'confirmed-adult=1; path=/';
    document.querySelector('.fancybox-overlay').remove();
    document.querySelector('.adult-gate').remove();
  });
</script>
"""

PAGE_TEMPLATE = """<!doctype html><html><head><title>fixture</title></head><body>
<div id="login" style="position:absolute;top:216px;left:112px">
  <a class="button login" style="display:inline-block;width:99px;height:40px">Login</a>
  <div id="login-wrapper" style="display:none">
    <form id="login-form" onsubmit="return false">
      <input name="username"><input name="password" type="password">
    </form>
  </div>
</div>
<div style="height:3000px"></div>
<script>
  document.querySelector('#login a.login').addEventListener('click', function () {
    document.querySelector('#login-wrapper').style.display = 'block';
  });
  document.querySelector('input[name=password]').addEventListener('keydown', function (e) {
    // Only reachable once the form was opened and both fields were typed into.
    // (No literal username/"log out" text in the page: is_logged_in() greps content.)
    if (e.key === 'Enter' && document.querySelector('input[name=username]').value.length === 6
        && this.value.length === 4) {
      document.querySelector('#login').remove();
      var a = document.createElement('a');
      a.id = 'signed-in'; a.href = '/log' + 'out'; a.textContent = 'Sign out';
      document.body.prepend(a);
    }
  });
</script>
__GATE__
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        show_gate = "confirmed-adult=1" not in (self.headers.get("Cookie") or "")
        body = PAGE_TEMPLATE.replace("__GATE__", GATE_HTML if show_gate else "").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class AgeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.pw = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()
        cls.server.shutdown()

    def setUp(self):
        self.spider = sgspider.SGSpider()
        self.spider.base_url = self.base_url
        self.spider.max_retries = 1
        self.spider.random_delay = lambda *a, **k: None  # keep tests fast
        creds = configparser.ConfigParser(interpolation=None)
        creds.read_dict({"main": {"username": "tester", "password": "pw%x"}})
        self.spider.credentials = creds
        self.spider.start_browser(self.pw)

    def tearDown(self):
        self.spider.stop_browser()

    def test_fixture_reproduces_blocked_click(self):
        """Without dismissing the gate, the overlay intercepts the login click."""
        page = self.spider.page
        page.goto(self.base_url)
        with self.assertRaises(PlaywrightTimeout):
            page.locator("#login").first.click(timeout=1500)

    def test_dismiss_age_gate_unblocks_login_button(self):
        page = self.spider.page
        page.goto(self.base_url)
        self.assertTrue(self.spider.dismiss_age_gate())
        self.assertEqual(page.locator(".fancybox-overlay").count(), 0)
        page.locator("#login a.login").click(timeout=3000)
        page.wait_for_selector("input[name='username']", state="visible", timeout=3000)

    def test_dismiss_age_gate_is_noop_without_gate(self):
        page = self.spider.page
        page.goto(self.base_url)
        self.spider.dismiss_age_gate()
        page.reload()  # cookie now set -> server omits the gate
        self.assertFalse(self.spider.dismiss_age_gate())

    def test_full_login_flow_with_gate(self):
        """login() gets past the gate, opens the form, submits and verifies."""
        self.assertTrue(self.spider.login())
        self.assertTrue(self.spider.page.locator("#signed-in").is_visible())


if __name__ == "__main__":
    unittest.main()
