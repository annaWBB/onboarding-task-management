#!/usr/bin/env python3
"""
generate_status.py

Queries #onboarding-tasks in Slack, builds the current onboarding pipeline,
generates an HTML status page saved to output/onboarding-status.html,
and optionally sends a Slack DM to Anna with a plain-text summary.

Required env var:
  SLACK_TOKEN    — bot token (xoxb-) with scopes:
                   channels:history, groups:history, channels:read,
                   search:read, im:write, chat:write

Optional env vars:
  SEND_SLACK_DM  — set to 'true' to DM Anna (default: false)
"""

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── Config ─────────────────────────────────────────────────────────────────────
SLACK_TOKEN        = os.environ["SLACK_TOKEN"]
ANNA_USER_ID       = "U08R942H25B"
ONBOARDING_CHANNEL = "C0949617WP4"   # #onboarding-tasks
SEND_SLACK_DM      = os.environ.get("SEND_SLACK_DM", "false").lower() == "true"
OUTPUT_PATH        = Path(__file__).parent.parent / "output" / "onboarding-status.html"

client = WebClient(token=SLACK_TOKEN)
today  = datetime.now().date()


# ── Date helpers ───────────────────────────────────────────────────────────────

def parse_date(s: str) -> datetime | None:
    """Parse loose date strings like '5/4', '5/4/26', 'May 4'."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m/%d", "%B %d, %Y", "%b %d"):
        try:
            d = datetime.strptime(s, fmt)
            if d.year == 1900:
                d = d.replace(year=today.year)
            # Roll forward if the date already passed this year
            if d.date() < today - timedelta(days=180):
                d = d.replace(year=d.year + 1)
            return d
        except ValueError:
            continue
    return None


def thursday_before(start: datetime) -> datetime:
    """Return the Thursday of the week before a given start date."""
    # weekday(): Monday=0 … Sunday=6. Thursday=3.
    days_back = (start.weekday() - 3) % 7
    if days_back == 0:
        days_back = 7
    return start - timedelta(days=days_back)


def days_until(d: datetime) -> int:
    return (d.date() - today).days


# ── Slack helpers ──────────────────────────────────────────────────────────────

def channel_history(channel: str, limit: int = 100) -> list[dict]:
    try:
        resp = client.conversations_history(channel=channel, limit=limit)
        return resp["messages"]
    except SlackApiError as e:
        print(f"Error fetching channel history: {e}", file=sys.stderr)
        return []


def thread_replies(channel: str, ts: str) -> list[dict]:
    try:
        resp = client.conversations_replies(channel=channel, ts=ts)
        return resp["messages"][1:]  # skip parent
    except SlackApiError as e:
        print(f"Error fetching thread {ts}: {e}", file=sys.stderr)
        return []


def strip_slack_markup(text: str) -> str:
    """Remove mention tags, links, and other Slack mrkdwn."""
    text = re.sub(r"<@[A-Z0-9]+\|?[^>]*>", "", text)
    text = re.sub(r"<https?://[^|>]*\|?([^>]*)>", r"\1", text)
    return text.strip()


def is_strikethrough(text: str, name: str) -> bool:
    """Check if a name appears struck-through (~name~) in Slack mrkdwn."""
    # Slack uses ~text~ for strikethrough
    pattern = re.compile(r"~+([^~]+)~+")
    for match in pattern.finditer(text):
        if name.lower() in match.group(1).lower():
            return True
    return False


def text_contains(text: str, *keywords) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


# ── Pipeline parsing ───────────────────────────────────────────────────────────

class Person:
    def __init__(self, name: str, role: str, start: datetime, cohort_type: str = "core"):
        self.name         = name
        self.role         = role
        self.start        = start
        self.cohort_type  = cohort_type   # "core" | "convert"
        self.laptop       = "unknown"     # "ordered" | "not_ordered" | "na" | "unknown"
        self.laptop_note  = ""
        self.thu_email    = "unknown"     # "sent" | "not_sent" | "na" | "unknown"
        self.thu_due      = thursday_before(start) if cohort_type == "core" else None
        self.notes        = []

    @property
    def urgency(self) -> str:
        d = days_until(self.start)
        if d < 0:
            return "started"
        if d <= 5:
            return "imminent"
        if d <= 14:
            return "soon"
        return "later"

    @property
    def thu_urgency(self) -> str:
        if self.thu_due is None:
            return "na"
        d = days_until(self.thu_due)
        if d < 0:
            return "overdue"
        if d == 0:
            return "today"
        if d == 1:
            return "tomorrow"
        return "upcoming"


def parse_cohort_thread(parent_text: str, replies: list[dict]) -> list[Person]:
    """
    Parse an 'Onboarding – [date]' thread.
    The first reply from Sean typically lists people, roles, and start dates.
    Strikethrough on a name (~Name~) signals laptop purchased.
    """
    people: list[Person] = []

    # Combine all text to look for names/roles
    all_text = parent_text + "\n" + "\n".join(r.get("text", "") for r in replies)

    # Pattern: bullet with name (start date) possibly struck through
    # Example: "• ~Rowan Wing (5/4)~ purchased\n  ◦ Forward Deployed Arch"
    bullet_re = re.compile(
        r"[•\-\*]\s*(~+)?([A-Z][A-Za-z'\-]+(?: [A-Z][A-Za-z'\-]+)+)(~+)?"
        r"(?:\s*\(([^)]+)\))?"
        r"([^\n]*)\n?"
        r"(?:\s+[◦\-]\s*(.+))?",
        re.MULTILINE,
    )

    for m in bullet_re.finditer(all_text):
        struck_open  = bool(m.group(1))
        name         = m.group(2).strip()
        struck_close = bool(m.group(3))
        date_str     = m.group(4) or ""
        suffix       = (m.group(5) or "").strip()
        role         = (m.group(6) or "").strip()

        # Skip short matches that are likely noise
        if len(name.split()) < 2:
            continue

        start = parse_date(date_str) if date_str else None
        if start is None:
            continue

        p = Person(name=name, role=role, start=start)

        # Laptop: struck-through name or "purchased" in the same line
        struck = struck_open or struck_close or is_strikethrough(all_text, name)
        if struck or text_contains(suffix, "purchased", "ordered"):
            p.laptop = "ordered"
        else:
            p.laptop = "not_ordered"

        people.append(p)

    # Enrich with reply signals
    for r in replies:
        text = r.get("text", "")
        for p in people:
            first = p.name.split()[0].lower()
            if first not in text.lower():
                continue

            # Address received → laptop can be ordered now
            if text_contains(text, "address", "shipping") and not text_contains(text, "no address", "no response", "still no"):
                if p.laptop == "not_ordered":
                    p.laptop_note = "address on file"

            # Laptop ordered confirmation
            if text_contains(text, "ordered", "purchased", "shipping info will be sent"):
                p.laptop = "ordered"

            # Laptop issues
            if text_contains(text, "no address", "no response", "not home", "traveling"):
                p.laptop_note = strip_slack_markup(text)[:120]
                if p.laptop != "ordered":
                    p.laptop = "not_ordered"

            # Thursday email sent
            if text_contains(text, "email sent", "welcome email sent", "thursday email"):
                p.thu_email = "sent"

            # General notes
            note_triggers = ["⚠️", "issue", "blocked", "no response", "vault", "1password"]
            if any(t in text.lower() for t in note_triggers):
                clean = strip_slack_markup(text)[:160]
                if clean and clean not in p.notes:
                    p.notes.append(clean)

    return people


def parse_convert_thread(text: str, replies: list[dict]) -> Person | None:
    """
    Parse 'Name - convert to Core' threads.
    Example: 'Sri Velagapudi - convert to Core'
    """
    m = re.match(r"\*?([A-Z][A-Za-z'\- ]+?)\s*[-–]\s*convert", text, re.IGNORECASE)
    if not m:
        return None

    name = m.group(1).strip()
    # Start date: look for it in replies or default to today
    start = None
    for r in replies:
        ds = re.search(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b", r.get("text", ""))
        if ds:
            start = parse_date(ds.group(1))
            break
    if start is None:
        start = datetime.now()

    p = Person(name=name, role="Member → Core", start=start, cohort_type="convert")
    p.thu_email = "na"

    all_text = text + "\n" + "\n".join(r.get("text", "") for r in replies)

    if text_contains(all_text, "laptop ordered", "i just ordered", "ordered your laptop"):
        p.laptop = "ordered"
    elif text_contains(all_text, "no laptop", "doesn't have", "does not have"):
        p.laptop = "not_ordered"
    else:
        p.laptop = "not_ordered"

    if text_contains(all_text, "no response", "still no", "no address"):
        p.laptop_note = "No address response"
        p.notes.append("Emailed for shipping address — no reply yet")

    return p


def parse_individual_laptop_thread(title: str, replies: list[dict]) -> tuple[str, str]:
    """
    Parse 'Name Laptop' threads. Returns (name, laptop_status).
    """
    m = re.match(r"\*?([A-Z][A-Za-z'\- ]+?)\s+Laptop\b", title)
    if not m:
        return "", "unknown"

    name   = m.group(1).strip()
    status = "not_ordered"
    all_text = title + "\n" + "\n".join(r.get("text", "") for r in replies)

    if text_contains(all_text, "ordered", "purchased", "on its way", "2 day shipping"):
        status = "ordered"

    return name, status


# ── Main pipeline builder ──────────────────────────────────────────────────────

def build_pipeline() -> list[Person]:
    messages = channel_history(ONBOARDING_CHANNEL, limit=100)
    people_map: dict[str, Person] = {}     # name → Person
    laptop_overrides: dict[str, str] = {}  # name → "ordered" | "not_ordered"

    for msg in messages:
        text  = msg.get("text", "")
        ts    = msg.get("ts", "")
        clean = strip_slack_markup(text)

        # ── Cohort thread: "Onboarding - 5/4" ────────────────────────────────
        if re.search(r"Onboarding\s*[-–]\s*\d{1,2}/\d{1,2}", clean, re.IGNORECASE):
            replies = thread_replies(ONBOARDING_CHANNEL, ts)
            for p in parse_cohort_thread(clean, replies):
                # Deduplicate by name; keep most recent if duplicate
                if p.name not in people_map:
                    people_map[p.name] = p
                else:
                    # Merge: prefer newer, more complete data
                    existing = people_map[p.name]
                    if p.laptop == "ordered":
                        existing.laptop = "ordered"
                    existing.notes.extend(p.notes)

        # ── "Members converted to Core" thread ───────────────────────────────
        elif re.search(r"Members converted to Core", clean, re.IGNORECASE):
            replies = thread_replies(ONBOARDING_CHANNEL, ts)
            # Names listed in thread body
            for r in [msg] + replies:
                rtext = r.get("text", "")
                name_m = re.findall(r"<@[A-Z0-9]+\|([A-Za-z'\- ]+)>", rtext)
                for raw in name_m:
                    name = raw.strip().title()
                    if name and name not in people_map:
                        p = Person(name=name, role="Member → Core",
                                   start=datetime.now(), cohort_type="convert")
                        p.thu_email = "na"
                        people_map[name] = p

        # ── Individual convert thread: "Name - convert to Core" ──────────────
        elif re.search(r"convert to Core", clean, re.IGNORECASE):
            replies = thread_replies(ONBOARDING_CHANNEL, ts)
            p = parse_convert_thread(clean, replies)
            if p and p.name not in people_map:
                people_map[p.name] = p
            elif p:
                existing = people_map[p.name]
                if p.laptop == "ordered":
                    existing.laptop = "ordered"
                existing.notes.extend(p.notes)

        # ── Individual laptop thread: "Name Laptop" ───────────────────────────
        elif re.search(r"[A-Z][A-Za-z'\- ]+ Laptop\b", clean):
            replies = thread_replies(ONBOARDING_CHANNEL, ts)
            name, status = parse_individual_laptop_thread(clean, replies)
            if name:
                laptop_overrides[name] = status

    # Apply laptop overrides from dedicated threads
    for name, status in laptop_overrides.items():
        for pname, p in people_map.items():
            if name.lower() in pname.lower() or pname.lower() in name.lower():
                p.laptop = status

    # Sort: soonest start first, converts at end
    pipeline = sorted(
        people_map.values(),
        key=lambda p: (p.cohort_type == "convert", p.start),
    )

    return pipeline


# ── HTML generation ────────────────────────────────────────────────────────────

LAPTOP_ICON  = {"ordered": "✅", "not_ordered": "🟠", "unknown": "⬜", "na": "—"}
EMAIL_ICON   = {"sent": "✅", "not_sent": "🔴", "unknown": "⬜", "na": "—", "overdue": "🔴"}

START_COLOR  = {"imminent": "#fef3c7|#92400e", "soon": "#e0f2fe|#0369a1",
                "later": "#f3f4f6|#555555", "started": "#f0f0f0|#777777",
                "convert": "#ede9fe|#5b21b6"}

def urgency_label(p: Person) -> str:
    d = days_until(p.start)
    if p.cohort_type == "convert":
        return f"Converted {p.start.strftime('%-m/%-d')}"
    if d < 0:
        return f"Started {p.start.strftime('%-m/%-d')}"
    if d == 0:
        return "Starts today"
    if d == 1:
        return "Starts tomorrow"
    return f"Starts {p.start.strftime('%-m/%-d')}"


def laptop_cell(p: Person) -> str:
    icon = LAPTOP_ICON.get(p.laptop, "⬜")
    label = {"ordered": "Laptop ordered", "not_ordered": "Laptop — not ordered",
             "na": "Laptop N/A", "unknown": "Laptop unknown"}.get(p.laptop, "Laptop")
    detail = p.laptop_note or ""
    css = {"ordered": "done", "not_ordered": "warn", "unknown": "todo", "na": "done"}.get(p.laptop, "todo")
    return f"""<div class="task {css}">
      <span class="icon">{icon}</span>
      <div class="task-content">
        <div class="label">{label}</div>
        {"<div class='detail'>" + detail + "</div>" if detail else ""}
      </div>
    </div>"""


def email_cell(p: Person) -> str:
    if p.thu_email == "na" or p.thu_due is None:
        return """<div class="task done">
      <span class="icon">—</span>
      <div class="task-content"><div class="label">Thursday email N/A</div></div>
    </div>"""

    tu = p.thu_urgency
    icon = {"sent": "✅", "not_sent": "🔴", "overdue": "🔴",
            "today": "🔴", "tomorrow": "🔴", "upcoming": "⬜"}.get(
        tu if p.thu_email != "sent" else "sent", "⬜")

    if p.thu_email == "sent":
        label  = "Thursday email sent"
        detail = ""
        css    = "done"
    else:
        due_str = p.thu_due.strftime("%-m/%-d")
        label   = "Thursday email"
        css     = "todo"
        if tu in ("today", "tomorrow", "overdue"):
            css    = "danger"
            detail = f"Due {tu} ({due_str})"
        else:
            detail = f"Due {due_str}"

    return f"""<div class="task {css}">
      <span class="icon">{icon}</span>
      <div class="task-content">
        <div class="label">{label}</div>
        {"<div class='detail'>" + detail + "</div>" if detail else ""}
      </div>
    </div>"""


def badge(p: Person) -> str:
    urg = p.cohort_type if p.cohort_type == "convert" else p.urgency
    bg, fg = START_COLOR.get(urg, "#f3f4f6|#555").split("|")
    return (f'<span class="start-badge" style="background:{bg};color:{fg}">'
            f'{urgency_label(p)}</span>')


def card_class(p: Person) -> str:
    if p.urgency == "imminent" and p.thu_email not in ("sent", "na"):
        return "card urgent"
    if p.laptop == "not_ordered" and p.laptop_note:
        return "card alert"
    return "card"


def render_html(pipeline: list[Person]) -> str:
    # Split active vs recently started
    active    = [p for p in pipeline if days_until(p.start) >= -7]
    completed = [p for p in pipeline if days_until(p.start) < -7]

    # Urgent alerts
    alerts = []
    for p in active:
        if p.thu_urgency in ("today", "tomorrow") and p.thu_email not in ("sent", "na"):
            due = "today" if p.thu_urgency == "today" else "tomorrow"
            alerts.append(f"<strong>{p.name}</strong> (starts {p.start.strftime('%-m/%-d')}) — Thursday email due {due}")

    alert_html = ""
    if alerts:
        items = " · ".join(alerts)
        alert_html = f"""<div class="alert-banner">⚠️ {items}</div>"""

    def person_card(p: Person) -> str:
        notes_html = ""
        if p.notes:
            notes_html = "<div class='notes'>" + "<br>".join(f"⚠️ {n}" for n in p.notes[:3]) + "</div>"
        return f"""
<div class="{card_class(p)}">
  <div class="card-header">
    <div class="person-info">
      <div class="person-name">{p.name}</div>
      <div class="person-meta">{p.role or "Role TBC"}</div>
    </div>
    {badge(p)}
  </div>
  <div class="tasks">
    {laptop_cell(p)}
    {email_cell(p)}
  </div>
  {notes_html}
</div>"""

    active_cards    = "\n".join(person_card(p) for p in active)
    completed_cards = "\n".join(person_card(p) for p in completed) if completed else "<p style='color:#aaa;font-size:13px'>None yet.</p>"

    generated = datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Onboarding Status</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f4f1; color: #1a1a1a; padding: 32px 24px;
         max-width: 960px; margin: 0 auto; }}
  header {{ margin-bottom: 28px; }}
  header h1 {{ font-size: 22px; font-weight: 700; }}
  header .meta {{ font-size: 13px; color: #888; margin-top: 4px; }}
  .alert-banner {{ background: #fff3cd; border: 1.5px solid #f0c040; border-radius: 8px;
                   padding: 12px 16px; margin-bottom: 24px; font-size: 14px; color: #7a5c00; }}
  .section-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase;
                    letter-spacing: .08em; color: #999; margin-bottom: 10px; }}
  .card {{ background: #fff; border-radius: 10px; border: 1.5px solid #e8e8e4;
           padding: 18px 20px; margin-bottom: 12px; }}
  .card.urgent {{ border-color: #f0a000; background: #fffdf2; }}
  .card.alert  {{ border-color: #e57373; background: #fff8f8; }}
  .card-header {{ display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px; }}
  .person-info {{ flex: 1; }}
  .person-name {{ font-size: 15px; font-weight: 700; }}
  .person-meta {{ font-size: 12px; color: #888; margin-top: 2px; }}
  .start-badge {{ font-size: 12px; font-weight: 600; padding: 3px 10px;
                  border-radius: 20px; white-space: nowrap; }}
  .tasks {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .task {{ display: flex; align-items: flex-start; gap: 8px; font-size: 13px;
           padding: 8px 12px; border-radius: 7px; background: #f7f7f5; }}
  .task .icon {{ font-size: 15px; flex-shrink: 0; margin-top: 1px; }}
  .task.done   {{ background: #f0fdf4; color: #166534; }}
  .task.todo   {{ background: #f7f7f5; color: #555; }}
  .task.warn   {{ background: #fff7ed; color: #9a3412; }}
  .task.danger {{ background: #fef2f2; color: #991b1b; }}
  .task .label  {{ font-weight: 600; }}
  .task .detail {{ font-size: 11.5px; opacity: .85; margin-top: 1px; }}
  .task-content {{ flex: 1; }}
  .notes {{ margin-top: 10px; font-size: 12.5px; color: #666;
            background: #f9f9f7; border-radius: 6px; padding: 8px 12px; line-height: 1.5; }}
  .divider {{ height: 1px; background: #e8e8e4; margin: 28px 0; }}
  .completed-section .card {{ opacity: .65; }}
  @media (max-width: 600px) {{ .tasks {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<header>
  <h1>Onboarding Status</h1>
  <div class="meta">Generated {generated} · Source: #onboarding-tasks · Anna's tasks: laptop ordered + Thursday email</div>
</header>

{alert_html}

<div class="section-label">Active pipeline</div>
{active_cards}

<div class="divider"></div>

<div class="section-label completed-section">Recently started</div>
<div class="completed-section">
{completed_cards}
</div>
</body>
</html>"""


# ── Slack DM ───────────────────────────────────────────────────────────────────

def build_slack_summary(pipeline: list[Person]) -> str:
    lines = [f"*Onboarding Status — {datetime.now().strftime('%A, %B %-d')}*\n"]

    # Urgent items first
    urgent = [p for p in pipeline
              if p.thu_urgency in ("today", "tomorrow", "overdue")
              and p.thu_email not in ("sent", "na")]
    if urgent:
        lines.append("*🔴 Thursday email — action needed:*")
        for p in urgent:
            due = p.thu_urgency
            lines.append(f"  • {p.name} (starts {p.start.strftime('%-m/%-d')}) — due {due}")
        lines.append("")

    lines.append("*Pipeline:*")
    for p in pipeline:
        laptop_s = {"ordered": "✅ laptop", "not_ordered": "🟠 no laptop",
                    "unknown": "⬜ laptop?", "na": "—"}.get(p.laptop, "")
        email_s  = {"sent": "✅ email", "not_sent": "🔴 email not sent",
                    "unknown": f"⬜ email due {p.thu_due.strftime('%-m/%-d') if p.thu_due else '?'}",
                    "na": "—"}.get(p.thu_email, "")
        start_s  = urgency_label(p)
        lines.append(f"  • *{p.name}* ({start_s}) | {laptop_s} | {email_s}")
        if p.notes:
            lines.append(f"    ↳ {p.notes[0][:100]}")

    return "\n".join(lines)


def send_dm(text: str):
    try:
        resp = client.conversations_open(users=ANNA_USER_ID)
        dm_channel = resp["channel"]["id"]
        client.chat_postMessage(channel=dm_channel, text=text, mrkdwn=True)
        print("Slack DM sent.")
    except SlackApiError as e:
        print(f"Error sending DM: {e}", file=sys.stderr)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    print("Building onboarding pipeline...")
    pipeline = build_pipeline()
    print(f"Found {len(pipeline)} people in pipeline.")

    html = render_html(pipeline)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"HTML written to {OUTPUT_PATH}")

    if SEND_SLACK_DM:
        summary = build_slack_summary(pipeline)
        print("Sending Slack DM to Anna...")
        send_dm(summary)

    # Print summary to stdout for Actions logs
    for p in pipeline:
        print(f"  {p.name} | start={p.start.date()} | laptop={p.laptop} | thu_email={p.thu_email}")


if __name__ == "__main__":
    main()
