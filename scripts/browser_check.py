"""Explicit browser smoke check against a running local dashboard with real data.

Install the optional browser group and Chromium first; does not collect or modify data.
"""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--screenshot", default="artifacts/dashboard.png")
    args = parser.parse_args()
    output = Path(args.screenshot)
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
        page.goto(args.url, wait_until="domcontentloaded")
        page.get_by_role("heading", name="Toronto Bike Share Reliability Monitor").wait_for()
        page.get_by_text("Current station map", exact=True).wait_for(timeout=30000)
        assert page.locator('[data-testid="stException"]').count() == 0
        assert page.locator('[data-testid="stError"]').count() == 0
        search = page.get_by_role("textbox", name="Search stations")
        search.fill("Fort York")
        search.press("Enter")
        page.get_by_text("Current station map", exact=True).wait_for()
        # Wait for Streamlit's rerun to settle before checking/rendering the page.
        page.wait_for_timeout(1500)
        assert page.locator('[data-testid="stException"]').count() == 0
        search.fill("")
        search.press("Enter")
        page.wait_for_timeout(1500)
        page.get_by_role("tab", name="Frequently full").click()
        page.get_by_role("tab", name="Frequently empty").click()
        page.screenshot(path=str(output.with_stem(output.stem + "-analytics")))
        page.get_by_role(
            "heading", name="Toronto Bike Share Reliability Monitor"
        ).scroll_into_view_if_needed()
        page.screenshot(path=str(output))
        print(f"Browser rendered real dashboard, search and ranking tabs; screenshot: {output}")
        browser.close()


if __name__ == "__main__":
    main()
