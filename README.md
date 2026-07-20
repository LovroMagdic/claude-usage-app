# Claude Usage — Windows tray app

A tiny background app that lives in the Windows system tray and shows your
current Claude usage at a glance. Left-click the icon to pop up a small window;
the icon itself is a ring that turns **green → amber → red** as your 5-hour
session fills up.

<p align="center">
  <img src="readme_images/window_image.png" alt="Claude Usage popup window" width="440">
</p>

## The tray icon at a glance

The ring fills clockwise with your 5-hour session usage and changes color by
severity, so you can read your status without clicking. The images below are
real screenshots of the tray icon at different usage levels:

<p align="center">
  <img src="readme_images/tray_icon_stages.png" alt="Tray icon at 22%, 45%, 68% and 95% usage" width="620">
</p>

| Fill | Color | Meaning |
|------|-------|---------|
| under 60% | 🟢 green  | plenty of session headroom |
| 60–84%    | 🟠 amber  | getting close |
| 85%+      | 🔴 red    | nearly at the 5-hour limit |

## What it shows

- **Plan limits** — your 5-hour *session* and *weekly* usage %, with reset
  countdowns. Same numbers as Claude Code's `/usage` command, fetched live from
  the OAuth token already stored in `~/.claude/.credentials.json`.
- **Tokens · est. API cost** *(optional, toggle in the tray menu)* — tokens and
  estimated equivalent API cost for *today*, *this month*, and *all time*,
  read from your local Claude Code transcripts in `~/.claude/projects/`.
  Subscription users aren't billed per token — this is a "what it would cost on
  the API" estimate.

## Notifications

When your 5-hour session limit rolls over, the app pops a Windows toast so you
know you're back to full capacity — no need to open the popup or run `/usage`.

<p align="center">
  <img src="readme_images/notification_image.png" alt="Claude Usage session-reset notification" width="400">
</p>

## Requirements

- Windows
- Python 3.11+
- `pip install -r requirements.txt`

## Running

Install the dependencies once:

```bat
pip install -r requirements.txt
```

Then start the app with no console window:

```bat
pythonw tray_app.py
```

Or double-click **`Claude Usage.bat`** (a silent launcher). The launcher runs
the app under a uniquely-named copy of the Python runtime (`ClaudeUsage.exe`,
created next to `pythonw.exe` on first run) so the tray icon gets its **own
identity** — dragging it in or out of the hidden-icons flyout won't drag your
other Python tray apps along with it.

Only run **one** instance at a time — launching it again adds a second identical
icon to the tray. If you see duplicates, close the extras from *Task Manager*
(end the `pythonw.exe` / `ClaudeUsage.exe` process running `tray_app.py`) or
right-click each icon → *Quit*.

To start it automatically at login, drop a shortcut to the `.bat` into your
Startup folder (`shell:startup` in the Run dialog).

- **Refresh:** click the ↻ button, or right-click the tray icon → *Refresh now*.
- **Show/hide cost:** right-click the tray icon → *Show API cost estimate*.
- **Quit:** right-click the tray icon → *Quit*.

## How it works

- `tray_app.py` — the tray icon + popup UI.
- `usage_data.py` — data layer: the `/usage` limits endpoint plus transcript
  parsing.

The app only reads local files plus one authenticated HTTPS GET to
`api.anthropic.com`; it never writes anything except refreshing an expired OAuth
token in place. The `/usage` endpoint is rate-limited, so limits are polled at
most once every 5 minutes (with 429 back-off) and cached locally; token/cost
data refreshes every minute from local files.
