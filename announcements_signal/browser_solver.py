"""Optional Chromium challenge solver for the Bursa announcements collector.

OFF by default.  Enable with ``ANNOUNCEMENTS_BROWSER_SOLVER=1`` only after
installing Selenium *and* a matching Chromium/Chromedriver pair.  The solver
solves the managed JS challenge once, then hands the ``cf_clearance`` cookie
and browser User-Agent back to cloudscraper for pages 1-3.

Every failure path returns ``None`` and logs one line; the collector then
continues with its normal cloudscraper retries.  Nothing here is imported
unless the feature is enabled.
"""

from __future__ import annotations

import logging
import os
import shutil
from urllib.parse import urlencode

log = logging.getLogger("bursa.browser")

API_URL = "https://www.bursamalaysia.com/api/v1/announcements/search"


def _find_binary(env_name: str, candidates: tuple[str, ...]) -> str | None:
    explicit = os.environ.get(env_name, "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def solve_challenge(timeout: int = 45, reserve=None) -> dict | None:
    """Return ``{"cookies": {...}, "user_agent": ...}`` or ``None``.

    ``reserve`` is the collector's budget callback.  The browser navigation is
    counted as one API-budget request before it starts.  If the budget is
    exhausted, the solver is skipped without launching Chromium.
    """
    chromium = _find_binary("CHROMIUM_BINARY",
                            ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"))
    driver_bin = _find_binary("CHROMEDRIVER_BINARY", ("chromedriver",))
    if not chromium or not driver_bin:
        log.warning("Browser solver skipped: Chromium or chromedriver not found; using cloudscraper")
        return None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        log.warning("Browser solver skipped: selenium not installed; using cloudscraper")
        return None

    if reserve is not None and not reserve():
        log.warning("Browser solver skipped: request budget exhausted; using cloudscraper")
        return None

    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.binary_location = chromium
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,900")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # The production service runs as root; Chromium refuses to start
            # its sandbox there.  This is opt-in and only navigates Bursa.
            options.add_argument("--no-sandbox")
        driver = webdriver.Chrome(service=Service(executable_path=driver_bin), options=options)
        driver.set_page_load_timeout(timeout)
        url = API_URL + "?" + urlencode({"ann_type": "company", "per_page": 20, "page": 1})
        driver.get(url)

        def cleared(d):
            cookies = {c["name"]: c["value"] for c in d.get_cookies()}
            title = (d.title or "").lower()
            return cookies if "cf_clearance" in cookies and "just a moment" not in title else False

        cookies = WebDriverWait(driver, timeout, poll_frequency=0.5).until(cleared)
        user_agent = driver.execute_script("return navigator.userAgent")
        log.info("Browser solver obtained cf_clearance; reusing cookie for API pages")
        return {"cookies": cookies, "user_agent": user_agent}
    except Exception as exc:
        log.warning("Browser solver failed: %s; using cloudscraper", type(exc).__name__)
        return None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
