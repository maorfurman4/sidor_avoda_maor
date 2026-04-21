#!/usr/bin/env python3
"""
Academic Agentic Orchestrator
Syncs Gmail (TeachingBox) → Google Calendar → Telegram
"""

import os
import json
import base64
import re
import pytz
import anthropic
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import requests

# ─── Config ────────────────────────────────────────────────────────────────
ISRAEL_TZ = pytz.timezone("Asia/Jerusalem")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LECTURER_MAP = {
    "רמי אלקיים": "מעבדת מכניקה",
    "גבריאל בן סימון": "מדר (הרצאה)",
    "בועז ויינר": "מדר (תרגול)",
    "בועז": "מדר (תרגול)",
    "יוני": "חדו\"א (הרצאה)",
    "אלינה ליס": "חדו\"א (תרגול)",
    "אלינה": "חדו\"א (תרגול)",
    "אפרת אבישי": "פיזיקה חשמל (הרצאה)",
    "אבישי": "פיזיקה חשמל (הרצאה)",
    "אילנה": "פיזיקה חשמל (תרגול)",
    "ד\"ר גבי שפט": "הנדסת חשמל א' (הרצאה + תרגול)",
    "גבי שפט": "הנדסת חשמל א' (הרצאה + תרגול)",
    "ערן לבינגר": "מערכות ספרתיות (הרצאה + תרגול)",
}

CANCEL_KEYWORDS = ["ביטול", "לא יתקיים", "מבוטל", "לא תתקיים"]
DELAY_KEYWORDS = ["עיכוב", "מתעכב", "מתעכבת", "איחור"]


# ─── Shabbat Freeze ─────────────────────────────────────────────────────────
def is_shabbat_freeze() -> bool:
    now = datetime.now(ISRAEL_TZ)
    weekday = now.weekday()  # 4=Friday, 5=Saturday
    if weekday == 4 and now.hour >= 14:
        return True
    if weekday == 5 and now.hour < 20:
        return True
    return False


# ─── Google Auth ─────────────────────────────────────────────────────────────
def get_google_services():
    raw = os.environ["GOOGLE_CREDENTIALS"]
    creds_data = json.loads(raw)
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/calendar",
    ]
    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    gmail = build("gmail", "v1", credentials=creds)
    calendar = build("calendar", "v3", credentials=creds)
    return gmail, calendar


# ─── Gmail Fetch ─────────────────────────────────────────────────────────────
def fetch_teachingbox_emails(gmail) -> list[dict]:
    result = gmail.users().messages().list(
        userId="me",
        labelIds=["UNREAD"],
        q='label:TeachingBox',
        maxResults=20,
    ).execute()
    messages = result.get("messages", [])
    emails = []
    for msg in messages:
        full = gmail.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        body = extract_body(full["payload"])
        emails.append({
            "id": msg["id"],
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": body,
        })
    return emails


def extract_body(payload) -> str:
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return ""


def mark_as_read(gmail, msg_id: str):
    gmail.users().messages().modify(
        userId="me", id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# ─── AI Analysis ─────────────────────────────────────────────────────────────
def analyze_email_with_claude(email: dict) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    lecturer_list = "\n".join(f"- {k} → {v}" for k, v in LECTURER_MAP.items())
    prompt = f"""אתה עוזר אקדמי. נתח את האימייל הבא וזהה:
1. סוג הפעולה: "cancellation" | "update" | "delay" | "ignore"
2. שם המרצה (בדיוק כפי שמופיע)
3. שם הקורס (לפי מיפוי המרצים)
4. תאריך ושעה של השיעור המושפע (ISO 8601 אם אפשר)
5. פרטי השינוי (חדר חדש, שיעור השלמה וכו')

מיפוי מרצים:
{lecturer_list}

אימייל:
נושא: {email['subject']}
שולח: {email['from']}
תאריך: {email['date']}
גוף:
{email['body'][:2000]}

ענה JSON בלבד, ללא טקסט נוסף:
{{
  "action": "cancellation|update|delay|ignore",
  "lecturer": "שם המרצה",
  "course": "שם הקורס",
  "event_datetime": "ISO8601 or null",
  "change_details": "תיאור השינוי"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("```").strip()
    return json.loads(raw)


# ─── Calendar Operations ──────────────────────────────────────────────────────
def find_calendar_event(calendar, course: str, event_dt: str) -> dict | None:
    if not event_dt:
        return None
    try:
        dt = datetime.fromisoformat(event_dt).astimezone(ISRAEL_TZ)
    except Exception:
        return None
    time_min = (dt - timedelta(hours=2)).isoformat()
    time_max = (dt + timedelta(hours=2)).isoformat()
    events = calendar.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        q=course,
        singleEvents=True,
    ).execute().get("items", [])
    return events[0] if events else None


def delete_event(calendar, event_id: str):
    calendar.events().delete(calendarId="primary", eventId=event_id).execute()


def update_event(calendar, event: dict, change_details: str):
    event["description"] = (
        event.get("description", "") + f"\n\n📝 עדכון: {change_details}"
    )
    calendar.events().update(
        calendarId="primary", eventId=event["id"], body=event
    ).execute()


def add_event(calendar, course: str, event_dt: str, change_details: str):
    try:
        dt = datetime.fromisoformat(event_dt).astimezone(ISRAEL_TZ)
    except Exception:
        return
    end_dt = dt + timedelta(hours=2)
    event = {
        "summary": f"📚 {course}",
        "description": f"📝 {change_details}",
        "start": {"dateTime": dt.isoformat(), "timeZone": "Asia/Jerusalem"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Jerusalem"},
    }
    calendar.events().insert(calendarId="primary", body=event).execute()


# ─── Telegram ─────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


# ─── Orchestrator ─────────────────────────────────────────────────────────────
def format_date(iso: str | None) -> str:
    if not iso:
        return "לא ידוע"
    try:
        dt = datetime.fromisoformat(iso).astimezone(ISRAEL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return iso


def process_emails():
    if is_shabbat_freeze():
        print("🕯️ Shabbat freeze active — skipping.")
        return

    gmail, calendar = get_google_services()
    emails = fetch_teachingbox_emails(gmail)

    if not emails:
        print("No unread TeachingBox emails.")
        return

    for email in emails:
        print(f"Processing: {email['subject']}")
        try:
            analysis = analyze_email_with_claude(email)
        except Exception as e:
            print(f"Claude analysis failed: {e}")
            continue

        action = analysis.get("action", "ignore")
        course = analysis.get("course", "קורס לא ידוע")
        lecturer = analysis.get("lecturer", "")
        event_dt = analysis.get("event_datetime")
        details = analysis.get("change_details", "")
        date_str = format_date(event_dt)

        if action == "cancellation":
            event = find_calendar_event(calendar, course, event_dt)
            if event:
                delete_event(calendar, event["id"])
            msg = (
                f"⚠️ <b>הביטול בוצע:</b> {course}\n"
                f"📅 תאריך: {date_str}\n"
                f"הוסר מהיומן."
            )
            send_telegram(msg)

        elif action == "update":
            event = find_calendar_event(calendar, course, event_dt)
            if event:
                update_event(calendar, event, details)
            else:
                add_event(calendar, course, event_dt, details)
            msg = (
                f"✅ <b>עדכון בוצע ביומן:</b> {course}\n"
                f"📅 תאריך: {date_str}\n"
                f"📝 פרטי השינוי: {details}"
            )
            send_telegram(msg)

        elif action == "delay":
            msg = (
                f"⏳ <b>הודעת עיכוב:</b>\n"
                f"המרצה {lecturer} מתעכב בשיעור {course}.\n"
                f"📅 תאריך: {date_str}"
            )
            send_telegram(msg)

        else:
            print(f"Ignoring email (action={action})")

        mark_as_read(gmail, email["id"])


if __name__ == "__main__":
    process_emails()
