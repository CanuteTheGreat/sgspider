#!/usr/bin/env python3
"""
SGSpider - A web scraper for SuicideGirls using Playwright.
Uses a single browser instance for all requests to maintain consistent fingerprinting.
"""

import sys
import time
import re
import random
import gc
import fcntl
import atexit
import configparser
import platform
import hashlib
import base64
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Default configuration values (can be overridden by config file)
DEFAULT_HEADLESS = True
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 5
DEFAULT_DOWNLOAD_TIMEOUT = 30000  # 30 seconds for image downloads
DEFAULT_PAGE_LOAD_TIMEOUT = 60000  # 60 seconds for page loads
DEFAULT_MAX_ALBUM_PAGES = 0  # Limit album loading iterations (0 = unlimited)
DEFAULT_BROWSER_RESTART_INTERVAL = 50  # Restart browser every N albums to manage memory


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

        # Settings loaded from config (with defaults)
        self.headless = DEFAULT_HEADLESS
        self.max_retries = DEFAULT_MAX_RETRIES
        self.retry_base_delay = DEFAULT_RETRY_BASE_DELAY
        self.download_timeout = DEFAULT_DOWNLOAD_TIMEOUT
        self.page_load_timeout = DEFAULT_PAGE_LOAD_TIMEOUT
        self.max_album_pages = DEFAULT_MAX_ALBUM_PAGES
        self.browser_restart_interval = DEFAULT_BROWSER_RESTART_INTERVAL

        # Playwright instance reference (needed for browser restarts)
        self.playwright = None

    def load_credentials(self) -> dict:
        """Load credentials and settings from config file."""
        print("Reading configuration...")
        config = configparser.ConfigParser(interpolation=None)
        config.read("sgspider.ini")
        self.credentials = config

        # Load settings if present
        if config.has_section("settings"):
            settings = config["settings"]
            self.headless = settings.getboolean("headless", self.headless)
            self.max_retries = settings.getint("max_retries", self.max_retries)
            self.retry_base_delay = settings.getint("retry_base_delay", self.retry_base_delay)
            self.download_timeout = settings.getint("download_timeout", self.download_timeout)
            self.page_load_timeout = settings.getint("page_load_timeout", self.page_load_timeout)
            self.max_album_pages = settings.getint("max_album_pages", self.max_album_pages)
            self.browser_restart_interval = settings.getint("browser_restart_interval", self.browser_restart_interval)

        print("Configuration loaded.")
        return config

    def random_delay(self, min_sec: float = 1.0, max_sec: float = 3.0):
        """Sleep for a random duration to appear more human-like."""
        time.sleep(random.uniform(min_sec, max_sec))

    def human_type(self, element, text: str):
        """Type text with human-like delays."""
        for char in text:
            element.type(char, delay=random.uniform(100, 400))
            if random.random() < 0.1:
                time.sleep(random.uniform(0.5, 1.5))

    def human_click(self, element):
        """Click an element with human-like behavior."""
        try:
            bbox = element.bounding_box()
            if bbox:
                offset_x = random.uniform(0.2, 0.8) * bbox["width"]
                offset_y = random.uniform(0.2, 0.8) * bbox["height"]
                element.click(position={"x": offset_x, "y": offset_y}, delay=random.uniform(100, 300))
            else:
                element.click(delay=random.uniform(100, 300))
        except Exception:
            element.click()

    def random_mouse_movement(self):
        """Simulate random mouse movements on the page."""
        try:
            viewport = self.page.viewport_size
            if viewport:
                for _ in range(random.randint(2, 4)):
                    x = random.randint(100, viewport["width"] - 100)
                    y = random.randint(100, viewport["height"] - 100)
                    self.page.mouse.move(x, y)
                    time.sleep(random.uniform(0.2, 0.5))
        except Exception:
            pass

    def retry_operation(self, operation, description: str, max_retries: int = None):
        """
        Execute an operation with exponential backoff retry logic.

        Args:
            operation: Callable to execute
            description: Human-readable description for logging
            max_retries: Maximum number of retry attempts (defaults to self.max_retries)

        Returns:
            Result of the operation, or None if all retries failed
        """
        if max_retries is None:
            max_retries = self.max_retries

        last_error = None

        for attempt in range(max_retries):
            try:
                return operation()
            except Exception as e:
                last_error = e
                delay = self.retry_base_delay * (2 ** attempt) + random.uniform(0, 2)

                if attempt < max_retries - 1:
                    print(f"  Attempt {attempt + 1}/{max_retries} failed for {description}: {e}")
                    print(f"  Retrying in {delay:.1f} seconds...")
                    time.sleep(delay)
                else:
                    print(f"  All {max_retries} attempts failed for {description}: {last_error}")

        return None

    def start_browser(self, playwright):
        """Initialize the browser with anti-detection settings."""
        self.playwright = playwright
        print(f"System architecture: {platform.machine()}")
        print("Launching Playwright Chromium browser...")

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

        # Create context with realistic settings
        self.context = self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # Add anti-detection scripts
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

        self.page = self.context.new_page()
        self.page.set_default_timeout(self.page_load_timeout)

        # Handle popup windows - close any unwanted new tabs/popups
        def handle_popup(popup):
            popup_url = popup.url
            # Only allow pages from suicidegirls.com, close all others
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
        """Restart browser to free memory. Preserves login state by re-authenticating.

        Returns:
            True if restart and re-login successful, False otherwise
        """
        print("\n=== Restarting browser to free memory ===")

        # Stop current browser
        self.stop_browser()
        self.browser = None
        self.context = None
        self.page = None

        # Force garbage collection
        gc.collect()

        # Start fresh browser
        if self.playwright:
            self.start_browser(self.playwright)

            # Re-login
            if self.login():
                print("Browser restarted and re-logged in successfully.")
                return True
            else:
                print("Browser restarted but re-login failed!")
                return False
        else:
            print("Error: No playwright instance available for restart.")
            return False

    def capture_placeholder_hash(self, sample_image_url: str) -> bool:
        """
        Download an image WITHOUT authentication to capture the placeholder image hash.
        This hash will be used to detect when we're getting placeholder images instead of real content.

        Args:
            sample_image_url: URL of an image to download without auth

        Returns:
            True if placeholder hash was captured successfully
        """
        print("\n=== Capturing Placeholder Image Hash ===")
        print(f"Downloading image without authentication: {sample_image_url}")

        try:
            # Create a fresh context WITHOUT cookies (unauthenticated)
            fresh_context = self.browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

            try:
                # Download image without authentication
                response = fresh_context.request.get(sample_image_url, timeout=self.download_timeout)

                if response.status != 200:
                    print(f"  Failed to download: HTTP {response.status}")
                    return False

                body = response.body()
                self.placeholder_hash = hashlib.sha256(body).hexdigest()

                print(f"  Placeholder image size: {len(body)} bytes")
                print(f"  Placeholder hash: {self.placeholder_hash}")
                print("  This hash will be used to detect authentication failures.")

                return True

            finally:
                fresh_context.close()

        except Exception as e:
            print(f"  Error capturing placeholder hash: {e}")
            return False

    def is_placeholder_image(self, data: bytes) -> bool:
        """
        Check if the downloaded data matches the placeholder image.

        Args:
            data: Image data to check

        Returns:
            True if this is a placeholder image (authentication failure)
        """
        if not self.placeholder_hash:
            return False

        image_hash = hashlib.sha256(data).hexdigest()
        return image_hash == self.placeholder_hash

    def is_valid_existing_file(self, file_path: Path) -> bool:
        """
        Check if an existing file is valid (not corrupted or placeholder).

        Args:
            file_path: Path to the file to validate

        Returns:
            True if file is valid, False if it should be re-downloaded
        """
        try:
            if not file_path.exists():
                return False

            file_size = file_path.stat().st_size

            # Check minimum file size - use 10KB to catch truly broken/empty downloads
            # Note: Valid images can be as small as 15KB; placeholder detection handles auth failures
            if file_size < 10000:
                return False

            # Read file header to check if it's a valid image
            with open(file_path, "rb") as f:
                header = f.read(16)

                if len(header) < 4:
                    return False

                # Check for valid image format magic bytes
                is_valid_format = (
                    header[0:2] == b'\xff\xd8' or                    # JPEG
                    header[0:4] == b'\x89PNG' or                     # PNG
                    header[0:6] in (b'GIF87a', b'GIF89a') or         # GIF
                    (header[0:4] == b'RIFF' and header[8:12] == b'WEBP') or  # WebP
                    header[0:2] == b'BM'                             # BMP
                )

                if not is_valid_format:
                    return False

                # Check for placeholder by reading full file and hashing
                if self.placeholder_hash:
                    f.seek(0)
                    data = f.read()
                    file_hash = hashlib.sha256(data).hexdigest()
                    if file_hash == self.placeholder_hash:
                        return False

            return True

        except Exception:
            return False

    def accept_cookies(self):
        """Accept cookie consent if present."""
        try:
            cookie_btn = self.page.locator(
                "button:has-text('Accept'), button:has-text('Agree'), button:has-text('OK')"
            ).first
            if cookie_btn.is_visible(timeout=2000):
                print("Accepting cookies...")
                self.human_click(cookie_btn)
                self.random_delay(1, 2)
        except Exception:
            pass

    def is_logged_in(self) -> bool:
        """Check if we're currently logged in."""
        try:
            content = self.page.content().lower()
            url = self.page.url.lower()

            # Negative indicators
            if "login" in url or "join" in url:
                return False
            if "login" in content and "logout" not in content:
                return False

            # Positive indicators
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

    def login(self) -> bool:
        """
        Perform login to the site.

        Returns:
            True if login was successful, False otherwise
        """
        def attempt_login():
            print("Navigating to main page...")
            self.page.goto(self.base_url, wait_until="domcontentloaded")
            self.random_delay(10, 15)

            # Simulate reading the page
            self.page.evaluate("window.scrollTo(0, 200);")
            self.random_delay(2, 4)
            self.page.evaluate("window.scrollTo(0, 0);")
            self.random_delay(1, 2)

            self.accept_cookies()
            self.random_mouse_movement()
            self.page.keyboard.press("Escape")
            self.random_delay(1, 2)

            # Find and click login button
            login_selectors = [
                "#login",
                "a:has-text('Login')",
                "a:has-text('Log in')",
                "button:has-text('Login')",
                "button:has-text('Log in')",
                ".login-button",
                "[data-action='login']",
                "a[href*='login']",
            ]

            login_clicked = False
            for selector in login_selectors:
                try:
                    btn = self.page.locator(selector).first
                    if btn.is_visible(timeout=2000):
                        print(f"Found login button: {selector}")
                        self.human_click(btn)
                        login_clicked = True
                        self.random_delay(2, 4)

                        # Wait for login form
                        self.page.wait_for_selector("input[name='username']", timeout=5000)
                        print("Login form appeared.")
                        break
                except Exception:
                    continue

            if not login_clicked:
                raise Exception("Could not find login button")

            # Fill login form
            self.random_delay(1, 2)
            self.random_mouse_movement()

            user_field = self.page.locator("input[name='username']")
            pass_field = self.page.locator("input[name='password']")

            # Username
            print("Entering username...")
            user_field.hover()
            self.random_delay(0.3, 0.8)
            user_field.click()
            self.random_delay(0.3, 0.6)
            user_field.clear()
            self.human_type(user_field, self.credentials["main"]["username"])

            self.random_delay(0.5, 1.5)
            self.random_mouse_movement()

            # Password
            print("Entering password...")
            pass_field.hover()
            self.random_delay(0.3, 0.8)
            pass_field.click()
            self.random_delay(0.3, 0.6)
            pass_field.clear()
            self.human_type(pass_field, self.credentials["main"]["password"])

            self.random_delay(1, 2)
            self.random_mouse_movement()

            # Submit
            print("Submitting login form...")
            pass_field.press("Tab")
            self.random_delay(0.3, 0.8)
            pass_field.press("Enter")

            # Wait for login to complete
            self.random_delay(8, 12)

            # Verify login
            if not self.is_logged_in():
                raise Exception("Login verification failed")

            return True

        print("\n=== Logging In ===")
        result = self.retry_operation(attempt_login, "login")

        if result:
            print("Login successful!")
            return True
        else:
            print("Login failed after all retries.")
            return False

    def ensure_logged_in(self) -> bool:
        """Check login status and re-login if necessary."""
        if self.is_logged_in():
            return True

        print("Session expired, re-authenticating...")
        return self.login()

    def collect_album_urls(self) -> list:
        """
        Navigate to the photos page and collect all album URLs.

        Returns:
            List of album URLs
        """
        print("\n=== Collecting Album URLs ===")

        def load_albums_page():
            self.page.goto(f"{self.base_url}/photos/sg/recent/all/", wait_until="domcontentloaded")
            self.random_delay(3, 5)

            if "server error" in self.page.content().lower():
                raise Exception("Server error on photos page")

            return True

        result = self.retry_operation(load_albums_page, "load photos page")
        if not result:
            print("Failed to load photos page.")
            return []

        # Scroll to load more content
        if self.max_album_pages > 0:
            print(f"Scrolling to load albums (limited to {self.max_album_pages} iterations)...")
        else:
            print("Scrolling to load albums (this takes a while)...")
        print("Progress: [", end="", flush=True)

        consecutive_failures = 0
        max_failures = 5
        pages_loaded = 0

        limit_reached = False
        while consecutive_failures < max_failures:
            # Check page limit
            if self.max_album_pages > 0 and pages_loaded >= self.max_album_pages:
                limit_reached = True
                break
            pages_loaded += 1
            loaded_more = False

            try:
                # Try load-more button first
                load_more = self.page.locator("#load-more").first
                if load_more.is_visible(timeout=1000) and load_more.is_enabled():
                    self.human_click(load_more)
                    print(".", end="", flush=True)
                    loaded_more = True
                    consecutive_failures = 0
                    self.random_delay(2, 4)
            except Exception:
                pass

            if not loaded_more:
                try:
                    # Try infinite scroll
                    current_height = self.page.evaluate("document.body.scrollHeight")
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    self.random_delay(2, 3)
                    new_height = self.page.evaluate("document.body.scrollHeight")

                    if new_height > current_height:
                        print("s", end="", flush=True)
                        consecutive_failures = 0
                    else:
                        print("x", end="", flush=True)
                        consecutive_failures += 1
                except Exception:
                    print("x", end="", flush=True)
                    consecutive_failures += 1

            self.random_delay(1, 2)

        if limit_reached:
            print(f"] ({pages_loaded} iterations - LIMIT REACHED)")
        else:
            print(f"] ({pages_loaded} iterations)")

        # Extract album URLs using JavaScript for speed (much faster than iterating locators)
        print("Extracting album URLs...", flush=True)

        sharing_patterns = [
            "twitter.com", "mailto:", "facebook.com", "pinterest.com",
            "reddit.com", "tumblr.com", "instagram.com", "/share?",
            "?&body=", "share=", "intent/tweet"
        ]

        # Use JavaScript to extract all hrefs at once - much faster than iterating locators
        all_hrefs = self.page.evaluate("""
            () => {
                const links = document.querySelectorAll('a[href]');
                return Array.from(links).map(a => a.href);
            }
        """)

        print(f"Found {len(all_hrefs)} total links, filtering...", flush=True)

        album_urls = set()
        for href in all_hrefs:
            if not href:
                continue

            # Must be an album URL
            if "/girls/" not in href or "/album/" not in href:
                continue

            # Skip sharing links
            if any(pattern in href.lower() for pattern in sharing_patterns):
                continue

            # Must be on suicidegirls.com
            if "suicidegirls.com" not in href:
                continue

            # Normalize URL
            href = href.replace("http://", "https://")
            album_urls.add(href)

        album_list = list(album_urls)
        print(f"Found {len(album_list)} unique albums.", flush=True)

        return album_list

    def parse_album_url(self, url: str) -> tuple:
        """
        Parse an album URL to extract girl name and album name.

        Args:
            url: The album URL

        Returns:
            Tuple of (girl_name, album_name)
        """
        try:
            parsed = urlparse(url)
            parts = [p for p in parsed.path.split("/") if p]

            # Expected: ['girls', 'USERNAME', 'album', 'ID', 'ALBUM_NAME']
            if len(parts) >= 5 and parts[0] == "girls" and parts[2] == "album":
                return parts[1], parts[4]

            # Fallback parsing
            if "/girls/" in url and "/album/" in url:
                girl = url.split("/girls/")[1].split("/")[0]
                album_part = url.split("/album/")[1]
                album_parts = album_part.split("/")
                album = album_parts[1] if len(album_parts) >= 2 else "unknown"
                return girl, album
        except Exception:
            pass

        return "unknown", "unknown"

    def extract_image_urls(self, album_url: str) -> list:
        """
        Navigate to an album page and extract all image URLs.

        Args:
            album_url: URL of the album page

        Returns:
            List of image URLs
        """
        def load_and_extract():
            self.page.goto(album_url, wait_until="domcontentloaded")
            self.random_delay(2, 4)

            # Check for auth issues
            current_url = self.page.url.lower()
            if "join" in current_url or "login" in current_url:
                raise Exception("Redirected to login page")

            image_urls = []

            # Method 1: Photo containers with CDN links
            # IMPORTANT: Keep full URL including query params - they may contain auth tokens
            containers = self.page.locator(
                "li.photo-container a[href*='cloudfront'], li.photo-container a[href*='amazonaws']"
            ).all()

            for container in containers:
                try:
                    href = container.get_attribute("href", timeout=2000)
                    if href:
                        # Keep full URL with query params for auth tokens
                        if href not in image_urls:
                            image_urls.append(href)
                except Exception:
                    continue

            # Method 2: Fallback - any CDN links that look like images
            if not image_urls:
                all_links = self.page.locator("a[href]").all()
                for link in all_links:
                    try:
                        href = link.get_attribute("href", timeout=2000)
                        if not href:
                            continue

                        if "cloudfront" not in href and "amazonaws" not in href:
                            continue

                        # Check for image extension in the base path (before query params)
                        base_href = href.split("?")[0]
                        if any(ext in base_href.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
                            # Keep full URL with query params
                            if href not in image_urls:
                                image_urls.append(href)
                    except Exception:
                        continue

            return image_urls

        result = self.retry_operation(load_and_extract, f"extract images from {album_url}")
        return result if result else []

    def download_image_via_navigation(self, url: str, save_path: Path) -> tuple:
        """
        Download an image using the browser context's HTTP client.
        Uses context.request.get() to avoid "Download is starting" errors from page.goto().

        Args:
            url: URL of the image to download
            save_path: Path where the image should be saved

        Returns:
            Tuple of (success: bool, is_placeholder: bool)
        """
        def do_download():
            # Use context.request.get() instead of page.goto() to avoid download triggers
            # This makes an HTTP request using the browser's cookies without navigation
            response = self.context.request.get(url, timeout=self.download_timeout)

            try:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}")

                body = response.body()

                # Check if this is the placeholder image (auth failure)
                if self.is_placeholder_image(body):
                    return (False, True)  # Got placeholder - auth issue

                if len(body) < 1000:
                    raise Exception("Response too small, likely an error page")

                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(body)

                return (True, False)  # Success
            finally:
                # Dispose response to free inspector cache memory
                # This prevents "Request content was evicted from inspector cache" errors
                response.dispose()

        result = self.retry_operation(do_download, f"download {save_path.name}")
        if result is None:
            return (False, False)
        return result

    def process_album(self, album_url: str) -> tuple:
        """
        Process a single album: extract and download all images.

        Args:
            album_url: URL of the album

        Returns:
            Tuple of (downloaded_count: int, auth_failure: bool)
        """
        girl_name, album_name = self.parse_album_url(album_url)
        album_dir = self.download_dir / girl_name / album_name

        print(f"\n  Album: {girl_name}/{album_name}")

        # extract_image_urls navigates to album page, which handles auth check
        image_urls = self.extract_image_urls(album_url)

        if not image_urls:
            print("  No images found in album.")
            return (0, False)

        print(f"  Found {len(image_urls)} images")

        downloaded = 0
        skipped = 0
        auth_failures = 0

        for img_url in image_urls:
            # Extract filename from URL
            filename = img_url.split("/")[-1].split("?")[0]
            if not filename:
                filename = f"image_{downloaded + 1}.jpg"

            # Sanitize filename
            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
            save_path = album_dir / filename

            # Check if file exists and is valid
            if save_path.exists():
                if self.is_valid_existing_file(save_path):
                    skipped += 1
                    continue
                else:
                    # File is corrupted/placeholder - delete and re-download
                    print(f"    Replacing corrupted: {filename}")
                    save_path.unlink()

            success, is_placeholder = self.download_image_via_navigation(img_url, save_path)

            if success:
                downloaded += 1
                print(f"    Downloaded: {filename}")
                auth_failures = 0  # Reset on success
            elif is_placeholder:
                auth_failures += 1
                print(f"    AUTH FAILURE: {filename} (got placeholder image)")

                # If we get multiple placeholder images, session is dead
                if auth_failures >= 2:
                    print("  Multiple placeholder images detected - session expired!")
                    return (downloaded, True)
            else:
                print(f"    Failed: {filename}")

            # Small delay between downloads
            self.random_delay(0.5, 1.5)

        if skipped:
            print(f"  Skipped {skipped} existing files")
        print(f"  Downloaded {downloaded} new images")

        return (downloaded, False)

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

        with sync_playwright() as playwright:
            try:
                self.start_browser(playwright)

                if not self.login():
                    print("Failed to log in. Exiting.")
                    return

                if album_urls:
                    albums = album_urls
                    print(f"\n=== Processing {len(albums)} Specified Album(s) ===")
                else:
                    albums = self.collect_album_urls()

                if not albums:
                    print("No albums found. Exiting.")
                    return

                # Get a sample image URL to capture the placeholder hash
                print("\n=== Getting Sample Image for Placeholder Detection ===")
                first_album = albums[0]
                sample_images = self.extract_image_urls(first_album)

                if sample_images:
                    # Strip query params to get unauthenticated version for placeholder hash
                    sample_url = sample_images[0].split("?")[0]
                    self.capture_placeholder_hash(sample_url)
                else:
                    print("Warning: Could not get sample image for placeholder detection.")
                    print("Placeholder detection will be disabled.")

                print(f"\n=== Processing {len(albums)} Albums ===")

                total_downloaded = 0
                failed_albums = 0

                for i, album_url in enumerate(albums, 1):
                    print(f"\n[{i}/{len(albums)}] {album_url}")

                    try:
                        count, auth_failure = self.process_album(album_url)
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

                    # If too many consecutive failures, try to recover
                    if failed_albums >= 3:
                        print("\nToo many consecutive failures, attempting recovery...")
                        if not self.login():
                            print("Session lost and could not recover. Stopping.")
                            break
                        failed_albums = 0

                    # Periodic browser restart to manage memory
                    if self.browser_restart_interval > 0 and i % self.browser_restart_interval == 0 and i < len(albums):
                        if not self.restart_browser():
                            print("Browser restart failed, attempting to continue...")
                            if not self.login():
                                print("Could not recover session. Stopping.")
                                break

                print("\n" + "=" * 60)
                print(f"Finished! Downloaded {total_downloaded} images total.")
                print("=" * 60)

            finally:
                self.stop_browser()


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

    # Keep lock file open and register cleanup
    def release_lock():
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()
        try:
            lock_file.unlink()
        except OSError:
            pass

    atexit.register(release_lock)

    spider = SGSpider()
    # If album URLs provided as arguments, use them; otherwise collect from feed
    album_urls = sys.argv[1:] if len(sys.argv) > 1 else None
    spider.run(album_urls)


if __name__ == "__main__":
    main()
