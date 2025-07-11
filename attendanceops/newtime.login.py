import os
import asyncio
import re
from playwright.async_api import async_playwright

# 1. Read credentials from environment
EMPLOYEE = os.getenv("empICN")
COMPANY = os.getenv("compNum")
PASSWORD = os.getenv("password-1")

# 2. Define paths
DOWNLOAD_DIR = os.path.join(os.getcwd(), "downloads")

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        ctx = await browser.new_context(accept_downloads=True)
        page = await ctx.new_page()

        # Navigate to the page that hosts the login iframe
        await page.goto("https://newtime.co.il/", wait_until="networkidle")

        # 1. Load the root page (which embeds the login iframe)
        await page.goto("https://newtime.co.il/", wait_until="networkidle")

        # 2. Grab the login frame by matching its URL
        login_frame = page.frame(url=re.compile(r"login\.php"))
        if not login_frame:
            raise RuntimeError("❌ Login frame not found – check the iframe URL filter")

        # Fill credentials inside the iframe
        await login_frame.wait_for_selector("#empICN", state="visible")
        await login_frame.fill("#empICN", str(EMPLOYEE))
        await login_frame.fill("#compNum", str(COMPANY))
        await login_frame.fill("#password-1", str(PASSWORD))

        # Click the login link and wait for navigation back to parent page
        async with page.expect_navigation(url="**/main.php"):
            await login_frame.click("#managerLoginBtn")

        await browser.close()

asyncio.run(main())
