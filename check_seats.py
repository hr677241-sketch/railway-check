"""
Pakistan Railway (RABTA) Seat Availability Checker
───────────────────────────────────────────────────
Route  : Karachi (KCT) → Sadiqabad (SDK)
Trains : Khyber Mail | Fareed Express | Bahauddin Zakria Express
         (set TARGET_TRAINS = [] to monitor ALL trains on the route)
Class  : Economy (EC) + Economy Sleeper (ECS)
Date   : 2026-05-23
Alert  : Gmail → hr677241@gmail.com

RABTA renders a JS SPA with a fixed seat table:
  Columns: PC | ACSB | ACSL | ACLZ | ACSS | EC | ECS
  Each cell is either "N/A" or "<count> Rs.<price>"
We parse EC (index 5) and ECS (index 6) per row.
"""

import os
import re
import smtplib
import time
import traceback
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ──────────────────────────────────────────────
# CONFIGURATION  (secrets come from GitHub Actions env vars)
# ──────────────────────────────────────────────
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO       = "hr677241@gmail.com"

TRAVEL_DATE       = "2026-05-23"
FROM_STATION_CODE = "KCT"
TO_STATION_CODE   = "SDK"
FROM_STATION_NAME = "Karachi"
TO_STATION_NAME   = "Sadiqabad"

# Trains to watch (lowercase, partial match). Set to [] to alert on ANY train.
TARGET_TRAINS = [
    "khyber mail",
    "fareed express",
    "bahauddin zakria express",
]

SEARCH_URL = (
    "https://www.pakrailways.gov.pk/buy"
    f"?boardStationCode={FROM_STATION_CODE}"
    f"&arrivalStationCode={TO_STATION_CODE}"
    f"&travelDate={quote(TRAVEL_DATE + ' 00:00:00')}"
    "&travelPeriod=00%3A00-24%3A00"
)

# RABTA seat-column positions (0-based) after "Duration":
#   0=PC  1=ACSB  2=ACSL  3=ACLZ  4=ACSS  5=EC  6=ECS
EC_COL_INDEX  = 5
ECS_COL_INDEX = 6


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
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    driver.set_page_load_timeout(90)
    return driver


# ──────────────────────────────────────────────
# SPA WAIT
# ──────────────────────────────────────────────

def wait_for_spa(driver: webdriver.Chrome, timeout: int = 60) -> bool:
    """Poll until RABTA has rendered train results (or a 'no trains' notice)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            if "Booking" in body or "booking" in body.lower() \
                    or "No Train" in body or "no train" in body.lower():
                print("[INFO] SPA ready — train data detected")
                return True
        except Exception:
            pass
        time.sleep(3)
    print("[WARN] Timed out waiting for SPA — attempting parse anyway")
    return False


# ──────────────────────────────────────────────
# SEAT COLUMN PARSER
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# SEAT COLUMN PARSER  (fixed)
# ──────────────────────────────────────────────

def get_ec_ecs(row_text: str) -> tuple[int, int]:
    """
    Parse EC (col 5) and ECS (col 6) from a RABTA train-row string.

    RABTA renders seat columns in ONE of three formats per class:
      • "N/A"            → class not offered         → count = 0
      • "0"              → offered but sold out       → count = 0
      • "3\nRs.2300"     → 3 seats at Rs.2300         → count = 3
      • "3 Rs.2300"      → same, but on one line      → count = 3

    We locate the seat section (everything after the duration line, e.g.
    "10 h 2 min"), then walk it line-by-line.
    """
    # ── find where seat data starts (right after "N h M min") ──
    dur_m = re.search(r'\d+\s+h\s+\d+\s+min', row_text)
    seat_section = row_text[dur_m.end():] if dur_m else row_text

    lines = [l.strip() for l in seat_section.splitlines() if l.strip()]

    cols: list[int] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.upper() == "N/A":
            cols.append(0)
            i += 1

        elif re.match(r'^\d+\s+Rs\.', line, re.I):
            # "3 Rs.2300" on a single line
            cols.append(int(line.split()[0]))
            i += 1

        elif re.match(r'^\d+$', line):
            count = int(line)
            # peek: is the next line a price?
            if i + 1 < len(lines) and re.match(r'^Rs\.', lines[i + 1], re.I):
                cols.append(count)
                i += 2          # consume both the count and the price line
            else:
                cols.append(0)  # bare "0" = sold out, no price follows
                i += 1

        elif re.match(r'^Rs\.', line, re.I):
            i += 1              # orphaned price line – skip

        elif line.lower() == "booking":
            break               # end of seat data

        else:
            i += 1

    if len(cols) <= EC_COL_INDEX:
        return 0, 0

    ec  = cols[EC_COL_INDEX]
    ecs = cols[ECS_COL_INDEX] if len(cols) > ECS_COL_INDEX else 0
    return ec, ecs


def extract_seat_columns(row_text: str) -> list[int]:
    """Kept for compatibility; now delegates to get_ec_ecs internals."""
    dur_m = re.search(r'\d+\s+h\s+\d+\s+min', row_text)
    seat_section = row_text[dur_m.end():] if dur_m else row_text
    lines = [l.strip() for l in seat_section.splitlines() if l.strip()]
    cols: list[int] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.upper() == "N/A":
            cols.append(0); i += 1
        elif re.match(r'^\d+\s+Rs\.', line, re.I):
            cols.append(int(line.split()[0])); i += 1
        elif re.match(r'^\d+$', line):
            if i + 1 < len(lines) and re.match(r'^Rs\.', lines[i + 1], re.I):
                cols.append(int(line)); i += 2
            else:
                cols.append(0); i += 1
        elif re.match(r'^Rs\.', line, re.I):
            i += 1
        elif line.lower() == "booking":
            break
        else:
            i += 1
    return cols


# ──────────────────────────────────────────────
# TRAIN NAME EXTRACTOR
# ──────────────────────────────────────────────

_KNOWN_STATIONS = {
    "karachi", "cantt", "sadikabad", "sadiqabad", "lahore",
    "peshawar", "quetta", "rawalpindi", "multan", "faisalabad",
    "hyderabad", "sukkur", "larkana", "nawabshah",
}


def extract_train_name(row_text: str) -> str:
    """
    Pull the human-readable train name from a RABTA row string like:
      '11UP Hazara Express KARACHI CANTT SADIKABAD 07:00 ...'
    """
    # Strip leading train number (e.g. "11UP", "13DN")
    cleaned = re.sub(r'^\s*\d+\w+\s+', '', row_text.strip())
    name_parts = []
    for word in cleaned.split():
        if re.match(r'\d{1,2}:\d{2}', word):   # hit a time → stop
            break
        if word.upper() == word and word.lower() in _KNOWN_STATIONS:
            break
        if word.upper() == word and len(word) > 3:  # all-caps station word
            break
        name_parts.append(word)
    return " ".join(name_parts).strip() or cleaned[:40]


# ──────────────────────────────────────────────
# ROW PARSING
# ──────────────────────────────────────────────

def train_passes_filter(row_lower: str) -> bool:
    """Return True if the row should be considered (passes train-name filter)."""
    if not TARGET_TRAINS:
        return True          # empty filter → watch all trains
    return any(t in row_lower for t in TARGET_TRAINS)


def parse_row_text(row_text: str) -> dict | None:
    """
    Parse one train row (plain text) and return a result dict, or None.
    A valid row must contain 'Booking' and pass the train filter.
    """
    lower = row_text.lower()
    if "booking" not in lower:
        return None
    if not train_passes_filter(lower):
        return None

    ec, ecs = get_ec_ecs(row_text)
    if ec == 0 and ecs == 0:
        return None

    name = extract_train_name(row_text)
    print(f"[FOUND] ✅ {name} → EC: {ec}  ECS: {ecs}")
    return {
        "name":          name,
        "ec_seats":      ec,
        "ecs_seats":     ecs,
        "economy_seats": ec + ecs,
        "booking_url":   SEARCH_URL,
    }


# ──────────────────────────────────────────────
# SCRAPING
# ──────────────────────────────────────────────

def scrape(driver: webdriver.Chrome) -> list[dict]:
    print(f"[INFO] Loading: {SEARCH_URL}")
    driver.get(SEARCH_URL)
    wait_for_spa(driver)

    body_text = driver.find_element(By.TAG_NAME, "body").text
    print(f"[DEBUG] Page snippet (first 800 chars):\n{body_text[:800]}")
    print("─" * 50)

    found   = []
    seen    = set()

    # ── Strategy 1: parse <tr> elements ───────────────────────────
    rows = driver.find_elements(By.TAG_NAME, "tr")
    if rows:
        print(f"[INFO] Parsing {len(rows)} <tr> element(s)")
        for row in rows:
            try:
                rt = row.text.strip()
                if not rt or rt in seen:
                    continue
                seen.add(rt)
                r = parse_row_text(rt)
                if r:
                    found.append(r)
            except Exception:
                pass

    # ── Strategy 2: split body text on train-number markers ───────
    if not found:
        print("[INFO] No <tr> hits — splitting body text on train numbers")
        # RABTA rows start with a number+direction e.g. "11UP", "13DN"
        segments = re.split(r'(?=\b\d{1,3}(?:UP|DN)\b)', body_text)
        for seg in segments:
            seg = seg.strip()
            if not seg or seg in seen or len(seg) < 30:
                continue
            seen.add(seg)
            r = parse_row_text(seg)
            if r:
                found.append(r)

    # ── Strategy 3: line-by-line fallback ─────────────────────────
    if not found:
        print("[INFO] Last resort — line-by-line body scan")
        for line in body_text.splitlines():
            line = line.strip()
            if not line or line in seen or len(line) < 30:
                continue
            seen.add(line)
            r = parse_row_text(line)
            if r:
                found.append(r)

    # ── Diagnostic: warn if target trains never appeared ──────────
    if not found and TARGET_TRAINS:
        page_lower = body_text.lower()
        missing = [t for t in TARGET_TRAINS if t not in page_lower]
        if missing:
            print(
                f"[WARN] Target train(s) not found on page for {TRAVEL_DATE}: "
                + ", ".join(t.title() for t in missing)
            )
            print("[WARN] These trains may not operate on this date. "
                  "Check the RABTA website manually.")

    return found


# ──────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────

def send_alert(available: list[dict]):
    subject = (
        f"🚂 SEATS AVAILABLE — {FROM_STATION_NAME} → {TO_STATION_NAME}"
        f" | Economy | {TRAVEL_DATE}"
    )

    rows_html = ""
    for t in available:
        ec_cell  = (f"<span style='color:#15803d;font-weight:700;font-size:18px;'>"
                    f"{t['ec_seats']}</span>") if t["ec_seats"] else "<span style='color:#9ca3af;'>—</span>"
        ecs_cell = (f"<span style='color:#15803d;font-weight:700;font-size:18px;'>"
                    f"{t['ecs_seats']}</span>") if t["ecs_seats"] else "<span style='color:#9ca3af;'>—</span>"
        rows_html += f"""
        <tr>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;font-weight:600;">
            {t['name']}</td>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;text-align:center;">
            {ec_cell}</td>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;text-align:center;">
            {ecs_cell}</td>
          <td style="padding:12px 16px;border:1px solid #e5e7eb;text-align:center;">
            <a href="{t['booking_url']}"
               style="display:inline-block;background:#2563eb;color:#fff;
                      padding:8px 18px;border-radius:6px;text-decoration:none;
                      font-size:13px;font-weight:600;">Book Now →</a></td>
        </tr>"""

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    html = f"""
<html><body style="font-family:Arial,sans-serif;background:#f0fdf4;padding:32px 16px;margin:0;">
  <div style="max-width:640px;margin:auto;background:#fff;border-radius:14px;
              box-shadow:0 4px 20px rgba(0,0,0,.08);overflow:hidden;">
    <div style="background:linear-gradient(135deg,#15803d,#16a34a);color:#fff;padding:28px;">
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
                       font-size:13px;color:#6b7280;">EC SEATS</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:13px;color:#6b7280;">ECS SEATS</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:13px;color:#6b7280;">ACTION</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;
                  padding:14px 16px;font-size:13px;color:#166534;">
        💡 <strong>Tip:</strong> You need your CNIC and registered mobile
        number to complete the RABTA booking.
      </div>
      <p style="margin-top:20px;font-size:11px;color:#9ca3af;border-top:
                1px solid #f3f4f6;padding-top:14px;">
        Sent by GitHub Actions seat-watcher &bull; {now_utc} UTC
      </p>
    </div>
  </div>
</body></html>"""

    _send_gmail(subject, html)
    print(f"[EMAIL] ✅ Alert sent to {ALERT_TO}")


def send_error_email(err: str):
    subj = "⚠️ Pakistan Rail Checker — Script Error"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    html = (
        f"<p>Seat checker crashed at <strong>{now_utc} UTC</strong>:</p>"
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
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    train_label = (
        ", ".join(t.title() for t in TARGET_TRAINS)
        if TARGET_TRAINS else "ALL trains on route"
    )
    print(f"\n{'='*60}")
    print(f"Pakistan Railway (RABTA) Seat Checker")
    print(f"Run time : {now_utc} UTC")
    print(f"Route    : {FROM_STATION_NAME} ({FROM_STATION_CODE})"
          f" → {TO_STATION_NAME} ({TO_STATION_CODE})")
    print(f"Date     : {TRAVEL_DATE}  |  Class: Economy (EC + ECS)")
    print(f"Trains   : {train_label}")
    print(f"URL      : {SEARCH_URL}")
    print(f"{'='*60}\n")

    driver = None
    try:
        driver    = make_driver()
        available = scrape(driver)

        if available:
            send_alert(available)
            print(f"\n✅ {len(available)} train(s) with Economy seats — alert sent to {ALERT_TO}!")
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
