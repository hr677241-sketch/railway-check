"""
Pakistan Railway (RABTA) Seat Availability Checker
───────────────────────────────────────────────────
Route  : Karachi (KCT) → Sadiqabad (SDK)
Trains : Khyber Mail | Fareed Express | Bahauddin Zakria Express
Class  : Economy
Date   : 2026-05-23
Alert  : Gmail → hr677241@gmail.com

Target URL (direct deep-link into RABTA SPA):
  https://www.pakrailways.gov.pk/buy
    ?boardStationCode=KCT
    &arrivalStationCode=SDK
    &travelDate=2026-05-23%2000%3A00%3A00
    &travelPeriod=00%3A00-24%3A00

RABTA is a JavaScript SPA — we use Selenium + headless Chrome
to load the page, wait for train cards to render, then parse them.
"""

import os
import re
import smtplib
import time
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# ──────────────────────────────────────────────
# CONFIGURATION  (secrets come from GitHub Actions env vars)
# ──────────────────────────────────────────────
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO       = "hr677241@gmail.com"

TRAVEL_DATE       = "2026-05-23"
FROM_STATION_CODE = "KCT"       # Karachi terminal code used by RABTA
TO_STATION_CODE   = "SDK"       # Sadiqabad code used by RABTA
FROM_STATION_NAME = "Karachi"
TO_STATION_NAME   = "Sadiqabad"

TARGET_TRAINS = [
    "khyber mail",
    "fareed express",
    "bahauddin zakria express",
]
TARGET_CLASS  = "economy"   # matched case-insensitively against card text

# Exact URL the RABTA website uses — copied from your browser
SEARCH_URL = (
    "https://www.pakrailways.gov.pk/buy"
    f"?boardStationCode={FROM_STATION_CODE}"
    f"&arrivalStationCode={TO_STATION_CODE}"
    f"&travelDate={quote(TRAVEL_DATE + ' 00:00:00')}"
    "&travelPeriod=00%3A00-24%3A00"
)

# ──────────────────────────────────────────────
# BROWSER SETUP
# ──────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    # Selenium 4.6+ has a built-in driver manager — no webdriver-manager needed
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    driver.set_page_load_timeout(90)
    return driver


# ──────────────────────────────────────────────
# SCRAPING
# ──────────────────────────────────────────────

def wait_for_spa(driver: webdriver.Chrome, timeout: int = 60) -> bool:
    """
    Poll until RABTA SPA has rendered train cards OR a 'no trains' message.
    Returns True when something useful is on screen.
    """
    css_candidates = [
        ".train-card", ".train-item", ".journey-card",
        "[class*='trainCard']", "[class*='TrainCard']",
        "[class*='train-result']", "[class*='TrainResult']",
        "li[class*='train']", "div[class*='train']",
    ]
    xpath_candidates = [
        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'khyber')]",
        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'fareed')]",
        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'bahauddin')]",
        "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'economy')]",
        "//*[contains(text(),'No Train')]",
        "//*[contains(text(),'no train')]",
    ]

    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in css_candidates:
            try:
                if driver.find_elements(By.CSS_SELECTOR, sel):
                    print(f"[INFO] SPA ready — found element: {sel}")
                    return True
            except Exception:
                pass
        for xp in xpath_candidates:
            try:
                if driver.find_elements(By.XPATH, xp):
                    print(f"[INFO] SPA ready — found XPath content")
                    return True
            except Exception:
                pass
        time.sleep(3)

    # Last resort: check body has real content
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.strip()
        if len(body) > 300:
            print("[INFO] SPA ready (body text > 300 chars)")
            return True
    except Exception:
        pass

    print("[WARN] Timed out waiting for SPA — will attempt parse anyway")
    return False


def scrape(driver: webdriver.Chrome) -> list[dict]:
    print(f"[INFO] Loading: {SEARCH_URL}")
    driver.get(SEARCH_URL)
    wait_for_spa(driver)

    body_text = driver.find_element(By.TAG_NAME, "body").text
    print(f"[DEBUG] Page snippet (first 800 chars):\n{body_text[:800]}")
    print("─" * 50)

    found = []

    # ── Strategy 1: named card containers ──────────────────────
    card_selectors = [
        ".train-card", ".train-item", ".journey-card",
        "[class*='trainCard']", "[class*='TrainCard']",
        "[class*='train-result']", "[class*='TrainResult']",
        "li[class*='train']", "div[class*='train']",
    ]
    cards = []
    for sel in card_selectors:
        try:
            cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                print(f"[INFO] {len(cards)} card(s) via selector '{sel}'")
                break
        except Exception:
            pass

    if cards:
        for card in cards:
            r = parse_block(card)
            if r:
                found.append(r)

    # ── Strategy 2: scan all block elements ────────────────────
    if not found:
        print("[INFO] No named cards — scanning div/li/article elements…")
        for tag in ("div", "li", "article", "section"):
            seen = set()
            for el in driver.find_elements(By.TAG_NAME, tag):
                try:
                    txt = el.text.strip().lower()
                    if len(txt) < 20 or len(txt) > 3000 or txt in seen:
                        continue
                    seen.add(txt)
                    if not any(t in txt for t in TARGET_TRAINS):
                        continue
                    if TARGET_CLASS not in txt and "eco" not in txt:
                        continue
                    r = parse_block(el)
                    if r and r["name"] not in {x["name"] for x in found}:
                        found.append(r)
                except Exception:
                    pass
            if found:
                break

    # ── Strategy 3: full-page text grep ────────────────────────
    if not found:
        print("[INFO] Last resort — full page text scan")
        pg = body_text.lower()
        for train in TARGET_TRAINS:
            if train in pg and ("eco" in pg or "economy" in pg):
                n = extract_seat_count(pg)
                if n > 0:
                    found.append({
                        "name":          train.title(),
                        "economy_seats": n,
                        "booking_url":   SEARCH_URL,
                    })
                    print(f"[FOUND] ✅ {train.title()} (full-page) → {n} seat(s)")

    return found


def parse_block(el, train_override: str = None) -> dict | None:
    try:
        text  = el.text.strip()
        lower = text.lower()

        matched = train_override or next(
            (t for t in TARGET_TRAINS if t in lower), None
        )
        if not matched:
            return None
        if TARGET_CLASS not in lower and "eco" not in lower:
            return None

        seats = extract_seat_count(lower)
        if seats == 0:
            print(f"[INFO] {matched.title()} found but 0 seats")
            return None

        links = el.find_elements(By.TAG_NAME, "a")
        booking_url = SEARCH_URL
        for a in links:
            href = a.get_attribute("href") or ""
            if "pakrailways" in href:
                booking_url = href
                break

        print(f"[FOUND] ✅ {matched.title()} → {seats} Economy seat(s)")
        return {
            "name":          matched.title(),
            "economy_seats": seats,
            "booking_url":   booking_url,
        }
    except Exception:
        return None


def extract_seat_count(text: str) -> int:
    patterns = [
        r"available[^0-9]{0,20}(\d+)",
        r"(\d+)[^0-9]{0,15}available",
        r"eco(?:nomy)?[^0-9]{0,15}(\d+)",
        r"seats?[^0-9]{0,10}(\d+)",
        r"berths?[^0-9]{0,10}(\d+)",
        r"\b([1-9]\d{0,2})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


# ──────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────

def send_alert(available: list[dict]):
    subject = (
        f"🚂 SEATS AVAILABLE — {FROM_STATION_NAME} → {TO_STATION_NAME}"
        f" | Economy | {TRAVEL_DATE}"
    )
    rows = ""
    for t in available:
        rows += f"""
        <tr>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;font-weight:600;">
            {t['name']}</td>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;
                     text-align:center;color:#15803d;font-weight:700;font-size:20px;">
            {t['economy_seats']}</td>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;text-align:center;">
            <a href="{t['booking_url']}"
               style="display:inline-block;background:#2563eb;color:#fff;
                      padding:8px 18px;border-radius:6px;text-decoration:none;
                      font-size:13px;font-weight:600;">Book Now →</a></td>
        </tr>"""

    html = f"""
<html><body style="font-family:Arial,sans-serif;background:#f0fdf4;padding:32px 16px;margin:0;">
  <div style="max-width:620px;margin:auto;background:#fff;border-radius:14px;
              box-shadow:0 4px 20px rgba(0,0,0,.08);overflow:hidden;">
    <div style="background:linear-gradient(135deg,#15803d,#16a34a);
                color:#fff;padding:28px;">
      <div style="font-size:32px;margin-bottom:6px;">🚂</div>
      <h2 style="margin:0;font-size:24px;">Economy Seats Found!</h2>
      <p style="margin:8px 0 0;opacity:.85;font-size:15px;">
        <strong>{FROM_STATION_NAME}</strong> &rarr;
        <strong>{TO_STATION_NAME}</strong> &nbsp;|&nbsp;
        {TRAVEL_DATE} &nbsp;|&nbsp; Economy Class
      </p>
    </div>
    <div style="padding:28px;">
      <p style="margin-top:0;font-size:15px;">
        Economy seats are available now.
        <strong style="color:#dc2626;">Book quickly — seats fill fast!</strong>
      </p>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       text-align:left;font-size:13px;color:#6b7280;">TRAIN</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:13px;color:#6b7280;">ECONOMY SEATS</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:13px;color:#6b7280;">ACTION</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;
                  padding:14px 16px;font-size:13px;color:#166534;">
        💡 <strong>Tip:</strong> You need your CNIC and registered mobile
        number to complete the RABTA booking.
      </div>
      <p style="margin-top:20px;font-size:11px;color:#9ca3af;border-top:
                1px solid #f3f4f6;padding-top:14px;">
        Sent by GitHub Actions seat-watcher &bull;
        {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")} UTC
      </p>
    </div>
  </div>
</body></html>"""

    _send_gmail(subject, html)
    print(f"[EMAIL] ✅ Alert sent to {ALERT_TO}")


def send_error_email(err: str):
    subj = "⚠️ Pakistan Rail Checker — Script Error"
    html = (
        f"<p>Seat checker crashed at "
        f"<strong>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</strong>:</p>"
        f"<pre style='background:#fef2f2;padding:12px;border-radius:6px;"
        f"font-size:12px;overflow:auto;'>{err}</pre>"
    )
    try:
        _send_gmail(subj, html)
    except Exception as e:
        print(f"[WARN] Could not send error email: {e}")


def _send_gmail(subject: str, html: str):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ALERT_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_PASSWORD)
        s.sendmail(GMAIL_USER, ALERT_TO, msg.as_string())


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Pakistan Railway (RABTA) Seat Checker")
    print(f"Run time : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Route    : {FROM_STATION_NAME} ({FROM_STATION_CODE})"
          f" → {TO_STATION_NAME} ({TO_STATION_CODE})")
    print(f"Date     : {TRAVEL_DATE}  |  Class: Economy")
    print(f"Trains   : {', '.join(t.title() for t in TARGET_TRAINS)}")
    print(f"URL      : {SEARCH_URL}")
    print(f"{'='*60}\n")

    driver = None
    try:
        driver    = make_driver()
        available = scrape(driver)

        if available:
            send_alert(available)
            print(f"\n✅ {len(available)} train(s) found — alert sent to {ALERT_TO}!")
        else:
            print("\n❌ No Economy seats right now. Will check again next run.")

    except Exception:
        err = traceback.format_exc()
        print(f"\n[CRITICAL]\n{err}")
        send_error_email(err)
        raise   # marks GitHub Actions run as ❌ failed

    finally:
        if driver:
            driver.quit()
            print("[INFO] Browser closed.")


if __name__ == "__main__":
    main()
