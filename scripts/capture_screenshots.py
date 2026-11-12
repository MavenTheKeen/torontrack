"""Capture the README and docs screenshots and the demo GIF from the live site.

Usage: python scripts/capture_screenshots.py [base URL]

Requires Playwright with Chromium and Pillow. Images are written to docs/screenshots.
"""

import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://torontrack.skynetlab.duckdns.org"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}
GIF_SIZE = (960, 600)
GIF_FRAME_MS = 140


def open_map(page, kind: str) -> None:
    page.click(f".map-kind-option[data-kind={kind}]")
    page.wait_for_timeout(6000 if kind == "food" else 3000)  # The food map loads the most.
    close = page.locator(".map-sheet .sheet-close")
    if close.is_visible():  # Close a sheet the map opens by itself.
        close.click()
    page.wait_for_timeout(800)


def capture_pages(browser) -> None:
    page = browser.new_page(viewport=DESKTOP, color_scheme="light")
    page.add_init_script("localStorage.setItem('torontrack-map-kind','transit')")
    for name, route, wait in (
        ("site-home", "#/", ".card"),
        ("site-reliability", "#/reliability", "main h1"),
        ("site-findings", "#/findings", ".finding-chart"),
        ("site-events", "#/events", "main h1"),
    ):
        page.goto(f"{BASE}/{route}")
        page.wait_for_selector(wait, timeout=40000)
        page.wait_for_timeout(2500)
        page.screenshot(path=OUT / f"{name}.png")
    page.goto(f"{BASE}/#/map")
    page.wait_for_selector(".map-kind-option", timeout=40000)
    page.wait_for_timeout(5000)
    page.screenshot(path=OUT / "site-map.png")
    for kind in ("food", "city", "weather"):
        open_map(page, kind)
        page.screenshot(path=OUT / f"map-{kind}.png")
    page.close()
    phone = browser.new_page(viewport=PHONE, color_scheme="light", device_scale_factor=2)
    phone.goto(f"{BASE}/#/")
    phone.wait_for_selector(".card", timeout=40000)
    phone.wait_for_timeout(2500)
    phone.screenshot(path=OUT / "phone-status.png")
    phone.close()


def capture_demo(browser) -> None:
    """The four maps in turn, then the findings page, as a looping GIF."""
    page = browser.new_page(viewport=DESKTOP, color_scheme="light")
    page.add_init_script("localStorage.setItem('torontrack-map-kind','transit')")
    page.goto(f"{BASE}/#/map")
    page.wait_for_selector(".map-kind-option", timeout=40000)
    page.wait_for_timeout(6000)
    frames = []

    def record(seconds: float) -> None:
        for _ in range(int(seconds * 1000 / GIF_FRAME_MS)):
            shot = Image.open(io.BytesIO(page.screenshot()))
            frames.append(shot.convert("RGB").resize(GIF_SIZE, Image.LANCZOS))
            page.wait_for_timeout(GIF_FRAME_MS // 2)

    record(3)
    for kind, seconds in (("food", 2), ("city", 2), ("weather", 4)):
        open_map(page, kind)
        record(seconds)
    page.goto(f"{BASE}/#/findings")
    page.wait_for_selector(".finding-chart", timeout=40000)
    record(2.5)
    page.close()
    # One palette for every frame, from a sample across the whole recording.
    sample = frames[:: max(1, len(frames) // 12)]
    sheet = Image.new("RGB", (GIF_SIZE[0], GIF_SIZE[1] * len(sample)))
    for i, frame in enumerate(sample):
        sheet.paste(frame, (0, GIF_SIZE[1] * i))
    palette = sheet.quantize(colors=160, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    gif[0].save(OUT / "demo.gif", save_all=True, append_images=gif[1:],
                duration=GIF_FRAME_MS, loop=0, optimize=True)  # fmt: skip


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        capture_pages(browser)
        capture_demo(browser)
        browser.close()


if __name__ == "__main__":
    main()
