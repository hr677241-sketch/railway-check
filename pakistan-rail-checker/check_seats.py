"""
Pakistan Railway Seat Availability Checker
Route  : Karachi → Sadikabad
Trains : Khyber Mail | Fareed Express | Bahauddin Zakria Express
Class  : Economy
Date   : 2026-05-23
Alert  : Gmail (hr677241@gmail.com)
"""

import os
import re
import smtplib
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ──────────────────────────────────────────────
# CONFIGURATION  (all secrets come from GitHub Secrets / env vars)
# ──────────────────────────────────────────────
GMAIL_USER     = os.environ["GMAIL_USER"]        # your Gmail address
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"] # Gmail App Password
ALERT_TO       = "hr677241@gmail.com"

TRAVEL_DATE    = "2026-05-23"          # YYYY-MM-DD
FROM_STATION   = "Karachi"             # display name for email
TO_STATION     = "Sadikabad"
FROM_CODE      = "KCI"                 # Pakistan Rail station code
TO_CODE        = "SDB"

TARGET_TRAINS  = [
    "khyber mail",
    "fareed express",
    "bahauddin zakria express",
]
TARGET_CLASS   = "economy"

# Pakistan Railway e-ticketing base URL
BASE_URL       = "https://eticketing.pakrail.gov.pk"
SEARCH_URL     = f"{BASE_URL}/Booking/SearchTrains"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def get_session():
    """Return a requests session with cookies from the landing page."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(BASE_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] Could not load landing page: {e}")
    return session


def extract_viewstate(html: str) -> dict:
    """Pull ASP.NET hidden fields needed for POST."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__RequestVerificationToken"]:
        tag = soup.find("input", {"name": name})
        if tag:
            fields[name] = tag.get("value", "")
    # Also look for a token in a meta tag (some ASP.NET Core apps)
    meta = soup.find("meta", {"name": "RequestVerificationToken"})
    if meta:
        fields["__RequestVerificationToken"] = meta.get("content", "")
    return fields


def search_trains(session: requests.Session) -> list[dict]:
    """
    Submit the search form and return a list of available train dicts:
      { name, departure, arrival, economy_seats, booking_url }
    """
    # Step 1 – load the search page to harvest hidden fields
    try:
        page = session.get(SEARCH_URL, timeout=30)
        hidden = extract_viewstate(page.text)
    except Exception as e:
        print(f"[ERROR] Search page fetch failed: {e}")
        return []

    # Step 2 – format date as the site expects (dd-MM-yyyy or dd/MM/yyyy)
    try:
        dt = datetime.strptime(TRAVEL_DATE, "%Y-%m-%d")
        date_param = dt.strftime("%d/%m/%Y")
    except ValueError:
        date_param = TRAVEL_DATE

    payload = {
        **hidden,
        "FromStation":  FROM_CODE,
        "ToStation":    TO_CODE,
        "JourneyDate":  date_param,
        "Quota":        "GN",   # General quota
        "Class":        "ECO",  # Economy
        "AdultCount":   "1",
        "ChildCount":   "0",
    }

    try:
        resp = session.post(SEARCH_URL, data=payload, timeout=45)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] POST to search failed: {e}")
        return []

    return parse_results(resp.text)


def parse_results(html: str) -> list[dict]:
    """Parse the results page and return matching trains."""
    soup = BeautifulSoup(html, "html.parser")
    found = []

    # Try to find train result cards/rows – adjust selectors if site changes layout
    # Common patterns on the PR portal
    train_blocks = (
        soup.select(".train-card")
        or soup.select(".train-result")
        or soup.select("table.trains-table tbody tr")
        or soup.select("[class*='train']")
    )

    if not train_blocks:
        # Fallback: search for any table rows
        train_blocks = soup.select("tr")

    for block in train_blocks:
        text = block.get_text(" ", strip=True).lower()

        # Check if this row is for one of our target trains
        matched_train = next((t for t in TARGET_TRAINS if t in text), None)
        if not matched_train:
            continue

        # Check economy seats
        if TARGET_CLASS not in text:
            continue

        # Try to find seat count (digits near keywords like "available", "seats", "eco")
        seat_count = extract_seat_count(block)
        if seat_count is None or seat_count == 0:
            print(f"[INFO] {matched_train.title()} found but 0 / unknown seats.")
            continue

        # Pull booking URL if present
        link_tag = block.find("a", href=True)
        booking_url = BASE_URL + link_tag["href"] if link_tag else SEARCH_URL

        found.append({
            "name":         matched_train.title(),
            "economy_seats": seat_count,
            "booking_url":  booking_url,
            "raw_text":     block.get_text(" ", strip=True)[:300],
        })
        print(f"[FOUND] {matched_train.title()} → {seat_count} Economy seat(s) available!")

    return found


def extract_seat_count(tag) -> int | None:
    """Try multiple heuristics to find available seat count in a block."""
    text = tag.get_text(" ", strip=True)

    # Pattern: "Available: 23" or "Seats: 5" or just a standalone number
    patterns = [
        r"available[:\s]+(\d+)",
        r"seats?[:\s]+(\d+)",
        r"eco(?:nomy)?[:\s]+(\d+)",
        r"\b(\d{1,3})\b",   # last-resort: first short number
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


# ──────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────

def send_email(available: list[dict]):
    """Send a Gmail alert with the list of available trains."""
    subject = f"🚂 Pakistan Railway Seats FOUND! {FROM_STATION} → {TO_STATION} on {TRAVEL_DATE}"

    rows = ""
    for t in available:
        rows += f"""
        <tr>
          <td style="padding:8px 12px;border:1px solid #ddd;">{t['name']}</td>
          <td style="padding:8px 12px;border:1px solid #ddd;text-align:center;
                     color:#16a34a;font-weight:bold;">{t['economy_seats']}</td>
          <td style="padding:8px 12px;border:1px solid #ddd;">
            <a href="{t['booking_url']}" style="color:#2563eb;">Book Now</a>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif;background:#f8fafc;padding:24px;">
      <div style="max-width:600px;margin:auto;background:#fff;border-radius:10px;
                  box-shadow:0 2px 8px rgba(0,0,0,.08);overflow:hidden;">
        <div style="background:#16a34a;color:#fff;padding:20px 24px;">
          <h2 style="margin:0;">🚂 Seats Available!</h2>
          <p style="margin:4px 0 0;">{FROM_STATION} → {TO_STATION} &nbsp;|&nbsp; {TRAVEL_DATE} &nbsp;|&nbsp; Economy Class</p>
        </div>
        <div style="padding:24px;">
          <p>Great news! Economy seats are available on the following train(s):</p>
          <table style="width:100%;border-collapse:collapse;margin-top:12px;">
            <thead>
              <tr style="background:#f1f5f9;">
                <th style="padding:10px 12px;border:1px solid #ddd;text-align:left;">Train</th>
                <th style="padding:10px 12px;border:1px solid #ddd;">Economy Seats</th>
                <th style="padding:10px 12px;border:1px solid #ddd;">Action</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
          <p style="margin-top:20px;font-size:13px;color:#6b7280;">
            ⚡ Book quickly — seats fill fast!<br>
            This alert was sent by your GitHub Actions seat-watcher.
          </p>
        </div>
      </div>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = ALERT_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASSWORD)
        smtp.sendmail(GMAIL_USER, ALERT_TO, msg.as_string())
    print(f"[EMAIL] Alert sent to {ALERT_TO}")


def send_error_email(error_msg: str):
    """Send a brief error notification so you know the checker is broken."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "⚠️ Pakistan Rail Checker – Error"
    msg["From"]    = GMAIL_USER
    msg["To"]      = ALERT_TO
    body = f"<p>The seat checker encountered an error:</p><pre>{error_msg}</pre>"
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASSWORD)
            smtp.sendmail(GMAIL_USER, ALERT_TO, msg.as_string())
    except Exception:
        pass   # don't crash on error-email failure


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"Pakistan Railway Seat Checker  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Route : {FROM_STATION} → {TO_STATION}")
    print(f"Date  : {TRAVEL_DATE}  |  Class: Economy")
    print(f"Trains: {', '.join(t.title() for t in TARGET_TRAINS)}")
    print(f"{'='*55}\n")

    try:
        session = get_session()
        available = search_trains(session)

        if available:
            send_email(available)
            print(f"\n✅ {len(available)} train(s) found. Email alert sent!")
        else:
            print("\n❌ No Economy seats found on target trains. Will check again next run.")

    except Exception as e:
        error = str(e)
        print(f"\n[CRITICAL ERROR] {error}")
        try:
            send_error_email(error)
        except Exception:
            pass
        raise   # re-raise so GitHub Actions marks the run as failed


if __name__ == "__main__":
    main()
