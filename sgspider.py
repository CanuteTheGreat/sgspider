#!/usr/bin/env python3
"""
SGSpider - A web scraper for SuicideGirls using Playwright.

The browser is used only for what needs a browser: logging in, scrolling the
album feed and reading album pages. Images live on an external CDN, so they are
fetched with a plain HTTP client (requests) that reuses the browser session's
cookies, several at a time, and streamed straight to disk.
"""

import sys
import time
import re
import random
import gc
import fcntl
import atexit
import json
import os
import threading
import configparser
import platform
import hashlib
from contextlib import contextmanager
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Default configuration values (can be overridden in the [settings] section of sgspider.ini)
DEFAULT_HEADLESS = True
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 5
DEFAULT_MAX_BACKOFF = 60  # seconds; cap for exponential backoff and Retry-After
DEFAULT_DOWNLOAD_TIMEOUT = 30000  # 30 seconds for image downloads
DEFAULT_PAGE_LOAD_TIMEOUT = 60000  # 60 seconds for page loads
DEFAULT_MAX_ALBUM_PAGES = 0  # Limit album loading iterations (0 = unlimited)
DEFAULT_BROWSER_RESTART_INTERVAL = 50  # Restart browser every N opened albums to manage memory
DEFAULT_RECENT_PERIOD = "30"  # Album feed period: /photos/sg/recent/<period>/ (e.g. "all", "30")
DEFAULT_DOWNLOAD_CONCURRENCY = 8  # parallel image downloads from the image CDN
DEFAULT_ALBUM_DELAY_MIN = 0.0  # optional pause between album page loads (seconds)
DEFAULT_ALBUM_DELAY_MAX = 0.0
DEFAULT_SKIP_COMPLETED_ALBUMS = True
DEFAULT_BLOCK_PAGE_ASSETS = True  # don't render images/fonts/media on listing pages
DEFAULT_FEED_WAIT_TIMEOUT = 5000  # ms to wait for more albums after load-more/scroll
DEFAULT_FEED_MAX_IDLE = 2  # stop scrolling after N attempts that load nothing new
DEFAULT_ALBUM_WAIT_TIMEOUT = 15000  # ms to wait for an album's photo links to appear

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Age-confirmation interstitial ("This is an adult website ... Enter - I'm 18+").
# It is a fancybox modal whose full-page .fancybox-overlay intercepts every click,
# so it must be confirmed before anything else on the page can be clicked.
AGE_GATE_CONFIRM_SELECTORS = [
    "[data-adult-confirm]",
    ".adult-gate button:has-text('Enter')",
    "#adult-gate button",
]
BLOCKING_OVERLAY_SELECTOR = ".fancybox-overlay"

LOGIN_SELECTORS = [
    "#login",
    "a:has-text('Login')",
    "a:has-text('Log in')",
    "button:has-text('Login')",
    "button:has-text('Log in')",
    ".login-button",
    "[data-action='login']",
    "a[href*='login']",
]

ALBUM_PHOTO_SELECTOR = (
    "li.photo-container a[href*='cloudfront'], li.photo-container a[href*='amazonaws']"
)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}
PART_PREFIX = ".sgpart-"  # in-progress downloads; renamed to the real name when complete
MIN_VALID_IMAGE_SIZE = 10000

# HTTP statuses worth retrying (rate limiting / transient server trouble).
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def looks_like_image(header: bytes) -> bool:
    """True if the first bytes are a known image format's magic bytes."""
    return (
        header[0:2] == b"\xff\xd8"  # JPEG
        or header[0:4] == b"\x89PNG"  # PNG
        or header[0:6] in (b"GIF87a", b"GIF89a")  # GIF
        or (header[0:4] == b"RIFF" and header[8:12] == b"WEBP")  # WebP
        or header[0:2] == b"BM"  # BMP
    )


def parse_retry_after(value, now: float = None):
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds, or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, when - (now if now is not None else time.time()))


class RetryableError(Exception):
    """A failure worth retrying, optionally with a server-requested wait."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """A failure that retrying won't fix (e.g. HTTP 404)."""


class Profiler:
    """Thread-safe accumulator of wall time per phase plus simple counters."""

    def __init__(self):
        self.lock = threading.Lock()
        self.times = defaultdict(float)
        self.calls = defaultdict(int)
        self.counters = defaultdict(int)
        self.started = time.time()

    @contextmanager
    def phase(self, name):
        t = time.time()
        try:
            yield
        finally:
            self.add(name, time.time() - t)

    def add(self, name, seconds):
        with self.lock:
            self.times[name] += seconds
            self.calls[name] += 1

    def count(self, name, n=1):
        with self.lock:
            self.counters[name] += n

    def summary(self) -> str:
        wall = time.time() - self.started
        lines = ["=== Timing summary ===", f"  {'total wall time':38s} {wall:8.1f}s"]
        for name, secs in sorted(self.times.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {name:38s} {secs:8.1f}s  ({self.calls[name]}x)")
        for name, n in sorted(self.counters.items()):
            lines.append(f"  {name:38s} {n}")
        mb = self.counters.get("bytes downloaded", 0) / 1e6
        dl = self.times.get("image downloads (wall, per album)", 0)
        if mb and dl:
            lines.append(f"  {'download throughput':38s} {mb / dl:8.1f} MB/s")
        return "\n".join(lines)


class SGSpider:
    """Main spider class that handles all scraping operations."""

    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.credentials = None
        self.base_url = "https://www.suicidegirls.com"
        self.download_dir = Path("suicidegirls").absolute()
        self.placeholder_hash = None  # Hash of the unauthenticated placeholder image
        self.placeholder_size = None  # Its size, so existing files only get hashed when they match
        self.state_file = Path(__file__).parent / ".sgspider.state.json"
        self.album_index_file = Path(__file__).parent / ".sgspider.albums.json"
        self.album_index = {}
        self.profiler = Profiler()

        # Settings loaded from config (with defaults)
        self.headless = DEFAULT_HEADLESS
        self.max_retries = DEFAULT_MAX_RETRIES
        self.retry_base_delay = DEFAULT_RETRY_BASE_DELAY
        self.max_backoff = DEFAULT_MAX_BACKOFF
        self.download_timeout = DEFAULT_DOWNLOAD_TIMEOUT
        self.page_load_timeout = DEFAULT_PAGE_LOAD_TIMEOUT
        self.max_album_pages = DEFAULT_MAX_ALBUM_PAGES
        self.browser_restart_interval = DEFAULT_BROWSER_RESTART_INTERVAL
        self.recent_period = DEFAULT_RECENT_PERIOD
        self.download_concurrency = DEFAULT_DOWNLOAD_CONCURRENCY
        self.album_delay_min = DEFAULT_ALBUM_DELAY_MIN
        self.album_delay_max = DEFAULT_ALBUM_DELAY_MAX
        self.skip_completed_albums = DEFAULT_SKIP_COMPLETED_ALBUMS
        self.block_page_assets = DEFAULT_BLOCK_PAGE_ASSETS
        self.feed_wait_timeout = DEFAULT_FEED_WAIT_TIMEOUT
        self.feed_max_idle = DEFAULT_FEED_MAX_IDLE
        self.album_wait_timeout = DEFAULT_ALBUM_WAIT_TIMEOUT

        # Playwright instance reference (needed for browser restarts)
        self.playwright = None
        # One requests.Session per download thread (Session isn't thread-safe)
        self._local = threading.local()
        self._cookie_snapshot = []
        self._cookie_version = 0

    # ------------------------------------------------------------------ config

    def load_credentials(self) -> dict:
        """Load credentials and settings from config file."""
        print("Reading configuration...")
        config = configparser.ConfigParser(interpolation=None)
        config.read("sgspider.ini")
        self.credentials = config

        if config.has_section("settings"):
            s = config["settings"]
            self.headless = s.getboolean("headless", self.headless)
            self.max_retries = max(1, s.getint("max_retries", self.max_retries))
            self.retry_base_delay = s.getfloat("retry_base_delay", self.retry_base_delay)
            self.max_backoff = s.getfloat("max_backoff", self.max_backoff)
            self.download_timeout = s.getint("download_timeout", self.download_timeout)
            self.page_load_timeout = s.getint("page_load_timeout", self.page_load_timeout)
            self.max_album_pages = s.getint("max_album_pages", self.max_album_pages)
            self.browser_restart_interval = s.getint("browser_restart_interval", self.browser_restart_interval)
            self.recent_period = s.get("recent_period", self.recent_period).strip().strip("/") or DEFAULT_RECENT_PERIOD
            self.download_concurrency = max(1, s.getint("download_concurrency", self.download_concurrency))
            self.album_delay_min = max(0.0, s.getfloat("album_delay_min", self.album_delay_min))
            self.album_delay_max = max(self.album_delay_min, s.getfloat("album_delay_max", self.album_delay_max))
            self.skip_completed_albums = s.getboolean("skip_completed_albums", self.skip_completed_albums)
            self.block_page_assets = s.getboolean("block_page_assets", self.block_page_assets)
            self.feed_wait_timeout = s.getint("feed_wait_timeout", self.feed_wait_timeout)
            self.feed_max_idle = max(1, s.getint("feed_max_idle", self.feed_max_idle))
            self.album_wait_timeout = s.getint("album_wait_timeout", self.album_wait_timeout)

        print("Configuration loaded.")
        print(
            f"  download_concurrency={self.download_concurrency} "
            f"album_delay={self.album_delay_min}-{self.album_delay_max}s "
            f"skip_completed_albums={self.skip_completed_albums} block_page_assets={self.block_page_assets}"
        )
        return config

    # ------------------------------------------------------------------ waits

    def sleep(self, seconds: float, reason: str = "sleep/backoff"):
        """All deliberate sleeps go through here so they show up in the timing summary."""
        if seconds <= 0:
            return
        with self.profiler.phase(reason):
            time.sleep(seconds)

    def album_delay(self):
        """Optional pause between album page loads (0 by default)."""
        if self.album_delay_max > 0:
            self.sleep(random.uniform(self.album_delay_min, self.album_delay_max), "album delay (configured)")

    def backoff_delay(self, attempt: int, retry_after=None) -> float:
        """Seconds to wait before retry number ``attempt`` (0-based). Retry-After wins when given."""
        if retry_after is not None:
            return min(self.max_backoff, retry_after)
        return min(self.max_backoff, self.retry_base_delay * (2 ** attempt) + random.uniform(0, 1))

    def retry_operation(self, operation, description: str, max_retries: int = None):
        """
        Execute an operation with exponential backoff retry logic.

        Returns the operation's result, or None if all retries failed.
        PermanentError is not retried; RetryableError.retry_after is honoured.
        """
        if max_retries is None:
            max_retries = self.max_retries

        for attempt in range(max_retries):
            try:
                return operation()
            except PermanentError as e:
                print(f"  Giving up on {description}: {e}")
                return None
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = self.backoff_delay(attempt, getattr(e, "retry_after", None))
                    print(f"  Attempt {attempt + 1}/{max_retries} failed for {description}: {e}")
                    print(f"  Retrying in {delay:.1f} seconds...")
                    self.sleep(delay)
                else:
                    print(f"  All {max_retries} attempts failed for {description}: {e}")
        return None

    # ------------------------------------------------------------------ browser

    def start_browser(self, playwright):
        """Initialize the browser with anti-detection settings."""
        self.playwright = playwright
        print(f"System architecture: {platform.machine()}")
        print("Launching Playwright Chromium browser...")

        with self.profiler.phase("browser start"):
            self.browser = playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # Comprehensive GPU disabling
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-gpu-sandbox",
                    "--disable-software-rasterizer",
                    "--disable-accelerated-2d-canvas",
                    "--disable-accelerated-video-decode",
                    "--disable-accelerated-video-encode",
                    "--disable-webgl",
                    "--disable-webgl2",
                    "--use-gl=swiftshader",
                    "--disable-features=VizDisplayCompositor,UseSkiaRenderer,Vulkan",
                    # Anti-detection
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--disable-extensions",
                    "--disable-default-apps",
                    "--no-first-run",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-client-side-phishing-detection",
                    "--disable-crash-reporter",
                    "--disable-oopr-debug-crash-dump",
                    "--no-crash-upload",
                    "--disable-low-res-tiling",
                    "--ignore-certificate-errors",
                    "--ignore-ssl-errors",
                    "--ignore-certificate-errors-spki-list",
                    "--allow-running-insecure-content",
                    "--disable-web-security",
                ],
            )

            self.context = self.browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=USER_AGENT,
            )

            self.context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                    configurable: true
                });

                for (let prop in window) {
                    if (prop && prop.includes('webdriver')) {
                        delete window[prop];
                    }
                }

                Object.defineProperty(navigator, 'plugins', {
                    get: () => [{
                        description: "Portable Document Format",
                        filename: "internal-pdf-viewer",
                        name: "Chrome PDF Plugin"
                    }, {
                        description: "Chromium PDF Plugin",
                        filename: "libpdf.so",
                        name: "Chromium PDF Viewer"
                    }],
                    configurable: true
                });

                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                    configurable: true
                });

                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            """)

            if self.block_page_assets:
                # Pages are only read for links; never render images/fonts/media in the browser.
                # (Image downloads use requests, which is unaffected.)
                def block_assets(route):
                    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                        route.abort()
                    else:
                        route.continue_()

                self.context.route("**/*", block_assets)

            self.page = self.context.new_page()
            self.page.set_default_timeout(self.page_load_timeout)

        # Handle popup windows - close any unwanted new tabs/popups
        def handle_popup(popup):
            popup_url = popup.url
            if "suicidegirls.com" not in popup_url:
                print(f"  Closing unwanted popup: {popup_url}")
                popup.close()

        self.context.on("page", handle_popup)
        print("Browser initialized successfully.")

    def stop_browser(self):
        """Clean up browser resources."""
        if self.browser:
            self.browser.close()
            print("Browser closed.")

    def restart_browser(self) -> bool:
        """Restart browser to free memory. Preserves login state by re-authenticating."""
        print("\n=== Restarting browser to free memory ===")
        self.stop_browser()
        self.browser = None
        self.context = None
        self.page = None
        gc.collect()

        if not self.playwright:
            print("Error: No playwright instance available for restart.")
            return False
        self.start_browser(self.playwright)
        if self.login():
            print("Browser restarted and re-logged in successfully.")
            return True
        print("Browser restarted but re-login failed!")
        return False

    # ------------------------------------------------------------------ HTTP client

    def sync_cookies(self):
        """Snapshot the browser context's cookies for the download threads."""
        try:
            cookies = self.context.cookies()
        except Exception as e:
            print(f"  Warning: could not read browser cookies: {e}")
            return
        if cookies != self._cookie_snapshot:
            self._cookie_snapshot = cookies
            self._cookie_version += 1

    def http_session(self) -> requests.Session:
        """The calling thread's requests.Session, carrying the browser's current cookies."""
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": USER_AGENT,
                "Referer": self.base_url + "/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            })
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self._local.session = sess
            self._local.cookie_version = -1
        if self._local.cookie_version != self._cookie_version:
            sess.cookies.clear()
            for c in self._cookie_snapshot:
                sess.cookies.set(
                    c["name"], c["value"],
                    domain=c.get("domain", ""), path=c.get("path", "/"),
                    secure=c.get("secure", False),
                )
            self._local.cookie_version = self._cookie_version
        return sess

    def capture_placeholder_hash(self, sample_image_url: str) -> bool:
        """
        Download an image WITHOUT authentication to capture the placeholder image hash,
        used to detect when we're getting placeholder images instead of real content.
        """
        print("\n=== Capturing Placeholder Image Hash ===")
        print(f"Downloading image without authentication: {sample_image_url}")
        try:
            with self.profiler.phase("placeholder capture"):
                resp = requests.get(
                    sample_image_url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.download_timeout / 1000,
                )
            if resp.status_code != 200:
                print(f"  Failed to download: HTTP {resp.status_code}")
                return False
            body = resp.content
            self.placeholder_hash = hashlib.sha256(body).hexdigest()
            self.placeholder_size = len(body)
            print(f"  Placeholder image size: {len(body)} bytes")
            print(f"  Placeholder hash: {self.placeholder_hash}")
            print("  This hash will be used to detect authentication failures.")
            return True
        except Exception as e:
            print(f"  Error capturing placeholder hash: {e}")
            return False

    def is_placeholder_image(self, data: bytes) -> bool:
        """True if the downloaded data matches the placeholder image."""
        if not self.placeholder_hash:
            return False
        return hashlib.sha256(data).hexdigest() == self.placeholder_hash

    def is_valid_existing_file(self, file_path: Path, size: int = None) -> bool:
        """
        Cheap validity check for a file already on disk.

        The download directory is a goofys (S3/B2) mount where every read is a remote
        GET (~0.5 s for a 16-byte header, several seconds for a full file), so this
        only uses the size from the directory listing. Files we write are validated
        (magic bytes, length, not-the-placeholder) before they get their final name,
        so size is enough: anything under 10 KB is a broken download, and a file is
        only read and hashed when its size equals the placeholder's.
        """
        try:
            if size is None:
                size = file_path.stat().st_size
            if size < MIN_VALID_IMAGE_SIZE:
                return False
            if self.placeholder_hash and size == self.placeholder_size:
                with open(file_path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest() == self.placeholder_hash:
                        return False
            return True
        except OSError:
            return False

    def download_image(self, url: str, save_path: Path) -> tuple:
        """
        Fetch one image with requests and stream it to disk.

        Data goes to a hidden part file next to the target and is renamed to the
        real name only once it is complete and validated, so an interrupted run
        never leaves a partial file under a real image name.

        Returns (success: bool, is_placeholder: bool).
        """
        def do_download():
            sess = self.http_session()
            part = save_path.parent / f"{PART_PREFIX}{save_path.name}"
            try:
                resp = sess.get(url, stream=True, timeout=self.download_timeout / 1000)
            except requests.RequestException as e:
                raise RetryableError(f"{type(e).__name__}: {e}")
            with resp:
                if resp.status_code in RETRYABLE_STATUSES:
                    self.profiler.count(f"HTTP {resp.status_code} responses")
                    raise RetryableError(
                        f"HTTP {resp.status_code}",
                        retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                    )
                if resp.status_code != 200:
                    self.profiler.count(f"HTTP {resp.status_code} responses")
                    raise PermanentError(f"HTTP {resp.status_code}")
                expected = resp.headers.get("Content-Length")
                digest = hashlib.sha256()
                written = 0
                header = b""
                t_io = 0.0
                try:
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=256 * 1024):
                            if not chunk:
                                continue
                            if len(header) < 16:
                                header += chunk[: 16 - len(header)]
                            digest.update(chunk)
                            t = time.time()
                            fh.write(chunk)
                            t_io += time.time() - t
                            written += len(chunk)
                    if self.placeholder_hash and digest.hexdigest() == self.placeholder_hash:
                        part.unlink(missing_ok=True)
                        return (False, True)  # Got placeholder - auth issue
                    if expected is not None and written != int(expected):
                        raise RetryableError(f"truncated: got {written} of {expected} bytes")
                    if written < 1000 or not looks_like_image(header):
                        raise RetryableError(f"not an image ({written} bytes)")
                    t = time.time()
                    os.replace(part, save_path)
                    t_io += time.time() - t
                except BaseException:
                    try:
                        part.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
                finally:
                    self.profiler.add("file writes (summed over threads)", t_io)
                self.profiler.count("bytes downloaded", written)
                return (True, False)

        result = self.retry_operation(do_download, f"download {save_path.name}")
        if result is None:
            return (False, False)
        return result

    # Kept for compatibility with older callers/tests.
    download_image_via_navigation = download_image

    # ------------------------------------------------------------------ site

    def dismiss_age_gate(self, wait_ms: int = 0) -> bool:
        """Confirm the site's age-verification modal if it is showing.

        The modal's overlay intercepts pointer events, so the login button (and
        anything else) can't be clicked until it is dismissed. Confirming sets the
        site's ``confirmed-adult`` cookie, so it normally only appears once per
        browser context.

        Args:
            wait_ms: how long to wait for the gate to appear (0 = only check now).

        Returns:
            True if a gate was found and dismissed, False if none was showing.
        """
        combined = self.page.locator(", ".join(AGE_GATE_CONFIRM_SELECTORS)).first
        if wait_ms:
            try:
                combined.wait_for(state="visible", timeout=wait_ms)
            except PlaywrightTimeout:
                return False
        for selector in AGE_GATE_CONFIRM_SELECTORS:
            try:
                btn = self.page.locator(selector).first
                if btn.is_visible():
                    print("Confirming age-verification prompt...")
                    btn.click(timeout=10000)
                    try:
                        self.page.locator(BLOCKING_OVERLAY_SELECTOR).first.wait_for(
                            state="hidden", timeout=10000
                        )
                    except PlaywrightTimeout:
                        print("  Warning: overlay still visible after confirming age prompt")
                    return True
            except PlaywrightTimeout:
                continue
            except Exception as e:
                print(f"  Could not dismiss age prompt via {selector}: {e}")
                continue
        return False

    def accept_cookies(self):
        """Accept cookie consent if present."""
        try:
            cookie_btn = self.page.locator(
                "button:has-text('Accept'), button:has-text('Agree'), button:has-text('OK')"
            ).first
            if cookie_btn.is_visible():
                print("Accepting cookies...")
                cookie_btn.click(timeout=10000)
        except Exception:
            pass

    def is_logged_in(self) -> bool:
        """Check if we're currently logged in."""
        try:
            url = self.page.url.lower()
            if "login" in url or "join" in url:
                return False

            logged_in_selectors = [
                "a[href*='/member/']",  # Member profile link
                "a[href*='/account']",  # Account settings
                ".user-menu",           # User menu
                ".logged-in",           # Logged-in class
                "#logout",              # Logout button
                "a[href*='logout']",    # Logout link
                ".member-nav",          # Member navigation
            ]
            for selector in logged_in_selectors:
                try:
                    if self.page.locator(selector).first.is_visible():
                        return True
                except Exception:
                    continue

            content = self.page.content().lower()
            if "logout" in content:
                return True
            if self.credentials:
                username = self.credentials["main"]["username"].lower()
                if username in content:
                    return True
            return False
        except Exception as e:
            print(f"Error checking login status: {e}")
            return False

    def wait_until_logged_in(self, timeout_s: float = 20.0) -> bool:
        """Poll the page until it shows the logged-in state (or time runs out)."""
        deadline = time.time() + timeout_s
        while True:
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            if self.is_logged_in():
                return True
            if time.time() >= deadline:
                return False
            self.page.wait_for_timeout(250)

    def login(self) -> bool:
        """Perform login to the site. Returns True if login was successful."""
        def attempt_login():
            print("Navigating to main page...")
            self.page.goto(self.base_url, wait_until="domcontentloaded")
            # Wait for whichever shows up first: the age gate or the page's login control.
            gate_or_login = ", ".join(AGE_GATE_CONFIRM_SELECTORS + ["#login"])
            try:
                self.page.locator(gate_or_login).first.wait_for(state="visible", timeout=15000)
            except PlaywrightTimeout:
                pass
            self.dismiss_age_gate()

            if self.is_logged_in():
                print("Already logged in!")
                return True

            self.accept_cookies()
            self.page.keyboard.press("Escape")
            self.dismiss_age_gate()

            login_clicked = False
            last_click_error = None
            for selector in LOGIN_SELECTORS:
                try:
                    btn = self.page.locator(selector).first
                    if btn.is_visible():
                        print(f"Found login button: {selector}")
                        btn.click(timeout=10000)
                        self.page.wait_for_selector(
                            "input[name='username']", state="visible", timeout=5000
                        )
                        print("Login form appeared.")
                        login_clicked = True
                        break
                except Exception as e:
                    # Keep the first line of the real reason (e.g. an overlay
                    # intercepting clicks) instead of silently swallowing it.
                    last_click_error = f"{selector}: {str(e).splitlines()[0]}"
                    print(f"  Login button {selector} did not open the form: {last_click_error}")
                    continue

            if not login_clicked:
                detail = f" (last error: {last_click_error})" if last_click_error else ""
                raise Exception(f"Could not open login form{detail}")

            user_field = self.page.locator("input[name='username']")
            pass_field = self.page.locator("input[name='password']")

            print("Entering username...")
            user_field.fill(self.credentials["main"]["username"])
            print("Entering password...")
            pass_field.fill(self.credentials["main"]["password"])

            print("Submitting login form...")
            pass_field.press("Enter")

            if not self.wait_until_logged_in():
                raise Exception("Login verification failed")
            return True

        print("\n=== Logging In ===")
        with self.profiler.phase("login"):
            result = self.retry_operation(attempt_login, "login")

        if result:
            self.sync_cookies()
            print("Login successful!")
            return True
        print("Login failed after all retries.")
        return False

    def ensure_logged_in(self) -> bool:
        """Check login status and re-login if necessary."""
        if self.is_logged_in():
            return True
        print("Session expired, re-authenticating...")
        return self.login()

    def _feed_state(self):
        return self.page.evaluate(
            "() => [document.querySelectorAll(\"a[href*='/album/']\").length, document.body.scrollHeight]"
        )

    def _wait_for_feed_growth(self, before) -> bool:
        """Wait (up to feed_wait_timeout) for more album links or a taller page."""
        try:
            self.page.wait_for_function(
                """([links, height]) =>
                    document.querySelectorAll("a[href*='/album/']").length > links ||
                    document.body.scrollHeight > height""",
                arg=list(before),
                timeout=self.feed_wait_timeout,
            )
            return True
        except PlaywrightTimeout:
            return False

    def collect_album_urls(self) -> list:
        """Navigate to the photos feed, load all of it and collect album URLs."""
        print("\n=== Collecting Album URLs ===")
        with self.profiler.phase("feed load+scroll"):
            def load_albums_page():
                self.page.goto(
                    f"{self.base_url}/photos/sg/recent/{self.recent_period}/", wait_until="domcontentloaded"
                )
                self.dismiss_age_gate()
                if "server error" in self.page.content().lower():
                    raise Exception("Server error on photos page")
                return True

            if not self.retry_operation(load_albums_page, "load photos page"):
                print("Failed to load photos page.")
                return []

            if self.max_album_pages > 0:
                print(f"Scrolling to load albums (limited to {self.max_album_pages} iterations)...")
            else:
                print("Scrolling to load albums...")
            print("Progress: [", end="", flush=True)

            idle = 0
            pages_loaded = 0
            limit_reached = False
            while idle < self.feed_max_idle:
                if self.max_album_pages > 0 and pages_loaded >= self.max_album_pages:
                    limit_reached = True
                    break
                pages_loaded += 1
                try:
                    before = self._feed_state()
                    load_more = self.page.locator("#load-more").first
                    if load_more.is_visible() and load_more.is_enabled():
                        load_more.click(timeout=10000)
                        mark = "."
                    else:
                        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                        mark = "s"
                    if self._wait_for_feed_growth(before):
                        print(mark, end="", flush=True)
                        idle = 0
                    else:
                        print("x", end="", flush=True)
                        idle += 1
                except Exception:
                    print("x", end="", flush=True)
                    idle += 1

            suffix = " - LIMIT REACHED" if limit_reached else ""
            print(f"] ({pages_loaded} iterations{suffix})")

            print("Extracting album URLs...", flush=True)
            sharing_patterns = [
                "twitter.com", "mailto:", "facebook.com", "pinterest.com",
                "reddit.com", "tumblr.com", "instagram.com", "/share?",
                "?&body=", "share=", "intent/tweet",
            ]
            all_hrefs = self.page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
            )
            print(f"Found {len(all_hrefs)} total links, filtering...", flush=True)

            album_urls = []
            seen = set()
            for href in all_hrefs:
                if not href or "/girls/" not in href or "/album/" not in href:
                    continue
                if any(pattern in href.lower() for pattern in sharing_patterns):
                    continue
                if urlparse(self.base_url).netloc.split(":")[0] not in href and "suicidegirls.com" not in href:
                    continue
                href = href.replace("http://www.suicidegirls.com", "https://www.suicidegirls.com")
                if href not in seen:
                    seen.add(href)
                    album_urls.append(href)

        print(f"Found {len(album_urls)} unique albums.", flush=True)
        return album_urls

    def parse_album_url(self, url: str) -> tuple:
        """Parse an album URL into (girl_name, album_name)."""
        try:
            parts = [p for p in urlparse(url).path.split("/") if p]
            # Expected: ['girls', 'USERNAME', 'album', 'ID', 'ALBUM_NAME']
            if len(parts) >= 5 and parts[0] == "girls" and parts[2] == "album":
                return parts[1], parts[4]
            if "/girls/" in url and "/album/" in url:
                girl = url.split("/girls/")[1].split("/")[0]
                album_parts = url.split("/album/")[1].split("/")
                album = album_parts[1] if len(album_parts) >= 2 else "unknown"
                return girl, album
        except Exception:
            pass
        return "unknown", "unknown"

    def extract_image_urls(self, album_url: str) -> list:
        """Open an album page and return its full-size image URLs (query strings kept)."""
        def load_and_extract():
            self.page.goto(album_url, wait_until="domcontentloaded")
            self.dismiss_age_gate()

            current_url = self.page.url.lower()
            if "join" in current_url or "login" in current_url:
                raise Exception("Redirected to login page")

            try:
                self.page.wait_for_selector(ALBUM_PHOTO_SELECTOR, state="attached", timeout=self.album_wait_timeout)
            except PlaywrightTimeout:
                # No photo grid (empty album / different layout): fall back below once the page settled.
                try:
                    self.page.wait_for_load_state("load", timeout=self.album_wait_timeout)
                except PlaywrightTimeout:
                    pass

            # IMPORTANT: keep the full URL including query params - they may contain auth tokens
            hrefs = self.page.evaluate(
                "sel => Array.from(document.querySelectorAll(sel)).map(a => a.getAttribute('href'))",
                ALBUM_PHOTO_SELECTOR,
            )
            image_urls = []
            for href in hrefs:
                if href and href not in image_urls:
                    image_urls.append(href)

            if not image_urls:
                for href in self.page.evaluate(
                    "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
                ):
                    if not href or ("cloudfront" not in href and "amazonaws" not in href):
                        continue
                    if href.split("?")[0].lower().endswith(IMAGE_EXTENSIONS) and href not in image_urls:
                        image_urls.append(href)
            return image_urls

        with self.profiler.phase("album page load+extract"):
            result = self.retry_operation(load_and_extract, f"extract images from {album_url}")
        return result if result else []

    # ------------------------------------------------------------------ album index

    @staticmethod
    def album_key(album_url: str) -> str:
        return urlparse(album_url).path.rstrip("/")

    def load_album_index(self):
        try:
            self.album_index = json.loads(self.album_index_file.read_text())
        except FileNotFoundError:
            self.album_index = {}
        except Exception as e:
            print(f"Warning: could not read album index ({e}); starting a new one.")
            self.album_index = {}

    def save_album_index(self):
        tmp = self.album_index_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.album_index, indent=0, sort_keys=True))
            os.replace(tmp, self.album_index_file)
        except Exception as e:
            print(f"Warning: could not save album index: {e}")

    def list_album_dir(self, album_dir: Path) -> dict:
        """filename -> size for the album directory (one listing call), cleaning stale part files."""
        files = {}
        try:
            with os.scandir(album_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    if entry.name.startswith(PART_PREFIX):
                        try:
                            os.unlink(entry.path)
                            print(f"    Removed leftover partial download: {entry.name}")
                        except OSError:
                            pass
                        continue
                    files[entry.name] = entry.stat().st_size
        except FileNotFoundError:
            pass
        return files

    def album_already_complete(self, album_url: str, album_dir: Path) -> bool:
        """True if the index says this album was fully downloaded and its files are still there."""
        entry = self.album_index.get(self.album_key(album_url))
        if not entry:
            return False
        names = entry.get("files") or []
        if not names:
            return False
        with self.profiler.phase("skip check (album dir listing)"):
            on_disk = self.list_album_dir(album_dir)
        return all(on_disk.get(n, 0) >= MIN_VALID_IMAGE_SIZE for n in names)

    def mark_album_complete(self, album_url: str, filenames: list):
        self.album_index[self.album_key(album_url)] = {"files": sorted(filenames), "completed": int(time.time())}
        self.save_album_index()

    # ------------------------------------------------------------------ albums

    @staticmethod
    def image_filename(img_url: str, fallback_index: int) -> str:
        filename = img_url.split("/")[-1].split("?")[0] or f"image_{fallback_index}.jpg"
        return re.sub(r'[<>:"/\\|?*]', "_", filename)

    def process_album(self, album_url: str) -> tuple:
        """
        Process a single album: extract and download all images.

        Returns (downloaded_count, auth_failure, opened_page).
        """
        girl_name, album_name = self.parse_album_url(album_url)
        album_dir = self.download_dir / girl_name / album_name
        print(f"\n  Album: {girl_name}/{album_name}")

        if self.skip_completed_albums and self.album_already_complete(album_url, album_dir):
            print("  Already complete on disk (album index) - skipped without opening")
            self.profiler.count("albums skipped (index)")
            return (0, False, False)

        image_urls = self.extract_image_urls(album_url)
        self.profiler.count("albums opened")
        if not image_urls:
            print("  No images found in album.")
            return (0, False, True)
        print(f"  Found {len(image_urls)} images")

        if self.placeholder_hash is None:
            self.capture_placeholder_hash(image_urls[0].split("?")[0])

        with self.profiler.phase("existing-file check (dir listing)"):
            on_disk = self.list_album_dir(album_dir)

        jobs = []
        filenames = []
        skipped = 0
        for n, img_url in enumerate(image_urls, 1):
            filename = self.image_filename(img_url, n)
            if filename in filenames:
                continue  # two URLs for the same file: fetch it once
            filenames.append(filename)
            save_path = album_dir / filename
            if filename in on_disk:
                if self.is_valid_existing_file(save_path, on_disk[filename]):
                    skipped += 1
                    continue
                print(f"    Replacing corrupted: {filename}")
                try:
                    save_path.unlink()
                except OSError:
                    pass
            jobs.append((img_url, save_path))

        downloaded = 0
        failed = 0
        placeholders = 0
        if jobs:
            self.sync_cookies()
            with self.profiler.phase("image downloads (wall, per album)"):
                with ThreadPoolExecutor(max_workers=min(self.download_concurrency, len(jobs))) as pool:
                    futures = {pool.submit(self.download_image, u, p): p for u, p in jobs}
                    for fut in as_completed(futures):
                        path = futures[fut]
                        try:
                            success, is_placeholder = fut.result()
                        except Exception as e:
                            success, is_placeholder = False, False
                            print(f"    Error: {path.name}: {e}")
                        if success:
                            downloaded += 1
                            print(f"    Downloaded: {path.name}")
                        elif is_placeholder:
                            placeholders += 1
                            print(f"    AUTH FAILURE: {path.name} (got placeholder image)")
                            if placeholders >= 2:
                                for f in futures:
                                    f.cancel()
                        else:
                            failed += 1
                            print(f"    Failed: {path.name}")
            self.profiler.count("images downloaded", downloaded)

        if skipped:
            print(f"  Skipped {skipped} existing files")
            self.profiler.count("images already on disk", skipped)
        print(f"  Downloaded {downloaded} new images")

        if placeholders >= 2:
            print("  Multiple placeholder images detected - session expired!")
            return (downloaded, True, True)
        if failed == 0 and placeholders == 0:
            self.mark_album_complete(album_url, filenames)
        return (downloaded, False, True)

    # ------------------------------------------------------------------ resume state

    def save_state(self, albums: list, current_index: int, total_downloaded: int):
        """Save current progress to state file for resume capability."""
        state = {
            "albums": albums,
            "current_index": current_index,
            "total_downloaded": total_downloaded,
            "timestamp": time.time(),
        }
        try:
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"Warning: Could not save state: {e}")

    def load_state(self) -> dict:
        """Load saved state if it exists."""
        if not self.state_file.exists():
            return None
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load state: {e}")
            return None

    def clear_state(self):
        """Remove state file when run completes successfully."""
        try:
            if self.state_file.exists():
                self.state_file.unlink()
        except Exception as e:
            print(f"Warning: Could not remove state file: {e}")

    # ------------------------------------------------------------------ entry points

    def check_login(self) -> bool:
        """Smoke check: log in with the configured account and report the result."""
        self.load_credentials()
        with sync_playwright() as playwright:
            try:
                self.start_browser(playwright)
                ok = self.login()
                print("LOGIN CHECK: OK" if ok else "LOGIN CHECK: FAILED")
                print(self.profiler.summary())
                return bool(ok)
            finally:
                self.stop_browser()

    def bench_downloads(self, album_url: str, levels: list, dest: Path) -> list:
        """Download one album's images at each concurrency level into dest; report MB/s."""
        self.load_credentials()
        results = []
        with sync_playwright() as playwright:
            try:
                self.start_browser(playwright)
                if not self.login():
                    return results
                urls = self.extract_image_urls(album_url)
                print(f"Benchmark album has {len(urls)} images")
                self.capture_placeholder_hash(urls[0].split("?")[0])
                self.sync_cookies()
                for level in levels:
                    target = dest / f"c{level}"
                    target.mkdir(parents=True, exist_ok=True)
                    before_bytes = self.profiler.counters.get("bytes downloaded", 0)
                    before_errs = sum(v for k, v in self.profiler.counters.items() if k.startswith("HTTP "))
                    t = time.time()
                    with ThreadPoolExecutor(max_workers=level) as pool:
                        outcomes = list(pool.map(
                            lambda u: self.download_image(u, target / self.image_filename(u, 0)), urls
                        ))
                    secs = time.time() - t
                    mb = (self.profiler.counters.get("bytes downloaded", 0) - before_bytes) / 1e6
                    ok = sum(1 for s, _ in outcomes if s)
                    errs = sum(v for k, v in self.profiler.counters.items() if k.startswith("HTTP ")) - before_errs
                    print(f"BENCH concurrency={level:3d} ok={ok}/{len(urls)} http_errors={errs} "
                          f"{mb:.1f} MB in {secs:.1f}s = {mb / secs:.1f} MB/s")
                    results.append((level, ok, errs, mb, secs))
                    for f in target.iterdir():
                        f.unlink()
                    target.rmdir()
            finally:
                self.stop_browser()
        return results

    def run(self, album_urls: list = None):
        """Main entry point - run the spider.

        Args:
            album_urls: Optional list of specific album URLs to process.
                        If not provided, collects albums from the feed.
        """
        print("=" * 60)
        print("SGSpider - Starting")
        print("=" * 60)

        self.load_credentials()
        self.load_album_index()

        saved_state = self.load_state()
        start_index = 0
        total_downloaded = 0
        albums = None

        if saved_state and not album_urls:
            state_age_hours = (time.time() - saved_state.get("timestamp", 0)) / 3600
            if state_age_hours < 24:
                albums = saved_state.get("albums", [])
                start_index = saved_state.get("current_index", 0)
                total_downloaded = saved_state.get("total_downloaded", 0)
                if albums and start_index < len(albums):
                    print("\n=== Resuming from saved state ===")
                    print(f"  State saved {state_age_hours:.1f} hours ago")
                    print(f"  Resuming at album {start_index + 1} of {len(albums)}")
                    print(f"  Previously downloaded: {total_downloaded} images")
                else:
                    albums = None
                    start_index = 0
                    total_downloaded = 0
                    self.clear_state()
            else:
                print(f"\nFound saved state but it's {state_age_hours:.1f} hours old (>24h). Starting fresh.")
                self.clear_state()

        with sync_playwright() as playwright:
            try:
                self.start_browser(playwright)

                if not self.login():
                    print("Failed to log in. Exiting.")
                    return

                if albums is None:
                    if album_urls:
                        albums = album_urls
                        print(f"\n=== Processing {len(albums)} Specified Album(s) ===")
                    else:
                        albums = self.collect_album_urls()

                if not albums:
                    print("No albums found. Exiting.")
                    return

                if start_index > 0:
                    print(f"\n=== Resuming: Processing albums {start_index + 1} to {len(albums)} ===")
                else:
                    print(f"\n=== Processing {len(albums)} Albums ===")

                failed_albums = 0
                opened_since_restart = 0
                for i in range(start_index, len(albums)):
                    album_url = albums[i]
                    print(f"\n[{i + 1}/{len(albums)}] {album_url}")

                    opened = False
                    try:
                        count, auth_failure, opened = self.process_album(album_url)
                        total_downloaded += count
                        if auth_failure:
                            print("  Auth failure detected, attempting re-login...")
                            if self.login():
                                print("  Re-login successful, continuing...")
                                failed_albums = 0
                            else:
                                print("  Re-login failed!")
                                failed_albums += 1
                        else:
                            failed_albums = 0
                    except Exception as e:
                        print(f"  Error processing album: {e}")
                        failed_albums += 1
                        opened = True

                    if not album_urls:
                        self.save_state(albums, i + 1, total_downloaded)

                    if failed_albums >= 3:
                        print("\nToo many consecutive failures, attempting recovery...")
                        if not self.login():
                            print("Session lost and could not recover. Stopping.")
                            break
                        failed_albums = 0

                    if opened:
                        opened_since_restart += 1
                        more_left = i + 1 < len(albums)
                        if (self.browser_restart_interval > 0 and more_left
                                and opened_since_restart >= self.browser_restart_interval):
                            opened_since_restart = 0
                            if not self.restart_browser():
                                print("Browser restart failed, attempting to continue...")
                                if not self.login():
                                    print("Could not recover session. Stopping.")
                                    break
                        elif more_left:
                            self.album_delay()

                self.clear_state()

                print("\n" + "=" * 60)
                print(f"Finished! Downloaded {total_downloaded} images total.")
                print("=" * 60)
            finally:
                self.stop_browser()
                print(self.profiler.summary())


def main():
    # Acquire exclusive lock to prevent multiple instances
    lock_file = Path(__file__).parent / ".sgspider.lock"
    lock_fp = open(lock_file, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Error: Another instance of SGSpider is already running.")
        print(f"If this is incorrect, delete the lock file: {lock_file}")
        sys.exit(1)

    def release_lock():
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()
        try:
            lock_file.unlink()
        except OSError:
            pass

    atexit.register(release_lock)

    spider = SGSpider()
    args = sys.argv[1:]
    if args and args[0] == "--check-login":
        # Smoke check: start the browser, log in, and exit (no downloads).
        sys.exit(0 if spider.check_login() else 1)
    if args and args[0] == "--bench-downloads":
        # --bench-downloads <album_url> [levels, e.g. 4,8,16] [scratch dir]
        if len(args) < 2:
            print("usage: sgspider.py --bench-downloads <album_url> [4,8,16] [scratch_dir]")
            sys.exit(2)
        levels = [int(x) for x in (args[2] if len(args) > 2 else "4,8,12,16").split(",")]
        dest = Path(args[3] if len(args) > 3 else "/tmp/sgspider-bench")
        sys.exit(0 if spider.bench_downloads(args[1], levels, dest) else 1)
    # If album URLs provided as arguments, use them; otherwise collect from feed
    spider.run(args or None)


if __name__ == "__main__":
    main()
