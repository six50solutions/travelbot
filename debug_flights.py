"""
debug_flights.py — See what Google Flights actually renders.
Opens a visible browser, navigates to Google Flights, screenshots + dumps selectors.

Usage:
    python debug_flights.py
"""
import asyncio
import re
from playwright.async_api import async_playwright

# Direct Google Flights URL with IATA codes
# Format: /travel/flights?q=Flights+to+OGG+from+ORD
# Or the tfs encoded format for round trips

URLS_TO_TRY = [
    # Option 1: Natural language search
    "https://www.google.com/travel/flights?q=Flights+from+ORD+to+OGG&hl=en&curr=USD",
    # Option 2: Direct flight search with dates
    "https://www.google.com/travel/flights/search?tfs=CBwQAhoeEgoyMDI2LTA3LTAxagcIARIDT1JEcgcIARIDT0dHGh4SCjIwMjYtMDctMDhqBwgBEgNPR0dyBwgBEgNPUkQ&hl=en&curr=USD",
]

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        url = URLS_TO_TRY[0]
        print(f"\nNavigating to: {url}\n")
        await page.goto(url, wait_until="networkidle", timeout=45_000)
        await page.wait_for_timeout(5000)

        # Dismiss any modals
        for text in ["Accept all", "I agree", "Reject all"]:
            try:
                btn = page.locator(f'button:has-text("{text}")').first
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        await page.wait_for_timeout(3000)
        await page.screenshot(path="debug_flights.png", full_page=False)
        print("✓ Screenshot saved: debug_flights.png")
        print(f"  Title: {await page.title()}")
        print(f"  URL:   {page.url}")

        # Find all dollar amounts
        body = await page.inner_text("body")
        prices = re.findall(r'\$[\d,]+', body)
        print(f"\n  Dollar amounts on page: {prices[:30]}")

        # Check selectors
        selectors = [
            # Google Flights specific
            '[data-ved] li',
            'li[class*="flight"]',
            '[jsname="t7vRVb"]',
            '[jsname="cfb5o"]',
            '.YMlIz',
            '.U3gSDe',
            '.pIav2d',
            '[role="listitem"]',
            'div[class*="price"]',
            'span[class*="price"]',
            # Price spans
            'span[aria-label*="$"]',
            'div[aria-label*="$"]',
            # Flight result rows
            '[data-id]',
            'li[data-ved]',
        ]

        print("\n  Selector hit counts:")
        for sel in selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    # Get text of first match
                    text = await page.locator(sel).first.inner_text()
                    text_preview = text[:80].replace('\n', ' ').strip()
                    print(f"    ✓ {sel:<40} → {count} | '{text_preview}'")
            except Exception:
                pass

        print("\n  Lines with $ signs:")
        lines = [l.strip() for l in body.split('\n') 
                 if '$' in l and any(c.isdigit() for c in l) and len(l.strip()) < 200]
        for l in lines[:20]:
            print(f"    {l[:120]}")

        input("\nPress Enter to close...")
        await browser.close()

asyncio.run(main())
