"""
Batch-fetch page source via the view-page-source.com tool (the same tool
saved as output1.html) and save each result into its own .html file.

How it works:
  1. Opens the tool page in a real Chrome browser (Selenium).
  2. For each link in LINKS, types it into the #uri input and clicks submit.
  3. Waits for the result to appear inside #code-block (.code-body).
  4. Saves the extracted source code to a file in OUTPUT_DIR.

Setup:
  pip install selenium

  You also need Google Chrome installed. Selenium 4.6+ auto-downloads the
  matching chromedriver for you (via Selenium Manager), so you normally
  don't need to install chromedriver yourself.

Usage:
  python scrape_via_viewpagesource.py
"""

import os
import re
import time
import random
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

# The tool page to drive. Use the live site (recommended, since the local
# file's JS calls a backend API that may not work correctly from file://).
# If you'd rather use your local copy, replace this with:
#   "file:///absolute/path/to/output1.html"
TOOL_URL = "https://www.view-page-source.com/"

OUTPUT_DIR = "output_pages"

# Delay range (seconds) between requests, to avoid hammering the tool/site
DELAY_RANGE = (2.5, 5.0)

# How long to wait for a result before giving up on a link
RESULT_TIMEOUT = 30

# Paste/replace the links you want processed here.
LINKS = [
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-1/overlord-tap-1-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-2/overlord-tap-2-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-tap-3-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-tap-3-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-tap-3-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-tap-3-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-tap-3-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-vol-3-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-3/overlord-tap-3-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-4/overlord-tap-4-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-5/overlord-tap-5-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-6/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-6/overlord-tap-6-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-7/overlord-tap-7-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-8/overlord-tap-8-ngoai-truyen-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-8/overlord-tap-8-ngoai-truyen-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-9/overlord-tap-9-brand-new-chapter/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-10/overlord-tap-10-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-10/overlord-tap-10-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-10/overlord-tap-10-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-10/overlord-tap-10-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-10/overlord-tap-10-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-11/overlord-tap-11-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-1-phan-1/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-1-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-12/overlord-vol-12-chuong-1-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-12/overlord-tap-12-chuong-2-phan-1/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-2-phan-2/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-2-phan-3/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-2-phan-4/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-3-phan-1/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chuong-3-phan-2/",
    "https://meobietbay.com/light-novel/overlord-tap-12-chap-3-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/chuong-1-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-1-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-1-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-1-phan-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-1-phan-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-2-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-2-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-2-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-2-phan-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-3-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-3-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-3-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-3-phan-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-3-phan-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-3-phan-6/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-4-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-4-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-4-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-4-phan-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-4-phan-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-4-phan-6/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-13/overlord-tap-13-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14/overlord-tap-14-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-chuong-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-chuong-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-chuong-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-chuong-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-chuong-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-14-chuong-ket/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-gioi-thieu/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15/overlord-tap-15-phan-mo-dau/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15/overlord-tap-15-chuong-1-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15/overlord-tap-15-chuong-1-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-1-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-2-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-2-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-2-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap15-chuong-3-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-3-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-3-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-chuong-3-phan-4-phan-cuoi/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-15-giao-doan/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-4-phan-1-trai-nghiem-cuoc-song-thon-lang/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-4-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-4-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-4-phan-4/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-4-phan-5/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-5-phan-1/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap16-chuong-5-phan-2/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-5-phan-3/",
    "https://meobietbay.com/light-novel/overlord/overlord-tap-16-chuong-ket/",
]


def load_links_from_file(path):
    """Optional helper: read one URL per line from a text file instead of
    hardcoding LINKS above. Call this and assign the result to LINKS if
    you prefer that workflow."""
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip().startswith("http")]


def slugify(url: str) -> str:
    """Turn a URL into a safe filename."""
    parsed = urlparse(url)
    slug = (parsed.path or parsed.netloc).strip("/")
    slug = slug.replace("/", "_")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", slug)
    return slug or "index"


def strip_scheme(url: str) -> str:
    """The tool's input field already shows a fixed 'https://' prefix and
    expects just the rest (e.g. 'example.com/path')."""
    return re.sub(r"^https?://", "", url)


def build_driver():
    options = webdriver.ChromeOptions()
    # Comment the next line out if you want to watch the browser work
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--disable-gpu")
    return webdriver.Chrome(options=options)


def fetch_source_for_url(driver, target_url: str) -> str:
    """Fill the form on the already-loaded tool page, submit, and return
    the extracted source code text once it appears."""

    wait = WebDriverWait(driver, RESULT_TIMEOUT)

    uri_input = wait.until(EC.presence_of_element_located((By.ID, "uri")))
    uri_input.clear()
    uri_input.send_keys(strip_scheme(target_url))

    submit_btn = driver.find_element(By.ID, "submit-btn")
    submit_btn.click()

    # Wait until #code-block has non-empty text (i.e. the async result
    # has actually loaded), retrying past any stale-element hiccups.
    def result_ready(d):
        try:
            el = d.find_element(By.ID, "code-block")
            return el if el.text.strip() else False
        except StaleElementReferenceException:
            return False

    code_el = wait.until(result_ready)
    return code_el.text


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    driver = build_driver()

    try:
        for i, link in enumerate(LINKS, start=1):
            print(f"[{i}/{len(LINKS)}] {link}")

            driver.get(TOOL_URL)  # fresh page each time, avoids leftover state

            try:
                source_code = fetch_source_for_url(driver, link)
            except TimeoutException:
                print(f"  -> TIMED OUT, skipping")
                continue

            filename = slugify(link) + ".html"
            filepath = os.path.join(OUTPUT_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(source_code)

            print(f"  -> saved to {filepath} ({len(source_code)} chars)")

            time.sleep(random.uniform(*DELAY_RANGE))

    finally:
        driver.quit()

    print("Done.")


if __name__ == "__main__":
    main()