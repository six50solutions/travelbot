"""
debug_scraper.py — Run this once to see what Google Hotels actually renders.
Saves a screenshot and dumps all dollar amounts found on the page.

Usage:
    python debug_scraper.py
"""
import asyncio
import re
from datetime import date
from playwright.async_api import async_playwright

URL = (
    "https://www.google.com/travel/hotels/search"
    "?q=The+Langham+Chicago&checkin=2026-08-10&checkout=2026-08-13&adults=2&hl=en&gl=us&curr=USD"
)

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)  # visible window
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        print(f"Navigating to: {URL}")
        await page.goto(URL, wait_until="networkidle", timeout=45_000)
        await page.wait_for_timeout(4000)

        # Screenshot
        await page.screenshot(path="debug_google_hotels.png", full_page=False)
        print("✓ Screenshot saved: debug_google_hotels.png")

        # Dump page title and URL
        print(f"  Page title: {await page.title()}")
        print(f"  Final URL:  {page.url}")

        # Find all dollar amounts
        body = await page.inner_text("body")
        prices = re.findall(r'\$\s*[\d,]+', body)
        print(f"\n  Dollar amounts found on page: {prices[:20]}")

        # Dump all text with $ signs (context)
        lines = [l.strip() for l in body.split('\n') if '$' in l and any(c.isdigit() for c in l)]
        print(f"\n  Lines containing prices ({len(lines)} found):")
        for l in lines[:15]:
            print(f"    {l[:100]}")

        # Check for common selectors
        selectors_to_check = [
            '[data-hotelid]', '[jsname="aXgaGb"]', '.BcKAgd',
            '[data-price]', '[jsname="priceRow"]', '.kCsRcb',
            'li[data-ved]', '[role="article"]', '.OSrXXb',
            'h2', '[aria-label*="$"]',
        ]
        print("\n  Selector hit counts:")
        for sel in selectors_to_check:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    print(f"    ✓ {sel:<35} → {count} element(s)")
            except Exception:
                pass

        input("\nPress Enter to close browser...")
        await browser.close()

asyncio.run(main())
