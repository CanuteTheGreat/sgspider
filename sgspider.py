#!/usr/bin/env python3
import shutil
import requests
import time
import re
import random
import os
import configparser
import platform
from playwright.sync_api import sync_playwright

print(f"System architecture: {platform.machine()}")
print("Using Playwright with Chromium...")

# Configuration options
HEADLESS_MODE = True  # Set to True to run headless (no browser window)

def random_delay(min_delay=1, max_delay=3):
    time.sleep(random.uniform(min_delay, max_delay))

def human_type(element, text):
    """More realistic human typing with Playwright"""
    for char in text:
        element.type(char, delay=random.uniform(100, 400))  # Slower, more variable typing
        # Occasionally pause as if thinking
        if random.random() < 0.1:  # 10% chance
            time.sleep(random.uniform(0.5, 1.5))

def human_click(element):
    """Human-like clicking with Playwright"""
    try:
        # Get element bounding box
        bbox = element.bounding_box()
        if bbox:
            # Calculate random offset within element
            offset_x = random.uniform(0.2, 0.8) * bbox['width']
            offset_y = random.uniform(0.2, 0.8) * bbox['height']
            
            # Click with offset and delay
            element.click(
                position={'x': offset_x, 'y': offset_y},
                delay=random.uniform(100, 300)
            )
        else:
            element.click(delay=random.uniform(100, 300))
    except Exception as e:
        # Fallback to simple click
        try:
            element.click()
        except:
            pass

def random_mouse_movement(page):
    """Simulate random mouse movements"""
    try:
        viewport_size = page.viewport_size
        if viewport_size:
            # Make small random movements
            movements = random.randint(2, 4)
            for _ in range(movements):
                x = random.randint(100, viewport_size['width'] - 100)
                y = random.randint(100, viewport_size['height'] - 100)
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.5))
    except Exception as e:
        # Mouse movement isn't critical, continue
        pass

def getcreds():
    print("Reading configuration.")
    configuration = configparser.ConfigParser(interpolation=None)
    configuration.read('sgspider.ini')
    print("Finished reading configuration.")
    return configuration

def verify_login(page, credentials):
    """Verify that login was actually successful"""
    print("Verifying login status...")
    
    try:
        page_content = page.content().lower()
        
        # Check for login indicators
        if "login" in page_content and "logout" not in page_content:
            print("❌ Still seeing login button - login failed")
            return False
            
        # Look for username or profile indicators
        username = credentials['main']['username'].lower()
        if username in page_content:
            print(f"✓ Username '{username}' found on page - login appears successful")
            return True
            
        # Check for logout link
        if "logout" in page_content:
            print("✓ Logout link found - login appears successful")
            return True
            
        # Check current URL
        current_url = page.url.lower()
        if "login" in current_url:
            print("❌ Still on login page - login failed")
            return False
            
        # Last resort - check page title
        title = page.title().lower()
        if "login" in title:
            print("❌ Page title contains 'login' - login may have failed")
            return False
            
        print("⚠️ Login status unclear - assuming login failed")
        return False
        
    except Exception as e:
        print(f"Error verifying login: {e}")
        return False

def login(page, credentials, retry_count=0):
    if retry_count >= 2:
        print("Maximum login retries reached, aborting")
        return False
        
    print("Loading front page and initiating login")
    
    # Always load main page since login is a popup
    print(f"Loading main page (attempt {retry_count + 1})")
    page.goto("https://suicidegirls.com")
        
    print("Waiting for page to fully load...")
    random_delay(15, 25)  # Much longer initial delay for better detection avoidance
    
    # Add some realistic browsing behavior before login
    try:
        # Simulate reading the page
        page.evaluate("window.scrollTo(0, 200);")
        random_delay(2, 4)
        page.evaluate("window.scrollTo(0, 0);")
        random_delay(1, 3)
    except:
        pass
    
    # Handle cookie consent if present
    try:
        cookie_accept = page.locator("button:has-text('Accept'), button:has-text('Agree'), button:has-text('OK')").first
        if cookie_accept.is_visible():
            print("Accepting cookies...")
            human_click(cookie_accept)
            random_delay(2, 3)
    except:
        pass
    
    # Check for server error
    page_content_lower = page.content().lower()
    if "server error" in page_content_lower or "error" in page.title().lower():
        print("Server error detected on main page, waiting longer and retrying...")
        random_delay(15, 25)  # Much longer wait
        page.reload()
        random_delay(8, 12)
        
        # Try alternative approach - go directly to login page
        if "server error" in page.content().lower():
            print("Still getting server error, trying direct login URL...")
            page.goto("https://suicidegirls.com/login/")
            random_delay(5, 8)
    
    # Add some random mouse movement first
    random_mouse_movement(page)
    
    page.keyboard.press("Escape")
    random_delay(1, 2)
    
    # Try to find and click login button to open popup/modal
    login_clicked = False
    login_selectors = [
        "#login",
        "a:has-text('Login')",
        "a:has-text('Log in')",
        "button:has-text('Login')",
        "button:has-text('Log in')",
        ".login-button",
        "[data-action='login']",
        "a[href*='login']",
        "*[class*='login']"
    ]
    
    for selector in login_selectors:
        try:
            login_btn = page.locator(selector).first
            if login_btn.is_visible():
                print(f"Found login button with selector: {selector}")
                human_click(login_btn)
                login_clicked = True
                
                # Wait for popup/modal to appear
                random_delay(2, 4)
                print("Waiting for login modal/popup to appear...")
                
                # Check if a modal appeared or username field is available
                try:
                    page.wait_for_selector("input[name='username']", timeout=5000)
                    print("Login form is now available")
                    break
                except:
                    print("No login modal detected, trying next selector...")
                    login_clicked = False
        except Exception as e:
            continue
    
    if not login_clicked:
        print("Unable to access login, aborting")
        return False
        
    random_delay(2, 4)
    
    # Find username and password fields
    try:
        user = page.locator("input[name='username']")
        password = page.locator("input[name='password']")
        
        # Add more realistic pre-typing behavior
        print("Simulating human reading/thinking behavior...")
        
        # Simulate reading the form first - move mouse around form elements
        try:
            form_elements = page.locator("input, button, label").all()
            if len(form_elements) >= 2:
                # Move to a few different form elements as if reading
                for i in range(min(3, len(form_elements))):
                    try:
                        page.mouse.move(*form_elements[i].bounding_box().values())
                        time.sleep(random.uniform(0.5, 1.2))
                    except:
                        pass
        except:
            pass
        
        random_delay(1, 2.5)  # Pause as if thinking
        
        # More natural field interaction - hover before clicking
        print("Filling username field...")
        try:
            user.hover()
            random_delay(0.5, 1.2)
        except:
            pass
        
        user.click()  # Use simple click instead of human_click for more reliability
        random_delay(0.5, 1.0)
        user.clear()
        random_delay(0.3, 0.8)
        
        # Type username with more realistic pauses
        human_type(user, credentials['main']['username'])
        
        # Simulate brief pause between fields (like thinking/reading)
        random_delay(0.8, 2.0)
        
        # Add small mouse movement between fields
        random_mouse_movement(page)
        
        print("Filling password field...")
        # Hover over password field before clicking
        try:
            password.hover()
            random_delay(0.5, 1.0)
        except:
            pass
            
        password.click()  # Use simple click for more reliability
        random_delay(0.5, 1.0)
        password.clear()
        random_delay(0.3, 0.8)
        human_type(password, credentials['main']['password'])
        
        # Realistic pause after finishing typing (like reviewing what was typed)
        random_delay(1.2, 2.8)
        
        # Add some mouse movement as if considering whether to submit
        print("Simulating consideration before submitting...")
        random_mouse_movement(page)
        random_delay(0.8, 1.5)
        
        # Capture current URL before submission to detect redirect
        pre_submit_url = page.url
        print(f"Pre-submit URL: {pre_submit_url}")
        
        # Submit form naturally - try Tab then Enter (very human-like)
        password.press("Tab")
        random_delay(0.5, 1.0)
        password.press("Enter")
        print("Login form submitted via Tab+Enter, waiting for response...")
        
    except Exception as e:
        print(f"Error interacting with login form: {e}")
        return False
    
    # Wait for login modal to close and page content to update
    print("Waiting for login modal to close and page to update...")
    random_delay(8, 12)  # Much longer wait for server processing
    
    # Check if login modal/popup has disappeared (indicating successful login)
    modal_closed = False
    for i in range(10):
        try:
            # Look for login modal elements
            modals = page.locator("div[class*='modal'], div[class*='popup'], div[class*='dialog']").all()
            visible_modals = [m for m in modals if m.is_visible()]
            
            if len(visible_modals) == 0:
                print("Login modal appears to have closed")
                modal_closed = True
                break
            else:
                print(f"Login modal still visible, waiting... (attempt {i+1})")
                random_delay(1, 2)
        except:
            # If we can't find modal elements, assume it closed
            modal_closed = True
            break
    
    if not modal_closed:
        print("Login modal may still be open - continuing anyway")
    
    # Additional wait for any dynamic content to load
    print("Waiting for page content to fully load...")
    random_delay(5, 8)
    
    print("Checking for errors...")
    
    # Enhanced server error handling
    page_content = page.content().lower()
    error_checks = 0
    while "server error" in page_content and error_checks < 3:
        print(f"Server error detected (check {error_checks + 1}/3), waiting longer...")
        random_delay(15, 25)  # Very long wait
        
        # Try refreshing page on second check
        if error_checks == 1:
            print("Refreshing page...")
            page.reload()
            random_delay(8, 12)
        
        page_content = page.content().lower()
        error_checks += 1
        
    if "server error" in page_content:
        print("Server errors persist after multiple attempts...")
        if retry_count < 1:
            print("Attempting full retry...")
            return login(page, credentials, retry_count + 1)
        else:
            print("Still attempting to verify login despite server errors...")
    
    print("Login completed successfully")
    random_delay(2, 3)
    
    # Verify login was actually successful
    return verify_login(page, credentials)

def getgirls(page):
    print("Loading photos page.")
    page.goto("https://www.suicidegirls.com/photos/sg/recent/all/")
    print("Finished loading photos page.")
    print(f"Current page title: {page.title()}")
    print(f"Current URL: {page.url}")
    
    # Check for server error on photos page
    if "server error" in page.content().lower():
        print("Server error on photos page! Waiting and retrying...")
        random_delay(10, 15)
        page.reload()
        random_delay(5, 8)
    
    random_delay(3, 5)  # Give more time for page to load
    
    # Debug: Look for any load-more type elements
    print("Debugging: Looking for load-more elements...")
    try:
        # Check various common selectors
        selectors_to_try = [
            "#load-more",
            "button#load-more",
            "*[class*='load-more']",
            "*:has-text('Load More')",
            "*:has-text('Show More')",
            "*[class*='more']"
        ]
        
        found_elements = []
        for selector in selectors_to_try:
            try:
                elements = page.locator(selector).all()
                for elem in elements:
                    if elem.is_visible():
                        text = elem.text_content()[:50] if elem.text_content() else ""
                        class_attr = elem.get_attribute('class') or ""
                        found_elements.append((selector, text, class_attr))
            except:
                pass
        
        if found_elements:
            print(f"Found {len(found_elements)} potential load-more elements:")
            for i, (selector, text, class_attr) in enumerate(found_elements):
                print(f"  {i+1}. Selector: {selector}")
                print(f"     Text: '{text}'")
                print(f"     Class: '{class_attr}'")
        else:
            print("No load-more elements found with common selectors")
            
        # Check page structure
        print("Checking page structure...")
        body_height = page.evaluate("document.body.scrollHeight")
        print(f"Page scroll height: {body_height}")
        
        # Check how many albums are already loaded on the page
        album_links = page.locator("a[href*='album']").all()
        print(f"Initial album links found: {len(album_links)}")
        
        if len(album_links) > 20:
            print(f"Found {len(album_links)} albums already loaded - might be enough to start with")
        
    except Exception as e:
        print(f"Debug error: {e}")
    
    # Load more pages for comprehensive album collection
    print("Starting to scroll through photos page.. this will take a *REALLY* LONG time!")
    print("Progress indicators: '.' = load-more button clicked, 's' = infinite scroll success, 'b' = button not clickable, 'i' = initial loading, 'x' = failure")
    print("Progress [", end='', flush=True)
    done = False
    cctr = 0
    pagectr = 0
    
    while not done:
        pagectr = pagectr + 1
        try:
            load_more = page.locator("#load-more").first
            if load_more.is_visible() and load_more.is_enabled():
                human_click(load_more)
                print('.', end='', flush=True)
                cctr = 0
                random_delay(2, 4)
            else:
                print('b', end='', flush=True)
                cctr = cctr + 1
        except Exception as e:
            try:
                load_more = page.locator("*:has-text('Load More'), *:has-text('Show More')").first
                if load_more.is_visible():
                    human_click(load_more)
                    print('.', end='', flush=True)
                    cctr = 0
                    random_delay(2, 4)
                else:
                    raise Exception("No load more button found")
            except:
                try:
                    current_height = page.evaluate("document.body.scrollHeight")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    random_delay(2, 3)
                    page.evaluate("window.scrollBy(0, 1000);")
                    random_delay(2, 3)
                    page.evaluate("window.scrollBy(0, -100);")
                    random_delay(1, 2)
                    page.evaluate("window.scrollBy(0, 200);")
                    random_delay(2, 4)
                    new_height = page.evaluate("document.body.scrollHeight")
                    
                    if new_height > current_height:
                        print('s', end='', flush=True)
                        cctr = 0
                    else:
                        if pagectr <= 3:
                            print('i', end='', flush=True)
                            cctr = cctr + 1
                        else:
                            print('x', end='', flush=True)
                            cctr = cctr + 1
                        random_delay(1, 2)
                except Exception as e:
                    print('x', end='', flush=True)
                    cctr = cctr + 1
                    random_delay(1, 2)
        
        max_failures = 5 if pagectr <= 5 else 3
        if cctr >= max_failures:
            done = True
    print("]\n")
    print("Total pages loaded: " + str(pagectr))

    print("Collecting the URLs for each album. This will take a LONG time!")

    # Get all links on the page
    all_page_links = page.locator("a[href]").all()
    print(f"Total links on page: {len(all_page_links)}")
    
    # Get all links that point to actual album pages
    # These might be relative URLs like /girls/username/album/id/name/
    # Filter out sharing links (twitter, mailto, etc.)
    print("Filtering links for album URLs...")
    print("Progress: [", end='', flush=True)
    
    album_links = []
    total_to_check = len(all_page_links)
    progress_interval = max(1, total_to_check // 50)  # Show max 50 dots
    
    for idx, link in enumerate(all_page_links):
        href = link.get_attribute("href")
        if href:
            # Check for album pattern in the URL, but exclude sharing links
            if "/girls/" in href and "/album/" in href:
                # Exclude sharing links
                if not any(sharing in href.lower() for sharing in ['twitter.com', 'mailto:', 'facebook.com', 'pinterest.com']):
                    album_links.append(link)
        
        # Show progress
        if (idx + 1) % progress_interval == 0 or idx == total_to_check - 1:
            print('.', end='', flush=True)
    
    print("]")
    
    total_links = len(album_links)
    print(f"\nFound {total_links} album links to process (from {len(all_page_links)} total links)")
    
    # If we found way too many links, something went wrong
    if len(all_page_links) > 10000:
        print("WARNING: Found over 10,000 total links on page - this suggests too many pages were loaded!")
        print("Consider reducing the number of pages loaded or checking the page loading logic.")
    
    girls = []
    
    if total_links > 0:
        processed = 0
        rejected_samples = []  # Keep track of rejected URLs for debugging
        
        print("Deduplicating album URLs...")
        print("Progress: [", end='', flush=True)
        progress_interval = max(1, total_links // 50)  # Show max 50 progress dots
        
        for i, link in enumerate(album_links):
            href = link.get_attribute("href")
            
            # Make URL absolute if it's relative
            if href and href.startswith('/'):
                href = f"https://www.suicidegirls.com{href}"
            
            # Normalize URL (remove protocol variations for deduplication)
            if href:
                # Extract the path portion to deduplicate http vs https
                normalized = href.replace('http://', 'https://')
                
                # Add to girls list only if unique
                if normalized not in girls:
                    girls.append(normalized)
            
            processed += 1
            
            # Show progress dots
            if processed % progress_interval == 0 or processed == total_links:
                print('.', end='', flush=True)
                
            # Show percentage every 10%
            if processed % max(1, total_links // 10) == 0 or processed == total_links:
                percentage = (processed * 100) // total_links
                print(f' {percentage}%', end='', flush=True)
        
        print("]\n")
    else:
        print("No album links found. The page might use a different structure.")
    
    if rejected_samples:
        print("Sample rejected URLs:")
        for url in rejected_samples:
            print(f"  {url}")
    
    print(f"Album URLs found: {len(girls)}")
    if len(girls) > 0:
        print("All album URLs:")
        for i, url in enumerate(girls):  # Show all URLs
            print(f"  {i+1}: {url}")
    
    return girls

def getimgs(page, girls, credentials):
    print("collecting the URLs for the images. This will take a LONG time.")
    total_images_found = 0
    consecutive_auth_failures = 0
    max_consecutive_failures = 3  # Stop if we get 3 consecutive auth failures
    
    # Process all albums
    for i, girl in enumerate(girls):
        print(f"\nProcessing album {i+1}/{len(girls)}: {girl}")
        try:
            page.goto(girl)
            random_delay(2, 4)  # Give page time to load
            
            # Check if we're being redirected to login/join pages
            current_url = page.url.lower()
            page_content = page.content().lower()
            
            if "join" in current_url or "login" in current_url:
                print("❌ Redirected to join/login page for this album")
                print("🔄 Attempting re-authentication...")
                
                # Attempt full re-login
                login_success = login(page, credentials)
                if login_success:
                    print("✓ Re-authentication successful, retrying album...")
                    consecutive_auth_failures = 0  # Reset failure counter
                    page.goto(girl)
                    random_delay(2, 3)
                    current_url = page.url.lower()
                    
                    # Check if still redirected after re-login
                    if "join" in current_url or "login" in current_url:
                        print("❌ Still redirected after re-login - this album may be premium/unavailable")
                        continue
                    else:
                        print("✓ Successfully accessed album after re-login")
                        # Continue with normal processing below
                else:
                    print("❌ Re-authentication failed - skipping this album")
                    consecutive_auth_failures += 1
                    if consecutive_auth_failures >= max_consecutive_failures:
                        print(f"❌ Too many consecutive authentication failures ({consecutive_auth_failures}) - stopping")
                        break
                    continue
                
            if "/join/" in page_content and "logout" not in page_content:
                print("❌ Page contains join links - album may require authentication")
                print("🔄 Attempting full re-authentication...")
                
                # Attempt full re-login directly
                login_success = login(page, credentials)
                if login_success:
                    print("✓ Re-authentication successful, retrying album...")
                    consecutive_auth_failures = 0  # Reset failure counter
                    page.goto(girl)
                    random_delay(2, 3)
                    page_content = page.content().lower()
                    
                    if "/join/" in page_content and "logout" not in page_content:
                        print("❌ Still can't access album after re-login - may be premium/unavailable")
                        continue
                    else:
                        print("✓ Successfully accessed album after re-authentication")
                        # Continue with normal processing below
                else:
                    print("❌ Re-authentication failed - trying next album")
                    consecutive_auth_failures += 1
                    if consecutive_auth_failures >= max_consecutive_failures:
                        print(f"❌ Too many consecutive authentication failures ({consecutive_auth_failures}) - stopping")
                        break
                    continue
            
            # Method 1: Look for image links in photo containers (most reliable)
            image_urls = []
            
            # Try to find images in the photo containers
            photo_containers = page.locator("li.photo-container a[href*='cloudfront'], li.photo-container a[href*='amazonaws']").all()
            for container in photo_containers:
                href = container.get_attribute("href")
                if href:
                    # Clean up the URL (remove query parameters if needed)
                    if '?v=' in href:
                        href = href.split('?v=')[0]
                    if href not in image_urls:
                        image_urls.append(href)
            
            # Method 2: Also look for any CloudFront/AWS links as backup
            if len(image_urls) == 0:
                all_links = page.locator("a[href]").all()
                for link in all_links:
                    href = link.get_attribute("href")
                    if href and ("cloudfront" in href or "amazonaws" in href):
                        # Check if it looks like an image URL
                        if any(ext in href.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']) or '/photos/' in href:
                            if '?v=' in href:
                                href = href.split('?v=')[0]
                            if href not in image_urls:
                                image_urls.append(href)
            
            print(f"Found {len(image_urls)} image URLs on album page")
            
            # Extract girl name and album name from URL
            # URL format: https://www.suicidegirls.com/girls/USERNAME/album/ID/ALBUM_NAME/
            import urllib.parse
            parsed_url = urllib.parse.urlparse(girl)
            path_parts = [p for p in parsed_url.path.split('/') if p]
            
            # Expected path: ['girls', 'USERNAME', 'album', 'ID', 'ALBUM_NAME']
            if len(path_parts) >= 5 and path_parts[0] == 'girls' and path_parts[2] == 'album':
                name = path_parts[1]  # USERNAME
                album = path_parts[4]  # ALBUM_NAME
            else:
                # Fallback parsing
                name = girl.replace('https://www.suicidegirls.com/girls/', '').replace('http://www.suicidegirls.com/girls/', '')
                if '/album/' in name:
                    name = name.split('/album/')[0]
                album = "unknown"
                if '/album/' in girl:
                    album_part = girl.split('/album/')[1]
                    if '/' in album_part:
                        album_parts = album_part.split('/')
                        if len(album_parts) >= 2:
                            album = album_parts[1]  # Get album name after ID
            
            print(f"Girl: {name}, Album: {album}")
            
            # Process the found image URLs
            processed_image_urls = []
            for img in image_urls:
                if img and ("cloudfront" in img or "amazonaws" in img or "suicidegirls.com" in img):
                    # Check for image file extensions or image-like URLs
                    if any(ext in img.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']) or '/photo/' in img:
                        if '?v' in img:
                            new_img = img.split('?v')[0]
                            processed_image_urls.append(new_img)
                        else:
                            processed_image_urls.append(img)
            
            print(f"Image URLs found in album: {len(processed_image_urls)}")
            if len(processed_image_urls) > 0:
                print("Sample image URLs:")
                for j, img_url in enumerate(processed_image_urls[:3]):
                    print(f"  {img_url}")
                
                for img_url in processed_image_urls:
                    dlimgs(name, album, img_url)
                    total_images_found += 1
                
                # Reset auth failure counter on successful processing
                consecutive_auth_failures = 0
            else:
                print("No image URLs found - checking page content for debugging")
                # Get all links on the album page for debugging
                debug_links = page.locator("a[href]").all()
                sample_urls = []
                for link in debug_links[:10]:  # First 10 links
                    href = link.get_attribute("href")
                    if href:
                        sample_urls.append(href)
                
                print("Sample URLs found on album page:")
                for url in sample_urls:
                    print(f"  Sample URL: {url}")
                    
        except Exception as e:
            print(f"Error processing album {girl}: {e}")
            
    print(f"\nTotal images processed: {total_images_found}")

def dlimgs(girl, album, url):
    path = os.path.join(os.path.abspath('suicidegirls'), girl)
    path = os.path.join(path, album)
    os.makedirs(path, exist_ok=True)   
    filename = os.path.join(path, re.sub('(.*)/', "", os.path.join(path, url)))
    filename = filename.strip()
    filename = filename.split("?")[0]
    print("Looking at: " + str(url))
    if os.path.exists(filename.strip()) == True:
        print("File: " + str(filename) + " already downloaded, skipping!")
        return
    print("File: "  + str(filename) + " not downloaded, downloading now!")
    response = requests.get(url, stream=True)
    timeout = 10
    while True:
        try:
            with open(filename, 'wb') as out_file:
                shutil.copyfileobj(response.raw, out_file)
                break
        except:
            print("Encountered error writing file '" + str(filename) + "', sleeping " + str(timeout) + " seconds...")
            time.sleep(timeout)
            print("retrying...")
            timeout = timeout + 10
            pass
    del response

def main():
    max_retries = 3
    
    # Initialize Playwright
    with sync_playwright() as p:
        # Launch browser with comprehensive anti-detection settings
        browser = p.chromium.launch(
            headless=HEADLESS_MODE,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--disable-extensions',
                '--disable-default-apps',
                '--no-first-run',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding',
                '--disable-backgrounding-occluded-windows',
                '--disable-client-side-phishing-detection',
                '--disable-crash-reporter',
                '--disable-oopr-debug-crash-dump',
                '--no-crash-upload',
                '--disable-low-res-tiling',
                '--ignore-certificate-errors',
                '--ignore-ssl-errors',
                '--ignore-certificate-errors-spki-list',
                '--allow-running-insecure-content',
                '--disable-web-security',
                '--disable-features=VizDisplayCompositor',
                '--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ]
        )
        
        # Create new page with realistic viewport
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        
        # Add comprehensive anti-detection JavaScript
        page.add_init_script("""
            // Remove webdriver property
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });
            
            // Remove automation indicators
            for (let prop in window) {
                if (prop && prop.includes('webdriver')) {
                    delete window[prop];
                }
            }
            
            // Mock realistic plugins
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
            
            // Mock languages
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
                configurable: true
            });
            
            // Mock permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)
        
        print("Playwright Chromium browser initialized successfully")
        
        for attempt in range(max_retries):
            try:
                print(f"Attempt {attempt + 1} of {max_retries}")
                login_success = login(page, getcreds())
                
                if not login_success:
                    print(f"Login failed on attempt {attempt + 1}")
                    if attempt < max_retries - 1:
                        random_delay(30, 60)  # Long delay between attempts
                        continue
                    else:
                        print("Login failed on all attempts, giving up")
                        break
                
                # Login verification already done in login() function
                # If we get here, login was verified as successful
                print("Login verified successful, proceeding with scraping...")
                credentials = getcreds()
                getimgs(page, getgirls(page), credentials)
                break
                        
            except Exception as e:
                print(f"Error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    print("Retrying...")
                    random_delay(20, 40)
                else:
                    print("Max retries reached, giving up")
                    break
        
        # Close browser
        browser.close()
        print("Finished. You may want to run again to get additional albums that did not fit in the page views.")

if __name__ == '__main__':
    main()
