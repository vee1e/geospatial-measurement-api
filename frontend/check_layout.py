"""Check the landing page fits a viewport with no page scroll.

Needs Playwright with a local Chrome:  pip install playwright

    python3 check_layout.py        # exits 1 if any size scrolls
"""

from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:4173"


def metrics(page) -> dict:
    return page.evaluate(
        """() => {
            return {
                viewport: [innerWidth, innerHeight],
                docScrollHeight: document.documentElement.scrollHeight,
                bodyScrollHeight: document.body.scrollHeight,
                pageScrolls: document.documentElement.scrollHeight > innerHeight,
                tableScrolls: (() => {
                    const el = document.querySelector('.table-wrap');
                    if (!el || el.clientHeight === 0) return null;
                    return { client: el.clientHeight, scroll: el.scrollHeight, overflows: el.scrollHeight > el.clientHeight };
                })(),
                drop: (() => {
                    const el = document.querySelector('#drop');
                    const r = el.getBoundingClientRect();
                    return { top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height) };
                })(),
                h1Bottom: Math.round(document.querySelector('h1').getBoundingClientRect().bottom),
                sections: [...document.querySelectorAll('main > section')].map(s => ({
                    cls: s.className, h: Math.round(s.getBoundingClientRect().height)
                })),
            };
        }"""
    )


def run(width: int, height: int) -> dict:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(URL, wait_until="networkidle")

        empty = metrics(page)
        page.screenshot(path=f"/tmp/landing-{width}x{height}-empty.png")

        page.set_input_files("#file-input", "/tmp/many.kml")
        page.wait_for_selector("#feature-rows tr", timeout=30000)
        page.wait_for_function(
            "() => document.querySelector('#status-message').textContent === 'Complete.'",
            timeout=30000,
        )
        page.wait_for_timeout(900)  # let the count-up animation settle

        loaded = metrics(page)
        loaded["rows"] = page.eval_on_selector_all("#feature-rows tr", "els => els.length")
        page.screenshot(path=f"/tmp/landing-{width}x{height}-loaded.png")
        browser.close()

    return {"empty": empty, "loaded": loaded}


if __name__ == "__main__":
    sizes = [(2560, 1440), (1920, 1080), (1440, 900)]
    results = {f"{w}x{h}": run(w, h) for w, h in sizes}
    print(json.dumps(results, indent=2))
    bad = [
        name
        for name, data in results.items()
        if data["empty"]["pageScrolls"] or data["loaded"]["pageScrolls"]
    ]
    print("\npage scrolls:", bad or "none")
    sys.exit(1 if bad else 0)
