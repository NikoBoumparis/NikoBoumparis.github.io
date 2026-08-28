#!/usr/bin/env python3
"""Send a notification email after new publications were added to the site.

Reads new_entries.json (written by update_publications.py) and, if it's
non-empty, emails a summary via Gmail SMTP using a Gmail App Password.

Required env vars:
    GMAIL_USER          the sending Gmail address
    GMAIL_APP_PASSWORD  a Gmail App Password for that account
"""
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

TO_ADDR = "nikoblablub@gmail.com"
SITE_URL = "https://www.boumparis.com"
NEW_ENTRIES_JSON = Path(__file__).resolve().parent / "new_entries.json"


def main():
    if not NEW_ENTRIES_JSON.exists():
        print("No new_entries.json found — nothing to notify.")
        return 0

    added = json.loads(NEW_ENTRIES_JSON.read_text(encoding="utf-8"))
    if not added:
        print("No new entries — skipping notification email.")
        return 0

    username = os.environ.get("GMAIL_USER")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not username or not app_password:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email.", file=sys.stderr)
        return 1

    lines = [
        f"{len(added)} new publication(s) were added to {SITE_URL} from your ORCID record.",
        "Please check the formatting and correct anything that looks off.",
        "",
    ]
    for entry in added:
        lines.append(f"- {entry['title']} ({entry['year']}), {entry['journal']}")
        lines.append(f"  {entry['doi_url']}")
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = "Website publications updated — please check"
    msg["From"] = username
    msg["To"] = TO_ADDR
    msg.set_content(body)

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
        s.login(username, app_password)
        s.send_message(msg)

    print(f"Notification email sent to {TO_ADDR}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
