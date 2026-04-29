# Onboarding Task Management

Daily automated status page for Tribe AI core member onboarding.
Scans `#onboarding-tasks` in Slack and surfaces Anna's two owned tasks
for every person in the pipeline: **laptop ordered** and **Thursday before-start email sent**.

## What it does

Runs every weekday morning at 8am ET via GitHub Actions:
1. Queries `#onboarding-tasks` channel history and all active onboarding threads
2. Parses each person's name, role, start date, and task status
3. Generates `output/onboarding-status.html` and commits it back to this repo
4. Optionally sends Anna a Slack DM with a plain-text summary

## Setup

### 1. Required GitHub secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | What it is |
|--------|------------|
| `SLACK_TOKEN` | A bot token (`xoxb-`) with scopes: `channels:history`, `groups:history`, `channels:read`, `search:read`, `im:write`, `chat:write` |

### 2. Slack bot scopes

Create a Slack app at https://api.slack.com/apps and add these OAuth scopes:
- `channels:history` — read public channels
- `groups:history` — read private channels the bot is in
- `channels:read`
- `search:read`
- `im:write` — open DMs
- `chat:write` — send messages

Invite the bot to `#onboarding-tasks`:
```
/invite @your-bot-name
```

### 3. Trigger manually

Go to **Actions → Daily Onboarding Status → Run workflow** to run on demand.

## How status is detected

| Signal | Where it comes from |
|--------|---------------------|
| **Laptop ordered** | Strikethrough on name (`~Name~`) in cohort thread, or "ordered/purchased" in reply |
| **Thursday email** | "email sent" reply in the onboarding thread, or "thursday email" confirmation |
| **Shipping address** | Reply in cohort thread with address content |
| **Open issues** | Replies containing ⚠️, "no response", "blocked", "1password", "vault" |

## Source of truth gap

`#hires-and-exits` is the intended canonical trigger channel (Craig posts new hires there),
but it is currently private and the bot cannot read it. Until the bot is invited,
`#onboarding-tasks` is the effective source. To fix: invite the bot to `#hires-and-exits`
and Craig/Sean should keep start dates and roles accurate there.

## Local development

```bash
pip install -r requirements.txt
export SLACK_TOKEN=xoxb-your-token
python scripts/generate_status.py
# opens output/onboarding-status.html
```

To also send the Slack DM:
```bash
SEND_SLACK_DM=true python scripts/generate_status.py
```
