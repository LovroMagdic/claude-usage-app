"""
Claude Usage — a background system-tray app for Windows.

Sits in the taskbar tray. Left-click the icon to pop up a small window that
shows, at a glance:
  * your Claude plan limits (5-hour session + weekly) — the same numbers the
    `/usage` command shows inside Claude Code, with reset countdowns;
  * token usage & estimated cost from your local Claude Code transcripts
    (today / this month / all-time).

The tray icon itself is a ring that turns green -> amber -> red as your
current 5-hour session fills up, so you can read your status without clicking.

Run:  pythonw tray_app.py      (pythonw = no console window)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone

import pystray
from PIL import Image, ImageDraw, ImageTk

LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "claudecode.png")

import usage_data

# --- high-DPI awareness so text/icon are crisp on Windows ------------------
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# --- palette (flat monospace, monochrome — matches the design card) --------
BG        = "#0a0a0a"   # window background
BORDER    = "#262626"   # outer border
DIV       = "#1f1f1f"   # section dividers
DIV_ROW   = "#1a1a1a"   # token-row separators
TITLE     = "#f5f5f5"   # "claude usage"
FG        = "#e5e5e5"   # values, percentages, bar fill
LABEL     = "#cccccc"   # limit labels (session / weekly)
ROW_LABEL = "#f0f0f0"   # token-row labels
SECTION   = "#777777"   # PLAN LIMITS / TOKENS headers
SUB       = "#666666"   # reset text, row detail, × close
UPDATED   = "#555555"   # "updated HH:MM:SS"
REFRESH   = "#999999"   # ↻ icon
BADGE     = "#8a8a8a"   # PRO badge text
BADGE_BD  = "#333333"   # PRO badge border
TRACK     = "#242424"   # bar track
BAR_FILL  = "#e5e5e5"   # bar fill (monochrome)

FONT      = "Consolas"  # ui-monospace equivalent on Windows

REFRESH_SECONDS = 60

# --- animation tuning ---
ANIM_STEPS = 12         # frames per open/close animation
ANIM_MS    = 11         # ms between frames  (~130ms total)
SLIDE_PX   = 14         # how far the window slides while fading
SPINNER    = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # braille frames for the refresh spinner
MIN_SPIN_S = 0.45       # keep the spinner up at least this long

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".app_config.json")

# Notify when the 5-hour session resets, but only if it was used at least this
# much beforehand (a reset from ~0% isn't worth a popup). Set to 0 to always
# notify on reset.
NOTIFY_RESET_MIN_PCT = 50


def _ease_out(t):
    return 1 - (1 - t) ** 3


def _parse_iso(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def sev_color(pct):
    """Severity color — used only for the tray-icon ring (the popup card is
    intentionally monochrome)."""
    if pct is None:
        return "#666666"
    if pct >= 85:
        return "#e0605e"
    if pct >= 60:
        return "#e0a54a"
    return "#5fb87a"


# --- formatting helpers ----------------------------------------------------

def human_tokens(n):
    n = n or 0
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def human_reset(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return ""
    delta = dt - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "resetting…"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"resets in {d}d {h}h"
    if h:
        return f"resets in {h}h {m}m"
    return f"resets in {m}m"


# --- tray icon image -------------------------------------------------------

def make_icon_image(pct):
    """A ring filled proportionally to pct, colored by severity."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad, w = 6, 9
    box = [pad, pad, size - pad, size - pad]
    d.arc(box, 0, 360, fill=(58, 56, 53, 255), width=w)  # track
    if pct:
        col = sev_color(pct)
        rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        extent = 360 * min(pct, 100) / 100
        d.arc(box, -90, -90 + extent, fill=rgb + (255,), width=w)
    return img


# ===========================================================================
# The app
# ===========================================================================

class UsageApp:
    def __init__(self):
        self.snapshot = None          # latest fetched data
        self.last_updated = None
        self._last_hidden = 0.0       # for the icon-click toggle debounce
        self._anim_after = None       # pending open/close animation frame
        self._animating = False
        self._refreshing = False      # a refresh is in flight
        self._spinning = False        # spinner loop is running
        self._spin_start = 0.0
        self._prev_session_reset = None   # last-seen session resets_at (dt)
        self._prev_session_pct = None     # last-seen session utilization
        self.show_cost = self._load_config().get("show_cost", False)
        self._build_window()
        self._build_icon()

    # ---- persisted settings ----------------------------------------------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"show_cost": self.show_cost}, f)
        except Exception:
            pass

    def _load_logo(self, px):
        """Load the pre-rasterized Claude Code logo, scaled to px height.
        Returns a PhotoImage (keep a reference) or None if unavailable."""
        try:
            im = Image.open(LOGO_PATH).convert("RGB")
            im = im.resize((px, px), Image.LANCZOS)
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    # ---- window ----------------------------------------------------------
    def _build_window(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Claude Usage")
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self._hide_from_taskbar()

        self.W = 400

        self.outer = tk.Frame(self.root, bg=BG, highlightthickness=1,
                              highlightbackground=BORDER)
        self.outer.pack(fill="both", expand=True)

        # --- header (claude usage · PRO / ×) ---
        header = tk.Frame(self.outer, bg=BG)
        header.pack(fill="x")
        hin = tk.Frame(header, bg=BG)
        hin.pack(fill="x", padx=18, pady=14)
        left = tk.Frame(hin, bg=BG)
        left.pack(side="left")
        self._logo_img = self._load_logo(28)
        if self._logo_img is not None:
            tk.Label(left, image=self._logo_img, bg=BG).pack(
                side="left", padx=(0, 9))
        tk.Label(left, text="claude usage", bg=BG, fg=TITLE,
                 font=(FONT, 11)).pack(side="left")
        badge = tk.Frame(left, bg=BG, highlightthickness=1,
                         highlightbackground=BADGE_BD)
        badge.pack(side="left", padx=(10, 0))
        self.plan_lbl = tk.Label(badge, text="PRO", bg=BG, fg=BADGE,
                                 font=(FONT, 7))
        self.plan_lbl.pack(padx=6, pady=1)
        close = tk.Label(hin, text="×", bg=BG, fg=SUB, font=(FONT, 14),
                         cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.hide())
        tk.Frame(self.outer, height=1, bg=DIV).pack(fill="x")

        # --- dynamic body (limits + tokens) ---
        self.body = tk.Frame(self.outer, bg=BG)
        self.body.pack(fill="x")

        # --- footer (updated · ↻) ---
        tk.Frame(self.outer, height=1, bg=DIV).pack(fill="x")
        foot = tk.Frame(self.outer, bg=BG)
        foot.pack(fill="x")
        fin = tk.Frame(foot, bg=BG)
        fin.pack(fill="x", padx=18, pady=12)
        self.updated_lbl = tk.Label(fin, text="", bg=BG, fg=UPDATED,
                                    font=(FONT, 8))
        self.updated_lbl.pack(side="left")
        r = tk.Label(fin, text="↻", bg=BG, fg=REFRESH, font=(FONT, 11),
                     cursor="hand2")
        r.pack(side="right")
        self.refresh_lbl = r
        r.bind("<Button-1>", lambda e: self.refresh_async(force=True))
        r.bind("<Enter>", lambda e: not self._spinning and r.config(fg=FG))
        r.bind("<Leave>", lambda e: not self._spinning and r.config(fg=REFRESH))

        self.root.bind("<Escape>", lambda e: self.hide())
        self.root.bind("<FocusOut>", self._on_focus_out)

    def _hide_from_taskbar(self):
        """Mark the window as a Windows 'tool window' so it never gets a
        taskbar button or Alt-Tab entry — it lives only in the tray."""
        try:
            import ctypes
            self.root.update_idletasks()
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_APPWINDOW = 0x00040000
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _on_focus_out(self, _e):
        # hide when the user clicks elsewhere
        self.root.after(120, self._maybe_hide)

    def _maybe_hide(self):
        try:
            if self.root.focus_displayof() is None:
                self.hide()
        except Exception:
            self.hide()

    # ---- section builders -------------------------------------------------
    def _section_label(self, parent, text):
        tk.Label(parent, text=text, bg=BG, fg=SECTION,
                 font=(FONT, 8)).pack(anchor="w")

    def _bar(self, parent, label, pct, sub, gap):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, gap))
        head = tk.Frame(row, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=label, bg=BG, fg=LABEL,
                 font=(FONT, 9)).pack(side="left")
        pct_txt = "—" if pct is None else f"{pct:.0f}%"
        tk.Label(head, text=pct_txt, bg=BG, fg=FG,
                 font=(FONT, 9)).pack(side="right")

        cw = self.W - 36
        cv = tk.Canvas(row, height=4, width=cw, bg=BG, highlightthickness=0)
        cv.pack(fill="x", pady=(6, 0))
        self._round_rect(cv, 0, 0, cw, 4, 2, TRACK)
        if pct:
            fillw = max(4, int(cw * min(pct, 100) / 100))
            self._round_rect(cv, 0, 0, fillw, 4, 2, BAR_FILL)
        if sub:
            tk.Label(row, text=sub, bg=BG, fg=SUB,
                     font=(FONT, 8)).pack(anchor="w", pady=(6, 0))

    @staticmethod
    def _round_rect(cv, x1, y1, x2, y2, r, color):
        cv.create_oval(x1, y1, x1 + 2 * r, y2, fill=color, outline=color)
        cv.create_oval(x2 - 2 * r, y1, x2, y2, fill=color, outline=color)
        cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline=color)

    def _cost_row(self, label, bucket):
        tk.Frame(self.body, height=1, bg=DIV_ROW).pack(fill="x")  # border-top
        rin = tk.Frame(self.body, bg=BG)
        rin.pack(fill="x", padx=18, pady=9)
        top = tk.Frame(rin, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=label, bg=BG, fg=ROW_LABEL,
                 font=(FONT, 10)).pack(side="left")
        tk.Label(top, text=f"${bucket['cost']:,.2f}", bg=BG, fg=FG,
                 font=(FONT, 10, "bold")).pack(side="right")
        detail = (f"{human_tokens(bucket['out'])} out · "
                  f"{human_tokens(bucket['cr'])} cache · "
                  f"{bucket['msgs']} msgs")
        tk.Label(rin, text=detail, bg=BG, fg=SUB,
                 font=(FONT, 8)).pack(anchor="w", pady=(2, 0))

    # ---- render -----------------------------------------------------------
    def render(self):
        for w in self.body.winfo_children():
            w.destroy()

        snap = self.snapshot or {}
        lim = snap.get("limits", {})
        tok = snap.get("tokens", {})

        self.plan_lbl.config(text=(lim.get("plan") or "pro").upper())

        # --- plan limits ---
        pl = tk.Frame(self.body, bg=BG)
        pl.pack(fill="x", padx=18, pady=(16, 6))
        self._section_label(pl, "PLAN LIMITS")
        tk.Frame(pl, height=12, bg=BG).pack()  # spacer

        has_bars = bool(lim.get("session"))
        if has_bars:
            s = lim.get("session") or {}
            self._bar(pl, "session (5-hour)", s.get("percent"),
                      human_reset(s.get("resets_at")), gap=16)
            wk = lim.get("weekly") or {}
            self._bar(pl, "weekly", wk.get("percent"),
                      human_reset(wk.get("resets_at")), gap=14)
            for sc in lim.get("weekly_scoped") or []:
                if sc.get("percent"):
                    self._bar(pl, f"weekly · {sc['name'].lower()}",
                              sc.get("percent"),
                              human_reset(sc.get("resets_at")), gap=14)
        if lim.get("error"):
            tk.Label(pl, text=lim["error"], bg=BG, fg=SUB,
                     font=(FONT, 8), wraplength=self.W - 40,
                     justify="left").pack(anchor="w")

        # --- tokens (hidden unless toggled on) ---
        if self.show_cost:
            tk.Frame(self.body, height=1, bg=DIV).pack(fill="x",
                                                       padx=18, pady=8)
            th = tk.Frame(self.body, bg=BG)
            th.pack(fill="x", padx=18, pady=(6, 4))
            self._section_label(th, "TOKENS · EST. API COST")
            if tok:
                self._cost_row("today", tok["today"])
                self._cost_row("this month", tok["month"])
                self._cost_row("all time", tok["total"])
        else:
            tk.Frame(self.body, height=6, bg=BG).pack()  # small bottom breather

        if self.last_updated:
            self.updated_lbl.config(
                text="updated " + self.last_updated.strftime("%H:%M:%S"))

    # ---- show / hide (animated) ------------------------------------------
    def _cancel_anim(self):
        if self._anim_after is not None:
            try:
                self.root.after_cancel(self._anim_after)
            except Exception:
                pass
            self._anim_after = None

    def show(self):
        if self.root.state() != "withdrawn" and not self._animating:
            return
        self._cancel_anim()
        self.render()
        self.root.update_idletasks()
        self._h = self.outer.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._x = sw - self.W - 16
        self._y = sh - self._h - 56          # sit above the taskbar
        self.root.attributes("-alpha", 0.0)
        self.root.geometry(
            f"{self.W}x{self._h}+{self._x}+{self._y + SLIDE_PX}")
        self._hide_from_taskbar()      # keep it out of the taskbar
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._animate(opening=True, step=0)

    def hide(self):
        self._last_hidden = time.time()
        if self.root.state() == "withdrawn":
            return
        self._cancel_anim()
        # ensure geometry anchors exist (e.g. if hide fires very early)
        if not hasattr(self, "_y"):
            self._h = self.outer.winfo_reqheight()
            self._x = self.root.winfo_x()
            self._y = self.root.winfo_y()
        self._animate(opening=False, step=0)

    def _animate(self, opening, step):
        self._animating = True
        t = _ease_out(step / ANIM_STEPS)
        if opening:
            alpha, off = t, int(SLIDE_PX * (1 - t))
        else:
            alpha, off = 1 - t, int(SLIDE_PX * t)
        try:
            self.root.attributes("-alpha", max(0.0, min(1.0, alpha)))
            self.root.geometry(
                f"{self.W}x{self._h}+{self._x}+{self._y + off}")
        except Exception:
            pass

        if step < ANIM_STEPS:
            self._anim_after = self.root.after(
                ANIM_MS, lambda: self._animate(opening, step + 1))
            return

        # animation finished
        self._anim_after = None
        self._animating = False
        if opening:
            self.root.attributes("-alpha", 1.0)
            self.root.focus_force()
        else:
            self.root.withdraw()
            self.root.attributes("-alpha", 1.0)

    def toggle_from_icon(self):
        """Called when the tray icon is clicked. If the window is open, close
        it; if closed, open it. Clicking the icon steals focus from the window,
        which fires the auto-hide — so if the window was hidden in the last
        moment, this same click caused it and we leave it closed instead of
        re-opening (prevents a close→reopen flicker)."""
        if self.root.state() != "withdrawn":
            self.hide()
        elif time.time() - self._last_hidden > 0.4:
            self.show()

    # ---- data refresh -----------------------------------------------------
    def refresh_async(self, force=False):
        if not self._refreshing:
            self._spin_start = time.time()
        self._refreshing = True
        if not self._spinning and self.root.state() != "withdrawn":
            self._spin(0)
        threading.Thread(target=self._refresh_worker, args=(force,),
                         daemon=True).start()

    def _spin(self, i):
        """Animate the ↻ icon into a braille spinner while a refresh runs."""
        done = (not self._refreshing) and \
            (time.time() - self._spin_start >= MIN_SPIN_S)
        if done:
            self._spinning = False
            self.refresh_lbl.config(text="↻", fg=REFRESH)
            return
        self._spinning = True
        self.refresh_lbl.config(text=SPINNER[i % len(SPINNER)], fg=FG)
        self.root.after(70, lambda: self._spin(i + 1))

    def _refresh_worker(self, force):
        data = usage_data.fetch_all(force=force)
        # marshal back onto the tk thread
        self.root.after(0, lambda: self._apply(data))

    def _apply(self, data):
        self.snapshot = data
        self.last_updated = datetime.now()
        self._refreshing = False
        # if the window is open but the spinner never started, kick it once so
        # the finish frame restores the ↻ glyph
        if not self._spinning and self.root.state() != "withdrawn":
            self.refresh_lbl.config(text="↻", fg=REFRESH)
        # update icon + tooltip from session %
        s = (data.get("limits") or {}).get("session") or {}
        pct = s.get("percent")
        self.icon.icon = make_icon_image(pct)
        wk = (data.get("limits") or {}).get("weekly") or {}
        self.icon.title = self._tooltip(pct, wk.get("percent"))
        # notify if the 5-hour session limit just reset
        self._check_session_reset(s.get("resets_at"), pct)
        if self.root.state() != "withdrawn":
            self.render()
        # schedule next auto-refresh
        self.root.after(REFRESH_SECONDS * 1000, self.refresh_async)

    # ---- 5-hour reset notification ---------------------------------------
    def _check_session_reset(self, new_iso, new_pct):
        """Detect when the rolling 5-hour window has reset and post a tray
        notification. A reset shows up as resets_at jumping forward (a new
        window began) or clearing (the window elapsed while idle)."""
        new_dt = _parse_iso(new_iso)
        prev_dt = self._prev_session_reset
        prev_pct = self._prev_session_pct

        reset = False
        if prev_dt is not None:
            if new_dt is None:
                reset = True                                   # window cleared
            elif (new_dt - prev_dt).total_seconds() > 300:     # jumped forward
                reset = True

        # remember the newest values for next comparison
        self._prev_session_reset = new_dt
        self._prev_session_pct = new_pct

        if reset and prev_pct is not None and prev_pct >= NOTIFY_RESET_MIN_PCT:
            self._notify_reset(prev_pct)

    def _notify_reset(self, prev_pct):
        msg = (f"Your 5-hour usage limit has reset "
               f"(was at {prev_pct:.0f}%). You're back to full capacity.")
        try:
            self.icon.notify(msg, "Claude Usage")
        except Exception:
            pass

    @staticmethod
    def _tooltip(session_pct, weekly_pct):
        s = "—" if session_pct is None else f"{session_pct:.0f}%"
        w = "—" if weekly_pct is None else f"{weekly_pct:.0f}%"
        return f"Claude Usage\nSession: {s}  ·  Weekly: {w}"

    # ---- tray icon --------------------------------------------------------
    def _build_icon(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show / hide", self._on_show, default=True),
            pystray.MenuItem("Refresh now", self._on_refresh),
            pystray.MenuItem("Show API cost estimate", self._on_toggle_cost,
                             checked=lambda item: self.show_cost),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )
        self.icon = pystray.Icon("claude_usage", make_icon_image(None),
                                 "Claude Usage", menu)

    # pystray callbacks run on the icon's own thread -> marshal to tk
    def _on_show(self, icon, item):
        self.root.after(0, self.toggle_from_icon)

    def _on_refresh(self, icon, item):
        self.root.after(0, lambda: self.refresh_async(force=True))

    def _on_toggle_cost(self, icon, item):
        def do():
            self.show_cost = not self.show_cost
            self._save_config()
            if self.root.state() != "withdrawn":
                self._rerender_resize()
        self.root.after(0, do)

    def _rerender_resize(self):
        """Re-render and re-fit the window height in place (used when the cost
        section is toggled while the window is open)."""
        self.render()
        self.root.update_idletasks()
        self._h = self.outer.winfo_reqheight()
        sh = self.root.winfo_screenheight()
        self._y = sh - self._h - 56
        self.root.geometry(f"{self.W}x{self._h}+{self._x}+{self._y}")

    def _on_quit(self, icon, item):
        icon.stop()
        self.root.after(0, self._destroy)

    def _destroy(self):
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    # ---- run --------------------------------------------------------------
    def run(self):
        # tray icon on a daemon thread; tk mainloop on the main thread
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.root.after(300, self.refresh_async)
        self.root.mainloop()


if __name__ == "__main__":
    UsageApp().run()
