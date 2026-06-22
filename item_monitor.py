### Authenticated listing monitor with JSON snapshots and change detection.

from datetime import datetime
from pathlib import Path
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


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
SITE_CONFIG_PATH = BASE_DIR / "site_config.json"
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "listing_snapshot.json"
NEW_ITEMS_PATH = DATA_DIR / "new_items.json"

USERNAME_ENV_VAR = "PORTFOLIO_SCRAPER_USERNAME"
PASSWORD_ENV_VAR = "PORTFOLIO_SCRAPER_PASSWORD"
BASE_URL_ENV_VAR = "PORTFOLIO_TARGET_BASE_URL"
LISTING_URL_ENV_VAR = "PORTFOLIO_TARGET_LISTING_URL"

# These patterns pull stable-looking codes from links and image URLs. They are kept
# generic so the script can be shown publicly without exposing the real site.
ITEM_TOKEN_RE = re.compile(r"/([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+)(?=[-_.])", re.I)
ENTRY_KEY_RE = re.compile(r"/([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)-", re.I)


def load_local_env(filepath: Path = ENV_PATH) -> None:
    """Load local environment variables when python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(filepath)


def load_site_config(filepath: Path = SITE_CONFIG_PATH) -> dict:
    # Site-specific selectors are loaded from a local config file instead of
    # being hardcoded into the public source.
    path = Path(filepath)
    if not path.is_file():
        raise FileNotFoundError(
            f"Site config '{filepath}' not found. "
            "Copy site_config.example.json to site_config.json and fill in local values."
        )

    config = json.loads(path.read_text(encoding="utf-8"))
    required_keys = [
        "login_link_xpath",
        "username_locators",
        "password_locators",
        "secondary_auth_button_css",
        "account_menu_css",
        "item_selector",
        "next_button_selector",
        "overlay_selector",
        "card_ancestor_xpaths",
    ]
    missing = [key for key in required_keys if key not in config]
    if missing:
        raise ValueError(f"Missing required site config keys: {', '.join(missing)}")

    return config


def require_env_values() -> dict:
    # The script needs URLs and credentials at runtime, but those values should
    # stay in the environment or .env file, not in Git.
    values = {
        "base_url": os.getenv(BASE_URL_ENV_VAR, "").strip(),
        "listing_url": os.getenv(LISTING_URL_ENV_VAR, "").strip(),
        "username": os.getenv(USERNAME_ENV_VAR, "").strip(),
        "password": os.getenv(PASSWORD_ENV_VAR, "").strip(),
    }

    missing = [
        env_var
        for key, env_var in [
            ("base_url", BASE_URL_ENV_VAR),
            ("listing_url", LISTING_URL_ENV_VAR),
            ("username", USERNAME_ENV_VAR),
            ("password", PASSWORD_ENV_VAR),
        ]
        if not values[key]
    ]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return values


def resolve_by(by_name: str):
    return getattr(By, by_name.upper())


def wait_first(driver, timeout: int, locators: list[dict]):
    # Some login pages use different field attributes across sessions or UI
    # versions, so this tries a list of possible locators in order.
    wait = WebDriverWait(driver, timeout)
    last_exc = None

    for locator in locators:
        try:
            by = resolve_by(locator["by"])
            return wait.until(EC.presence_of_element_located((by, locator["value"])))
        except Exception as exc:
            last_exc = exc

    raise last_exc


def authenticate(driver, username: str, password: str, config: dict) -> None:
    # Complete the sign-in flow using selectors from the local config. The
    # function waits for each step so it does not race against page loading.
    login_link = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.XPATH, config["login_link_xpath"]))
    )
    driver.get(login_link.get_attribute("href"))

    username_el = wait_first(driver, 20, config["username_locators"])
    username_el.clear()
    username_el.send_keys(username + Keys.ENTER)

    secondary_button = WebDriverWait(driver, 30).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, config["secondary_auth_button_css"]))
    )
    secondary_button.click()

    password_el = wait_first(driver, 20, config["password_locators"])
    password_el.clear()
    password_el.send_keys(password + Keys.ENTER)


def wait_for_authenticated_state(driver, config: dict, timeout: int = 300) -> None:
    # After login, wait for an element that only appears for signed-in users.
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, config["account_menu_css"]))
    )


def setup_abort_event():
    # Ctrl+C sets an event instead of stopping immediately. This lets the script
    # save whatever data it already collected.
    abort_event = threading.Event()
    previous_handler = None

    def handle_sigint(sig, frame):
        print("\nAbort requested. Finishing current work...")
        abort_event.set()

    try:
        previous_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handle_sigint)
    except Exception:
        pass

    return abort_event, previous_handler


def restore_signal_handler(previous_handler) -> None:
    if previous_handler is None:
        return

    try:
        signal.signal(signal.SIGINT, previous_handler)
    except Exception:
        pass


def load_page(driver, item_selector: str, timeout: int = 30, stable_for: float = 0.8) -> None:
    # Dynamic pages often keep loading entries after the first item appears.
    # This waits until the item count has stayed the same for a short time.
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


def clear_overlay_if_present(driver, overlay_selector: str) -> None:
    # Popups and notices can block the next-page click. Try a normal close first,
    # then hide the overlay if no usable button is found.
    overlays = driver.find_elements(By.CSS_SELECTOR, overlay_selector)
    if not overlays:
        return

    buttons = overlays[0].find_elements(By.CSS_SELECTOR, "button")
    for button in buttons:
        text = (button.text or "").lower()
        if any(keyword in text for keyword in ["understand", "accept", "agree", "ok", "got it", "close"]):
            try:
                button.click()
                return
            except Exception:
                pass

    driver.execute_script("arguments[0].style.display='none';", overlays[0])


def click_next_safely(driver, next_button, overlay_selector: str) -> bool:
    # Pagination clicks can fail if a sticky overlay gets in the way, so this
    # retries after clearing overlays before using a JavaScript click fallback.
    for _ in range(3):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_button)
            next_button.click()
            return True
        except ElementClickInterceptedException:
            clear_overlay_if_present(driver, overlay_selector)

    driver.execute_script("arguments[0].click();", next_button)
    return True


def urls_from_image(image, abort_event=None) -> list[str]:
    # Images can store URLs in several attributes depending on lazy loading and
    # responsive image markup, so collect every candidate URL we can inspect.
    urls = []

    try:
        for attr in ("src", "data-src"):
            value = image.get_attribute(attr) or ""
            if value:
                urls.append(value)

        for attr in ("srcset", "data-srcset"):
            value = image.get_attribute(attr) or ""
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


def extract_item_tokens(card_el, href=None, abort_event=None) -> list[str]:
    # Prefer codes found in image URLs because they are usually more stable than
    # display text. The link is used as a guard so unrelated image codes are not kept.
    base = None
    if href:
        match = ENTRY_KEY_RE.search(href)
        if match:
            base = match.group(1).lower()

    tokens, seen = [], set()
    images = card_el.find_elements(By.CSS_SELECTOR, "img, picture source")

    for image in images:
        if abort_event is not None and abort_event.is_set():
            break

        for url in urls_from_image(image, abort_event=abort_event):
            match = ITEM_TOKEN_RE.search(url)
            if not match:
                continue

            token = match.group(1).upper().replace("_", "-")
            if base and not token.lower().startswith(base + "-"):
                continue
            if token not in seen:
                seen.add(token)
                tokens.append(token)

    if tokens:
        return tokens

    # Some pages keep image URLs in raw HTML before Selenium exposes them as
    # attributes, so parse the card markup as a fallback.
    try:
        return extract_item_tokens_from_html(card_el.get_attribute("innerHTML") or "", base=base)
    except Exception:
        return []


def extract_item_tokens_from_html(html: str, base=None) -> list[str]:
    tokens, seen = [], set()
    for match in ITEM_TOKEN_RE.finditer(html or ""):
        token = match.group(1).upper().replace("_", "-")
        if base and not token.lower().startswith(base + "-"):
            continue
        if token not in seen:
            seen.add(token)
            tokens.append(token)

    return tokens


def wait_for_card_token(card_el, timeout: float = 1.5) -> bool:
    # Give lazy-loaded card content a short second chance before accepting that
    # no token was available for this entry.
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


def find_item_card(anchor, card_ancestor_xpaths: list[str]):
    # The clickable link may be nested inside a larger card. The config provides
    # a few ancestor paths so the scraper can find the full card container.
    for xpath in card_ancestor_xpaths:
        try:
            return anchor.find_element(By.XPATH, xpath)
        except Exception:
            continue
    raise ValueError("Could not locate card ancestor for entry.")


def scrape(driver, config: dict, listing_url: str, abort_event) -> list[dict]:
    # Main scraping loop: visit each listing page, collect entry data, then move
    # through pagination until there are no more pages or the user aborts.
    items = []
    item_selector = config["item_selector"]
    next_selector = config["next_button_selector"]
    overlay_selector = config["overlay_selector"]

    driver.get(listing_url)

    try:
        while True:
            if abort_event.is_set():
                print("Abort requested. Ending scrape.")
                break

            load_page(driver, item_selector, timeout=30, stable_for=0.8)
            clear_overlay_if_present(driver, overlay_selector)
            item_links = driver.find_elements(By.CSS_SELECTOR, item_selector)

            for anchor in item_links:
                if abort_event.is_set():
                    print("Abort requested during item loop.")
                    return items

                name = anchor.text.strip()
                href = anchor.get_attribute("href")
                card = find_item_card(anchor, config["card_ancestor_xpaths"])

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", card)
                time.sleep(0.05)

                # Scrolling helps lazy-loaded images render before token extraction.
                item_tokens = extract_item_tokens(card, href=href, abort_event=abort_event)
                if not item_tokens:
                    wait_for_card_token(card, timeout=1.5)
                    item_tokens = extract_item_tokens(card, href=href, abort_event=abort_event)

                items.append(
                    {
                        "name": name,
                        "link": href,
                        "tokens": item_tokens,
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

            # Wait for the old page content to go stale so the next loop reads
            # the new page instead of scraping the same entries again.
            if first_item is not None:
                WebDriverWait(driver, 30).until(EC.staleness_of(first_item))

    except KeyboardInterrupt:
        abort_event.set()
        print("Keyboard interrupt caught. Saving partial results...")
    except (WebDriverException, ConnectionResetError) as exc:
        if abort_event.is_set():
            print(f"Abort plus driver error ({type(exc).__name__}). Saving partial results...")
        else:
            print(f"WebDriver error ({type(exc).__name__}): {exc}. Saving partial results...")

    return items


def normalize_link(link: str) -> str:
    # Links are used as stable snapshot keys. Normalizing reduces false changes
    # from casing, spaces, or optional suffixes.
    return (link or "").lower().strip().removesuffix(".html")


def load_snapshot(snapshot_path: Path = SNAPSHOT_PATH) -> dict:
    # If this is the first run, there is no previous snapshot to compare against.
    if not snapshot_path.exists():
        return {}

    with snapshot_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    items = payload.get("items", {})
    if not isinstance(items, dict):
        return {}

    return items


def build_snapshot(current_items: list[dict]) -> dict:
    # Store entries in a dictionary keyed by normalized link so comparison is a
    # simple lookup instead of a nested loop.
    snapshot = {}
    for item in current_items:
        key = normalize_link(item.get("link"))
        if not key:
            continue
        snapshot[key] = {
            "name": item.get("name"),
            "link": item.get("link"),
            "tokens": item.get("tokens", []),
        }
    return snapshot


def save_snapshot(
    current_items: list[dict],
    new_items: list[dict],
    snapshot_path: Path = SNAPSHOT_PATH,
    new_items_path: Path = NEW_ITEMS_PATH,
) -> None:
    # Write both the full current state and the smaller "new only" report.
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    with snapshot_path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "saved_at": datetime.now().isoformat(),
                "items": build_snapshot(current_items),
            },
            file,
            indent=2,
        )

    new_items_path.parent.mkdir(parents=True, exist_ok=True)
    with new_items_path.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "saved_at": datetime.now().isoformat(),
                "new_items": build_snapshot(new_items),
            },
            file,
            indent=2,
        )


def find_new_items(previous_snapshot: dict, current_items: list[dict]) -> list[dict]:
    # Anything with a normalized link that was not in the previous snapshot is
    # treated as newly discovered.
    new_items = []
    for item in current_items:
        key = normalize_link(item.get("link"))
        if key not in previous_snapshot:
            new_items.append(item)

    return new_items


def main():
    # High-level run order: load configuration, sign in, scrape, compare, save.
    load_local_env()
    config = load_site_config()
    env_values = require_env_values()
    abort_event, previous_handler = setup_abort_event()
    current_items = []
    previous_snapshot = {}

    driver = webdriver.Chrome()
    try:
        previous_snapshot = load_snapshot()

        driver.get(env_values["base_url"])
        authenticate(driver, env_values["username"], env_values["password"], config)
        wait_for_authenticated_state(driver, config, timeout=300)

        current_items = scrape(driver, config, env_values["listing_url"], abort_event)
    finally:
        restore_signal_handler(previous_handler)
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

    new_items = find_new_items(previous_snapshot, current_items)
    save_snapshot(current_items, new_items)

    print(f"Found {len(new_items)} new items.")
    print("Script complete.")
    return new_items


if __name__ == "__main__":
    main()
