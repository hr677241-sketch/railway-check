"""
Pakistan Railway (RABTA) Seat Availability Checker
───────────────────────────────────────────────────
Route  : Karachi (KCT) → Sadiqabad (SDK)
Trains : Khyber Mail | Fareed Express | Bahauddin Zakria Express
         (set TARGET_TRAINS = [] to monitor ALL trains on the route)
Class  : Economy (EC) + Economy Sleeper (ECS)
Date   : 2026-05-23
Alert  : Gmail → hr677241@gmail.com
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
# CONFIGURATION
# ──────────────────────────────────────────────
GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO       = "hr677241@gmail.com"

TRAVEL_DATE       = "2026-06-23"
FROM_STATION_CODE = "KCT"
TO_STATION_CODE   = "SDK"
FROM_STATION_NAME = "Karachi"
TO_STATION_NAME   = "Sadiqabad"

# Trains to watch (lowercase, partial match). Set [] to alert on ANY train.
TARGET_TRAINS = [
         "khyber mail",
         "fareed express",
         "bahauddin zakria express",
         "Millat Express",
         "Allama Iqbal Express",
]

SEARCH_URL = (
    "https://www.pakrailways.gov.pk/buy"
    f"?boardStationCode={FROM_STATION_CODE}"
    f"&arrivalStationCode={TO_STATION_CODE}"
    f"&travelDate={quote(TRAVEL_DATE + ' 00:00:00')}"
    "&travelPeriod=00%3A00-24%3A00"
)

# RABTA seat-column positions (0-based) after duration marker:
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
    """Poll until RABTA has rendered train results or a 'no trains' notice."""
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
# SEAT COLUMN PARSER  (fixed)
# ──────────────────────────────────────────────

def _parse_seat_lines(seat_section: str) -> list[int]:
    """
    Walk seat section line-by-line and return integer counts per column.

    RABTA renders each seat class in ONE of three formats:
      • "N/A"           → not offered                → 0
      • "0"             → offered but sold out        → 0
      • "3\nRs.2300"    → 3 seats at Rs.2300          → 3
      • "3 Rs.2300"     → same on one line            → 3
    """
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
                i += 2      # consume count + price line
            else:
                cols.append(0)  # bare "0" = sold out, no price
                i += 1

        elif re.match(r'^Rs\.', line, re.I):
            i += 1          # orphaned price line – skip

        elif line.lower() == "booking":
            break           # end of seat data

        else:
            i += 1

    return cols


def get_ec_ecs(row_text: str) -> tuple[int, int]:
    """Return (ec_seats, ecs_seats) from a single train row string."""
    # Anchor to everything AFTER the duration string e.g. "10 h 2 min"
    dur_m = re.search(r'\d+\s+h\s+\d+\s+min', row_text)
    seat_section = row_text[dur_m.end():] if dur_m else row_text

    cols = _parse_seat_lines(seat_section)

    if len(cols) <= EC_COL_INDEX:
        return 0, 0

    ec  = cols[EC_COL_INDEX]
    ecs = cols[ECS_COL_INDEX] if len(cols) > ECS_COL_INDEX else 0
    return ec, ecs


# ──────────────────────────────────────────────
# PRICE EXTRACTOR
# ──────────────────────────────────────────────

def get_ec_ecs_with_price(row_text: str) -> tuple[int, int, str, str]:
    """Return (ec_seats, ecs_seats, ec_price, ecs_price)."""
    dur_m = re.search(r'\d+\s+h\s+\d+\s+min', row_text)
    seat_section = row_text[dur_m.end():] if dur_m else row_text
    lines = [l.strip() for l in seat_section.splitlines() if l.strip()]

    cols_data = []   # list of (count, price_str)
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.upper() == "N/A":
            cols_data.append((0, "N/A"))
            i += 1

        elif re.match(r'^\d+\s+Rs\.', line, re.I):
            parts = line.split(None, 1)
            cols_data.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
            i += 1

        elif re.match(r'^\d+$', line):
            count = int(line)
            if i + 1 < len(lines) and re.match(r'^Rs\.', lines[i + 1], re.I):
                cols_data.append((count, lines[i + 1]))
                i += 2
            else:
                cols_data.append((0, "Sold Out"))
                i += 1

        elif re.match(r'^Rs\.', line, re.I):
            i += 1

        elif line.lower() == "booking":
            break

        else:
            i += 1

    def _get(idx):
        if idx < len(cols_data):
            return cols_data[idx]
        return (0, "N/A")

    ec_count,  ec_price  = _get(EC_COL_INDEX)
    ecs_count, ecs_price = _get(ECS_COL_INDEX)
    return ec_count, ecs_count, ec_price, ecs_price


# ──────────────────────────────────────────────
# TRAIN NAME + TIMING EXTRACTOR
# ──────────────────────────────────────────────

_KNOWN_STATIONS = {
    "karachi", "cantt", "sadikabad", "sadiqabad", "lahore",
    "peshawar", "quetta", "rawalpindi", "multan", "faisalabad",
    "hyderabad", "sukkur", "larkana", "nawabshah",
}


def extract_train_info(row_text: str) -> dict:
    """Extract train name, departure time, arrival time, duration."""
    cleaned = re.sub(r'^\s*\d+\w+\s+', '', row_text.strip())

    name_parts = []
    for word in cleaned.split():
        if re.match(r'\d{1,2}:\d{2}', word):
            break
        if word.upper() == word and word.lower() in _KNOWN_STATIONS:
            break
        if word.upper() == word and len(word) > 3:
            break
        name_parts.append(word)
    name = " ".join(name_parts).strip() or cleaned[:40]

    times = re.findall(r'\d{1,2}:\d{2}', row_text)
    dep_time = times[0] if len(times) > 0 else "—"
    arr_time = times[1] if len(times) > 1 else "—"

    dur_m = re.search(r'(\d+\s+h\s+\d+\s+min)', row_text)
    duration = dur_m.group(1) if dur_m else "—"

    # +1 day indicator
    next_day = "+1" in row_text or "+1 day" in row_text.lower()

    return {
        "name":     name,
        "dep":      dep_time,
        "arr":      f"{arr_time}{' (+1)' if next_day else ''}",
        "duration": duration,
    }


# ──────────────────────────────────────────────
# ROW PARSING
# ──────────────────────────────────────────────

def train_passes_filter(row_lower: str) -> bool:
    if not TARGET_TRAINS:
        return True
    return any(t in row_lower for t in TARGET_TRAINS)


def parse_row_text(row_text: str) -> dict | None:
    lower = row_text.lower()
    if "booking" not in lower:
        return None
    if not train_passes_filter(lower):
        return None

    ec, ecs, ec_price, ecs_price = get_ec_ecs_with_price(row_text)

    if ec == 0 and ecs == 0:
        return None

    info = extract_train_info(row_text)
    print(f"[FOUND] ✅ {info['name']} | Dep: {info['dep']} → Arr: {info['arr']}"
          f" | EC: {ec} ({ec_price})  ECS: {ecs} ({ecs_price})")

    return {
        "name":        info["name"],
        "dep":         info["dep"],
        "arr":         info["arr"],
        "duration":    info["duration"],
        "ec_seats":    ec,
        "ecs_seats":   ecs,
        "ec_price":    ec_price,
        "ecs_price":   ecs_price,
        "booking_url": SEARCH_URL,
    }


# ──────────────────────────────────────────────
# SCRAPING
# ──────────────────────────────────────────────

def scrape(driver: webdriver.Chrome) -> list[dict]:
    print(f"[INFO] Loading: {SEARCH_URL}")
    driver.get(SEARCH_URL)
    wait_for_spa(driver)

    body_text = driver.find_element(By.TAG_NAME, "body").text
    print(f"[DEBUG] Page snippet (first 1000 chars):\n{body_text[:1000]}")
    print("─" * 60)

    found = []
    seen  = set()

    # ── Strategy 1: <tr> elements ─────────────────────────────────
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

    # ── Strategy 2: split on train-number markers ──────────────────
    if not found:
        print("[INFO] No <tr> hits — splitting body text on train numbers")
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

    # ── Diagnostic warnings ────────────────────────────────────────
    if not found and TARGET_TRAINS:
        page_lower = body_text.lower()
        missing = [t for t in TARGET_TRAINS if t not in page_lower]
        if missing:
            print("[WARN] Target train(s) not found on page for "
                  f"{TRAVEL_DATE}: " + ", ".join(t.title() for t in missing))
            print("[WARN] These trains may not run on this date.")

    return found


# ──────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────

def send_alert(available: list[dict]):
    total_ec  = sum(t["ec_seats"]  for t in available)
    total_ecs = sum(t["ecs_seats"] for t in available)
    total     = total_ec + total_ecs

    subject = (
        f"🚨 {total} ECONOMY SEATS AVAILABLE — "
        f"{FROM_STATION_NAME} → {TO_STATION_NAME} | {TRAVEL_DATE}"
    )

    rows_html = ""
    for t in available:

        def seat_cell(count, price):
            if count:
                return (
                    f"<span style='color:#15803d;font-weight:700;font-size:17px;'>"
                    f"{count}</span>"
                    f"<br><span style='color:#6b7280;font-size:11px;'>{price}</span>"
                )
            return "<span style='color:#d1d5db;font-size:13px;'>—</span>"

        rows_html += f"""
        <tr>
          <td style="padding:14px 16px;border:1px solid #e5e7eb;">
            <strong style="font-size:15px;">{t['name']}</strong><br>
            <span style="font-size:12px;color:#6b7280;">
              🕐 {t['dep']} → {t['arr']} &nbsp;({t['duration']})
            </span>
          </td>
          <td style="padding:14px 16px;border:1px solid #e5e7eb;text-align:center;">
            {seat_cell(t['ec_seats'], t['ec_price'])}
          </td>
          <td style="padding:14px 16px;border:1px solid #e5e7eb;text-align:center;">
            {seat_cell(t['ecs_seats'], t['ecs_price'])}
          </td>
          <td style="padding:14px 16px;border:1px solid #e5e7eb;text-align:center;">
            <a href="{t['booking_url']}"
               style="display:inline-block;background:#dc2626;color:#fff;
                      padding:10px 20px;border-radius:8px;text-decoration:none;
                      font-size:13px;font-weight:700;letter-spacing:.3px;">
              BOOK NOW →
            </a>
          </td>
        </tr>"""

    now_utc  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    now_pkt  = datetime.now(timezone.utc).strftime("%H:%M")   # rough PKT ref

    html = f"""
<html><body style="font-family:Arial,sans-serif;background:#fef2f2;padding:32px 16px;margin:0;">
  <div style="max-width:660px;margin:auto;background:#fff;border-radius:16px;
              box-shadow:0 4px 24px rgba(0,0,0,.10);overflow:hidden;">

    <!-- HEADER -->
    <div style="background:linear-gradient(135deg,#dc2626,#b91c1c);color:#fff;padding:30px;">
      <div style="font-size:36px;margin-bottom:8px;">🚨🚂</div>
      <h2 style="margin:0;font-size:26px;font-weight:800;">Economy Seats Available!</h2>
      <p style="margin:10px 0 0;opacity:.9;font-size:15px;">
        <strong>{FROM_STATION_NAME}</strong> &rarr;
        <strong>{TO_STATION_NAME}</strong>
        &nbsp;|&nbsp; {TRAVEL_DATE} &nbsp;|&nbsp; Economy Class
      </p>
    </div>

    <!-- URGENCY BANNER -->
    <div style="background:#fef9c3;border-bottom:1px solid #fde047;
                padding:12px 24px;font-size:14px;color:#854d0e;font-weight:600;">
      ⚡ {total} seat(s) found across {len(available)} train(s) —
      <span style="color:#dc2626;">Book immediately before they fill up!</span>
    </div>

    <!-- BODY -->
    <div style="padding:28px;">
      <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       text-align:left;font-size:12px;color:#6b7280;
                       text-transform:uppercase;letter-spacing:.5px;">Train</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:12px;color:#6b7280;
                       text-transform:uppercase;letter-spacing:.5px;">EC Seats</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:12px;color:#6b7280;
                       text-transform:uppercase;letter-spacing:.5px;">ECS Seats</th>
            <th style="padding:10px 16px;border:1px solid #e5e7eb;
                       font-size:12px;color:#6b7280;
                       text-transform:uppercase;letter-spacing:.5px;">Action</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>

      <!-- BOOKING TIPS -->
      <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:10px;
                  padding:16px 18px;font-size:13px;color:#166534;margin-bottom:20px;">
        <strong>📋 To complete booking you need:</strong><br>
        &bull; Your CNIC number<br>
        &bull; CNIC-registered mobile number<br>
        &bull; Online payment method (credit/debit card or JazzCash/EasyPaisa)
      </div>

      <!-- DIRECT LINK -->
      <div style="text-align:center;margin-bottom:24px;">
        <a href="{SEARCH_URL}"
           style="display:inline-block;background:#15803d;color:#fff;
                  padding:14px 36px;border-radius:10px;text-decoration:none;
                  font-size:16px;font-weight:800;letter-spacing:.5px;">
          🎫 Go to RABTA Booking Page
        </a>
      </div>

      <!-- FOOTER -->
      <p style="margin:0;font-size:11px;color:#9ca3af;
                border-top:1px solid #f3f4f6;padding-top:14px;text-align:center;">
        Sent by GitHub Actions seat-watcher &bull; {now_utc} UTC &bull;
        Checks every 15 minutes automatically
      </p>
    </div>
  </div>
</body></html>"""

    _send_gmail(subject, html)
    print(f"[EMAIL] ✅ Alert sent to {ALERT_TO}")


def send_no_seats_summary(checked_trains: list[str]):
    """Send a daily digest at midnight if no seats found all day (optional)."""
    pass   # reserved for future use


def send_error_email(err: str):
    subj = "⚠️ Pakistan Rail Checker — Script Error"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    html = f"""
<html><body style="font-family:Arial,sans-serif;padding:24px;">
  <h3 style="color:#dc2626;">⚠️ Seat Checker Crashed</h3>
  <p>Run time: <strong>{now_utc} UTC</strong></p>
  <pre style="background:#fef2f2;padding:14px;border-radius:8px;
              font-size:12px;overflow:auto;border:1px solid #fca5a5;">{err}</pre>
  <p style="font-size:12px;color:#6b7280;">
    Check your GitHub Actions logs for full details.
  </p>
</body></html>"""
    try:
        _send_gmail(subj, html)
        print(f"[EMAIL] Error report sent to {ALERT_TO}")
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
    print(f"  Pakistan Railway (RABTA) Seat Checker")
    print(f"  Run time : {now_utc} UTC")
    print(f"  Route    : {FROM_STATION_NAME} ({FROM_STATION_CODE})"
          f" → {TO_STATION_NAME} ({TO_STATION_CODE})")
    print(f"  Date     : {TRAVEL_DATE}  |  Class: Economy (EC + ECS)")
    print(f"  Trains   : {train_label}")
    print(f"  URL      : {SEARCH_URL}")
    print(f"{'='*60}\n")

    driver = None
    try:
        driver    = make_driver()
        available = scrape(driver)

        if available:
            total = sum(t["ec_seats"] + t["ecs_seats"] for t in available)
            send_alert(available)
            print(f"\n✅ {len(available)} train(s) | {total} total Economy "
                  f"seats — alert sent to {ALERT_TO}!")
        else:
            print("\n❌ No Economy seats right now. Will check again in 15 min.")

    except Exception:
        err = traceback.format_exc()
        print(f"\n[CRITICAL]\n{err}")
        send_error_email(err)
        raise

    finally:
        if driver:
            driver.quit()
            print("[INFO] Browser closed.")


if __name__ == "__main__":
    main()
