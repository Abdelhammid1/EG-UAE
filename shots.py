"""Render key screens to PNGs with a headless browser, for review."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5010"
OUT = Path("instance/shots")
OUT.mkdir(parents=True, exist_ok=True)


def shot(page, name):
    page.wait_for_timeout(700)  # let fonts/gradients settle
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    print("saved", name)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=2, locale="ar")
        page = ctx.new_page()

        # 1) login
        page.goto(f"{BASE}/login", wait_until="networkidle")
        shot(page, "01_login")

        # login as owner
        page.fill('input[name="username"]', "owner")
        page.fill('input[name="password"]', "owner123")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        shot(page, "02_dashboard")

        # 3) settings index
        page.goto(f"{BASE}/settings/", wait_until="networkidle")
        shot(page, "03_settings")

        # 4) settings section + change confirmation modal
        page.goto(f"{BASE}/settings/section/sales", wait_until="networkidle")
        try:
            page.click('text=تغيير', timeout=4000)
            page.wait_for_timeout(500)
            # fill a value and continue to the confirmation step
            page.fill('input[name="value"]', "30")
            page.click('text=متابعة')
            page.wait_for_timeout(600)
        except Exception as e:
            print("settings modal step skipped:", e)
        shot(page, "04_settings_confirm")

        # 5) journal form with live balance
        page.goto(f"{BASE}/accounting/journal/new", wait_until="networkidle")
        page.wait_for_timeout(400)
        try:
            selects = page.query_selector_all('select[name="account_id"]')
            amounts = page.query_selector_all('input[name="amount"]')
            # pick a debit account and a credit account, equal amounts
            selects[0].select_option(index=1)
            selects[1].select_option(index=2)
            amounts[0].fill("1500")
            amounts[1].fill("1500")
            page.wait_for_timeout(500)
        except Exception as e:
            print("journal fill skipped:", e)
        shot(page, "05_journal")

        # 6) treasury overview
        page.goto(f"{BASE}/treasury/", wait_until="networkidle")
        shot(page, "06_treasury")

        # 7) transfer page with FX revealed
        page.goto(f"{BASE}/treasury/transfer", wait_until="networkidle")
        page.wait_for_timeout(300)
        try:
            src = page.query_selector('select[name="src"]')
            dst = page.query_selector('select[name="dst"]')
            # choose two entities with different currencies to reveal FX block
            src.select_option(index=1)
            dst.select_option(index=2)
            page.wait_for_timeout(500)
        except Exception as e:
            print("transfer fill skipped:", e)
        shot(page, "07_transfer")

        # 8) trial balance
        page.goto(f"{BASE}/accounting/trial-balance", wait_until="networkidle")
        shot(page, "08_trial_balance")

        # 9) a treasury statement (first treasury)
        page.goto(f"{BASE}/treasury/treasury/1", wait_until="networkidle")
        shot(page, "09_statement")

        browser.close()


if __name__ == "__main__":
    run()
