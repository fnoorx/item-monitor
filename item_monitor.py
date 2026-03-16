"""Authenticated item-listing monitor with daily snapshot and change detection."""

from datetime import datetime, timedelta
from pathlib import Path
import csv
import json
import os
import re
import signal
import threading
import time

from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# ------------------------------------
# PATHS
# ------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CSV_DIR = BASE_DIR / "csv"
SITE_CONFIG_PATH = BASE_DIR / "site_config.json"
USERNAME_ENV_VAR = "PORTFOLIO_SCRAPER_USERNAME"
PASSWORD_ENV_VAR = "PORTFOLIO_SCRAPER_PASSWORD"


def load_site_config(filepath=SITE_CONFIG_PATH):
    """Load target-specific selectors and URLs from a local-only config file."""
    path = Path(filepath)

    if not path.is_file():
        raise FileNotFoundError(
            f"Site config '{filepath}' not found. "
            "Copy site_config.example.json to site_config.json and fill in local values."
        )

    config = json.loads(path.read_text(encoding="utf-8"))
    required_keys = [
        "base_url",
        "listing_url",
        "home_url_contains",
        "login_link_xpath",
        "username_locators",
        "password_locators",
        "secondary_auth_button_css",
        "account_menu_css",
        "item_selector",
        "next_button_selector",
        "cookie_overlay_selector",
        "card_ancestor_xpaths",
    ]

    missing = [key for key in required_keys if key not in config]
    if missing:
        raise ValueError(f"Missing required site config keys: {', '.join(missing)}")

    return config


# ------------------------------------
# ENVIRONMENT CREDENTIALS
# ------------------------------------
def load_credentials():
    """Read credentials from environment variables only."""
    env_username = os.getenv(USERNAME_ENV_VAR, "").strip()
    env_password = os.getenv(PASSWORD_ENV_VAR, "").strip()

    return env_username, env_password


def require_credentials(username, password, source="credentials"):
    if not username or not password:
        raise ValueError(
            f"Missing username/password from {source}. "
            f"Expected {USERNAME_ENV_VAR}/{PASSWORD_ENV_VAR}."
        )
    return username, password


# ------------------------------------
# SELENIUM HELPERS
# ------------------------------------
def wait_first(driver, timeout, locators):
    """Try multiple selectors so small UI changes do not break the flow immediately."""
    wait = WebDriverWait(driver, timeout)
    last_exc = None

    for locator in locators:
        by_name = locator["by"].upper()
        value = locator["value"]
        by = getattr(By, by_name)
        try:
            return wait.until(EC.presence_of_element_located((by, value)))
        except Exception as exc:
            last_exc = exc
    raise last_exc


def authenticate(driver, username, password, config):
    """Complete the sign-in flow, including the secondary auth prompt."""
    login_link = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, config["login_link_xpath"]))
    )
    url = login_link.get_attribute("href")
    driver.get(url)

    username_el = wait_first(driver, 20, config["username_locators"])
    username_el.clear()
    username_el.send_keys(username + Keys.ENTER)

    secondary_auth_button = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, config["secondary_auth_button_css"]))
    )
    secondary_auth_button.click()

    password_el = wait_first(driver, 20, config["password_locators"])
    password_el.clear()
    password_el.send_keys(password + Keys.ENTER)


def wait_for_home(driver, config, timeout=300):
    wait = WebDriverWait(driver, timeout)
    home_fragment = config["home_url_contains"].lower()

    wait.until(lambda d: home_fragment in d.current_url.lower())
    wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, config["account_menu_css"]))
    )


# ------------------------------------
# ABORT EVENT SETUP
# ------------------------------------
def setup_abort_event():
    abort_event = threading.Event()

    def handle_sigint(sig, frame):
        print("\nAbort requested (Ctrl+C). Finishing up...")
        abort_event.set()

    try:
        signal.signal(signal.SIGINT, handle_sigint)
    except Exception:
        pass

    return abort_event


# ------------------------------------
# SCRAPING FUNCTIONS
# ------------------------------------
ITEM_TOKEN_RE = re.compile(r"/([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+)(?=[-_.])", re.I)
ENTRY_KEY_RE = re.compile(r"/([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)-", re.I)


def load_page(driver, item_selector, timeout=30, stable_for=0.8):
    """Wait until listing content appears and stops changing due to lazy loading."""
    wait = WebDriverWait(driver, timeout)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, item_selector)))

    end = time.time() + timeout
    last_count = -1
    stable_since = None

    while time.time() < end:
        count = len(driver.find_elements(By.CSS_SELECTOR, item_selector))
        if count != last_count:
            last_count = count
            stable_since = time.time()
        elif stable_since and (time.time() - stable_since) >= stable_for and count > 0:
            return
        time.sleep(0.2)


def clear_overlay_if_present(driver, overlay_selector):
    """Dismiss blocking notices that can intercept clicks during pagination."""
    overlays = driver.find_elements(By.CSS_SELECTOR, overlay_selector)
    if overlays:
        buttons = overlays[0].find_elements(By.CSS_SELECTOR, "button")
        for button in buttons:
            txt = (button.text or "").lower()
            if any(k in txt for k in ["understand", "accept", "agree", "ok", "got it", "close"]):
                try:
                    button.click()
                    return
                except Exception:
                    pass
        driver.execute_script("arguments[0].style.display='none';", overlays[0])


def click_next_safely(driver, next_button, overlay_selector):
    for _ in range(3):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_button)
            next_button.click()
            return True
        except ElementClickInterceptedException:
            clear_overlay_if_present(driver, overlay_selector)

    driver.execute_script("arguments[0].click();", next_button)
    return True


def urls_from_img(img, abort_event=None):
    urls = []

    try:
        for attr in ("src", "data-src"):
            value = img.get_attribute(attr) or ""
            if value:
                urls.append(value)

        for attr in ("srcset", "data-srcset"):
            value = img.get_attribute(attr) or ""
            if not value:
                continue
            for part in value.split(","):
                url = part.strip().split(" ")[0].strip()
                if url:
                    urls.append(url)
    except (WebDriverException, ConnectionResetError):
        if abort_event is not None and abort_event.is_set():
            return []
        return []

    return urls


def extract_item_codes(card_el, href=None, abort_event=None):
    """Collect identifier-like tokens from image URLs, with HTML fallback when needed."""
    base = None
    if href:
        match = ENTRY_KEY_RE.search(href)
        if match:
            base = match.group(1).lower()

    codes, seen = [], set()
    # Prefer listing imagery first, then fall back to any image if the markup is inconsistent.
    imgs = card_el.find_elements(By.CSS_SELECTOR, "img[class*='item-image'], picture source")

    if not imgs:
        imgs = card_el.find_elements(By.CSS_SELECTOR, "img")

    for img in imgs:
        if abort_event is not None and abort_event.is_set():
            break

        for url in urls_from_img(img, abort_event=abort_event):
            match = ITEM_TOKEN_RE.search(url)
            if not match:
                continue
            code = match.group(1).upper()

            if base and not (code.lower().startswith(base + "-") or code.lower().startswith(base + "_")):
                continue
            if code not in seen:
                seen.add(code)
                codes.append(code)

    if not codes:
        # Some pages expose image URLs only in raw tile HTML until hydration completes.
        try:
            html = card_el.get_attribute("innerHTML") or ""
            fallback = extract_item_codes_from_html(html, base=base)
            if fallback:
                return fallback
        except Exception:
            pass

    return ", ".join(codes)


def extract_item_codes_from_html(html, base=None):
    codes, seen = [], set()
    for match in ITEM_TOKEN_RE.finditer(html or ""):
        code = match.group(1).upper().replace("_", "-")

        if base and not code.lower().startswith(base + "-"):
            continue

        if code not in seen:
            seen.add(code)
            codes.append(code)

    return ", ".join(codes)


def wait_for_tile_to_have_code(card_el, timeout=1.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            html = card_el.get_attribute("innerHTML") or ""
        except Exception:
            return False
        if ITEM_TOKEN_RE.search(html):
            return True
        time.sleep(0.08)
    return False


def find_item_card(anchor, card_ancestor_xpaths):
    for xpath in card_ancestor_xpaths:
        try:
            return anchor.find_element(By.XPATH, xpath)
        except Exception:
            continue
    raise ValueError("Could not locate the item card ancestor for this entry.")


def scrape(driver, config, abort_event):
    """Traverse the full listing, extract item data, and write a daily snapshot."""
    items = []
    item_selector = config["item_selector"]
    next_selector = config["next_button_selector"]
    overlay_selector = config["cookie_overlay_selector"]

    driver.get(config["listing_url"])

    try:
        while True:
            if abort_event.is_set():
                print("Abort event detected. Ending scrape.")
                break

            load_page(driver, item_selector, timeout=30, stable_for=0.8)
            clear_overlay_if_present(driver, overlay_selector)
            item_links = driver.find_elements(By.CSS_SELECTOR, item_selector)

            for anchor in item_links:
                if abort_event.is_set():
                    print("Abort during item loop. Ending scrape.")
                    return write_csv(items)

                name = anchor.text.strip()
                href = anchor.get_attribute("href")
                card = find_item_card(anchor, config["card_ancestor_xpaths"])

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                time.sleep(0.05)

                item_codes = extract_item_codes(card, href=href, abort_event=abort_event)

                if not item_codes:
                    # Give lazy-loaded tiles a second chance before accepting missing codes.
                    wait_for_tile_to_have_code(card, timeout=1.5)
                    item_codes = extract_item_codes(card, href=href, abort_event=abort_event)

                items.append(
                    {
                        "item_name": name,
                        "link": href,
                        "item_codes": item_codes,
                    }
                )

            next_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, next_selector))
            )

            if next_button.get_attribute("disabled") is not None:
                print("No more pages. Scraping completed.")
                break

            first_item = item_links[0] if item_links else None
            clear_overlay_if_present(driver, overlay_selector)
            click_next_safely(driver, next_button, overlay_selector)

            if first_item is not None:
                WebDriverWait(driver, 30).until(EC.staleness_of(first_item))

    except KeyboardInterrupt:
        abort_event.set()
        print("KeyboardInterrupt caught. Saving partial results...")
    except (WebDriverException, ConnectionResetError) as exc:
        if abort_event.is_set():
            print(f"Abort + driver error ({type(exc).__name__}). Saving partial results...")
        else:
            print(
                f"WebDriver error ({type(exc).__name__}): {exc}. "
                "Saving partial results anyway..."
            )

    return write_csv(items)


# ------------------------------------
# CSV FUNCTIONS
# ------------------------------------
def write_csv(items):
    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"all_items_{today_str}.csv"

    CSV_DIR.mkdir(parents=True, exist_ok=True)
    file_path = CSV_DIR / filename

    if file_path.exists():
        print(f"File {file_path} already exists. Skipping write.")
        return str(file_path)

    with open(file_path, "w", newline="", encoding="utf-8") as file:
        field_names = ["item_name", "link", "item_codes"]
        writer = csv.DictWriter(file, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(items)

    print(f"CSV saved as {filename}")
    return str(file_path)


def find_recent_csv(csv_dir: Path, days_back: int = 10):
    for days_ago in range(1, days_back + 1):
        date_string = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        path = csv_dir / f"all_items_{date_string}.csv"
        if path.exists():
            return str(path)
    return None


def compare_csv(old_file, new_file):
    """Compare consecutive snapshots and export only newly seen items."""
    import pandas as pd
    from pandas.errors import EmptyDataError, ParserError

    def normalize_links(series):
        # Normalize links so cosmetic suffix differences do not create false positives.
        values = series.fillna("").astype(str)
        return values.str.lower().str.strip().str.replace(r"\.html$", "", regex=True)

    try:
        old_df = pd.read_csv(old_file)
        new_df = pd.read_csv(new_file)
    except (EmptyDataError, ParserError) as exc:
        print(f"CSV read error: {exc}. Skipping comparison.")
        return None

    if "link" not in old_df.columns or "link" not in new_df.columns:
        print("Missing required 'link' column. Skipping comparison.")
        return None

    old_df["normalized_link"] = normalize_links(old_df["link"])
    new_df["normalized_link"] = normalize_links(new_df["link"])

    new_items = new_df[~new_df["normalized_link"].isin(old_df["normalized_link"])].drop(
        columns=["normalized_link"]
    )

    print(f"\nCompared {len(new_df)} items (new) with {len(old_df)} (old).")

    if new_items.empty:
        print("No new items found.")
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    output_path = CSV_DIR / f"new_items_{today_str}.csv"

    if output_path.exists():
        print(f"File {output_path} already exists. Skipping write.")
        return str(output_path)

    new_items.to_csv(output_path, index=False)
    print(f"New items saved to: {output_path}")

    return str(output_path)


# ------------------------------------
# MAIN
# ------------------------------------
def main():
    """Run authentication, scrape the current snapshot, then diff against a recent run."""
    abort_event = setup_abort_event()
    config = load_site_config(SITE_CONFIG_PATH)

    username, password = load_credentials()
    username, password = require_credentials(
        username,
        password,
        "environment variables",
    )

    driver = webdriver.Chrome()

    try:
        driver.get(config["base_url"])
        authenticate(driver, username, password, config)
        wait_for_home(driver, config, timeout=300)

        old_file_path = find_recent_csv(CSV_DIR, days_back=10)
        new_file_path = scrape(driver, config, abort_event)
    finally:
        try:
            driver.quit()
        except Exception:
            try:
                driver.close()
            except Exception:
                pass
            try:
                driver.service.stop()
            except Exception:
                pass

    if old_file_path:
        compare_csv(old_file_path, new_file_path)
    else:
        print("No old CSV file found in the last 10 days. Skipping comparison.")

    print("Script complete.")


if __name__ == "__main__":
    main()
