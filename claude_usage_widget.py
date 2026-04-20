#!/usr/bin/env python3
"""
Claude AI Usage Widget — Linux System Tray
Shows claude.ai subscription usage (5h / 7d) in the taskbar.
Click to see detailed breakdown + reset timers.

Supports two auth methods:
  1. Auto-detect from Claude Code credentials (~/.claude/.credentials.json)
  2. Manual OAuth token via config file (~/.config/claude-usage-widget/config.json)

Author: Statotech Systems
Version: 1.0.0
License: MIT
"""

__version__ = "1.1.0"
__author__ = "Statotech Systems"

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Notify", "0.7")

from gi.repository import Gtk, AppIndicator3, GLib, Notify, Gdk
import cairo
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

APP_ID = "claude-usage-widget"
APP_NAME = "Claude Usage"
ICON_NAME = "network-transmit-receive"
REFRESH_INTERVAL_SEC = 120
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"

CONFIG_DIR = Path.home() / ".config" / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"

# OAuth refresh endpoint + client_id extracted from the official Claude Code
# CLI (`~/.local/lib/node_modules/@anthropic-ai/claude-code/cli.js`). The
# widget only uses these for REFRESHING an access token that Claude Code
# already obtained; it does not perform the full authorize/PKCE handshake
# itself. Initial sign-in is expected to happen via `claude` (Claude Code).
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]

# ── Colors for the dynamic SVG icon ─────────────────────────────────────────

COLOR_WHITE = "#ffffff"
COLOR_YELLOW = "#eab308"
COLOR_ORANGE = "#f97316"
COLOR_RED = "#ef4444"
COLOR_GRAY = "#6b7280"


def get_color_for_pct(pct: float) -> str:
    if pct < 0.5:
        return COLOR_WHITE
    elif pct < 0.75:
        return COLOR_YELLOW
    elif pct < 0.9:
        return COLOR_ORANGE
    else:
        return COLOR_RED


def hex_to_rgb(hex_color: str) -> tuple:
    """Convert hex color to RGB tuple (0-1 range)."""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


ICON_W = 153
ICON_H = 32

_icon_seq = 0


def _apply_gray_antialias(ctx: "cairo.Context") -> None:
    """Force grayscale antialiasing on text.

    Cairo's default on Linux is ``ANTIALIAS_SUBPIXEL`` (RGB subpixel AA tuned
    for LCD panels), which rasterizes glyph edges into R/G/B subpixel
    contributions that show up as green/red chromatic fringes when the PNG is
    decoded off the LCD — for a small icon drawn on a solid color this looks
    like the wrong color entirely. Grayscale AA keeps the specified color.
    """
    opts = cairo.FontOptions()
    opts.set_antialias(cairo.ANTIALIAS_GRAY)
    ctx.set_font_options(opts)


def _rounded_rect_path(
    ctx: "cairo.Context", x: float, y: float, w: float, h: float, r: float
) -> None:
    if w < 2 * r:
        r = w / 2
    if h < 2 * r:
        r = h / 2
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    ctx.close_path()


def _draw_progress_bar(
    ctx: "cairo.Context",
    x: float,
    y: float,
    w: float,
    h: float,
    pct: int,
    color: str,
    text: str | None = None,
    text_size_pt: int = 7,
    track_alpha: float = 0.18,
) -> None:
    """Draw a sleek pill-shaped horizontal progress bar with optional inner text.

    ``pct`` is 0–100 representing the *fill* percentage. The caller decides
    whether this is usage or remaining — this widget passes remaining, so the
    bar reads like a gauge (100 = full / safe, 0 = empty / critical).

    When ``text`` is provided, it is drawn centered inside the bar using
    two-pass inverse rendering: the text uses ``color`` over the empty region
    (stays visible against a dark panel) and black over the filled region
    (inverted for contrast against the fill). Same pattern used for the
    battery label, now applied to the pill shape.
    """
    import gi

    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo

    r, g, b = hex_to_rgb(color)
    radius = h / 2

    _rounded_rect_path(ctx, x, y, w, h, radius)
    ctx.set_source_rgba(r, g, b, track_alpha)
    ctx.fill()

    pct = max(0, min(100, int(pct)))
    fill_w = w * pct / 100.0
    if fill_w > 0:
        ctx.save()
        _rounded_rect_path(ctx, x, y, w, h, radius)
        ctx.clip()
        ctx.rectangle(x, y, fill_w, h)
        ctx.set_source_rgb(r, g, b)
        ctx.fill()
        ctx.restore()

    if not text:
        return

    _apply_gray_antialias(ctx)

    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(f"Sans Bold {text_size_pt}"))
    layout.set_text(text, -1)
    tw, th = layout.get_pixel_size()

    text_x = x + (w - tw) / 2
    text_y = y + (h - th) / 2

    ctx.move_to(text_x, text_y)
    ctx.set_source_rgb(r, g, b)
    PangoCairo.show_layout(ctx, layout)

    if fill_w > 0:
        ctx.save()
        _rounded_rect_path(ctx, x, y, w, h, radius)
        ctx.clip()
        ctx.rectangle(x, y, fill_w, h)
        ctx.clip()
        ctx.move_to(text_x, text_y)
        ctx.set_source_rgb(0.0, 0.0, 0.0)
        PangoCairo.show_layout(ctx, layout)
        ctx.restore()


def _draw_pango_text(
    ctx: "cairo.Context",
    x: float,
    y: float,
    text: str,
    color: str,
    size_pt: int = 8,
    weight: str = "Bold",
) -> None:
    import gi

    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo

    _apply_gray_antialias(ctx)

    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(f"Sans {weight} {size_pt}"))
    layout.set_text(text, -1)
    r, g, b = hex_to_rgb(color)
    ctx.set_source_rgb(r, g, b)
    ctx.move_to(x, y)
    PangoCairo.show_layout(ctx, layout)


def write_icon(
    pct5: float = 0.0,
    pct7: float = 0.0,
    error: bool = False,
) -> str:
    """Generate a composite PNG: Anthropic logo + ``5H {bar}  1W {bar}`` layout.

    Each progress bar is a pill-shaped horizontal gauge whose fill length
    follows the *remaining* quota (100 % = full / safe, 0 % = empty / out).
    The remaining-% number is drawn centered inside each bar with two-pass
    inverse coloring (black over the filled portion, bar color over the
    empty portion). Labels ``5H`` (five-hour window) and ``1W`` (seven-day /
    one-week window) sit immediately before their bars.

    The on-panel ``set_label()`` is always kept empty: relying on it is
    fragile (it fails silently on GNOME Shell 46 with the
    ``ubuntu-appindicators`` extension when the string contains non-ASCII
    characters), and mirroring the icon content there would look duplicated.

    A fresh filename (``icon-<seq>.png``) is used on every call: AppIndicator
    short-circuits ``set_icon_full()`` when the path matches the currently-set
    icon, so overwriting a single file in place leaves the initial icon pinned
    in the panel. Rotating the filename forces a reload every update.
    """
    import gi

    gi.require_version("Rsvg", "2.0")
    from gi.repository import Rsvg

    pct5 = max(0.0, min(1.0, pct5))
    pct7 = max(0.0, min(1.0, pct7))

    rem5 = 100 - int(pct5 * 100)
    rem7 = 100 - int(pct7 * 100)

    color5 = COLOR_GRAY if error else get_color_for_pct(pct5)
    color7 = COLOR_GRAY if error else get_color_for_pct(pct7)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, ICON_W, ICON_H)
    ctx = cairo.Context(surface)

    svg_path = Path(__file__).resolve().parent / "anthropic-1.svg"
    handle = Rsvg.Handle.new_from_file(str(svg_path))
    dims = handle.get_intrinsic_dimensions()
    svg_w = dims.out_width.length
    svg_h = dims.out_height.length

    LOGO_SIZE = 26
    ctx.save()
    ctx.translate(1, (ICON_H - LOGO_SIZE) / 2)
    ctx.scale(LOGO_SIZE / svg_w, LOGO_SIZE / svg_h)
    rect = Rsvg.Rectangle()
    rect.x = 0
    rect.y = 0
    rect.width = svg_w
    rect.height = svg_h
    handle.render_document(ctx, rect)
    ctx.restore()

    if error:
        _draw_pango_text(
            ctx, x=30, y=(ICON_H - 14) / 2, text="ERR", color=COLOR_GRAY, size_pt=10
        )
    else:
        LABEL_COLOR = COLOR_WHITE
        LABEL_SIZE = 8
        LABEL_Y = 8

        BAR_W = 38
        BAR_H = 16
        BAR_Y = (ICON_H - BAR_H) / 2

        _draw_pango_text(
            ctx, x=33, y=LABEL_Y, text="5H", color=LABEL_COLOR, size_pt=LABEL_SIZE
        )
        _draw_progress_bar(
            ctx,
            x=50,
            y=BAR_Y,
            w=BAR_W,
            h=BAR_H,
            pct=rem5,
            color=color5,
            text=f"{rem5}%",
            text_size_pt=8,
        )

        _draw_pango_text(
            ctx, x=93, y=LABEL_Y, text="1W", color=LABEL_COLOR, size_pt=LABEL_SIZE
        )
        _draw_progress_bar(
            ctx,
            x=112,
            y=BAR_Y,
            w=BAR_W,
            h=BAR_H,
            pct=rem7,
            color=color7,
            text=f"{rem7}%",
            text_size_pt=8,
        )

    icon_dir = Path("/tmp") / APP_ID
    icon_dir.mkdir(exist_ok=True)

    global _icon_seq
    _icon_seq += 1
    icon_path = icon_dir / f"icon-{_icon_seq}.png"
    surface.write_to_png(str(icon_path))

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    for old in sorted(icon_dir.glob("icon*.png"), key=_mtime)[:-2]:
        try:
            old.unlink()
        except OSError:
            pass

    return str(icon_path)


# ── Token storage ───────────────────────────────────────────────────────────


_token_lock = threading.Lock()


def _read_widget_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_widget_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    os.chmod(CONFIG_FILE, 0o600)


def load_oauth_bundle() -> dict | None:
    """Load the full OAuth bundle (access/refresh/expiresAt/scopes/...).

    Widget config is preferred because the widget owns it; Claude Code's
    credentials file is used as a read-only fallback so that users who
    already logged in via Claude Code need no extra setup.
    """
    config = _read_widget_config()
    bundle = config.get("claudeAiOauth")
    if isinstance(bundle, dict) and bundle.get("accessToken"):
        return bundle

    if CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text())
            bundle = data.get("claudeAiOauth")
            if isinstance(bundle, dict) and bundle.get("accessToken"):
                return bundle
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_oauth_bundle(bundle: dict) -> None:
    config = _read_widget_config()
    config["claudeAiOauth"] = bundle
    config.pop("oauth_token", None)
    _write_widget_config(config)


def load_subscription_info() -> dict | None:
    bundle = load_oauth_bundle()
    if not bundle:
        return None
    return {
        "subscription_type": (bundle.get("subscriptionType") or "").title(),
        "rate_limit_tier": bundle.get("rateLimitTier") or "",
    }


def load_token() -> str | None:
    """Return a usable access token, refreshing silently if expired."""
    with _token_lock:
        bundle = load_oauth_bundle()
        if not bundle:
            return None

        access = bundle.get("accessToken")
        expires_at = bundle.get("expiresAt") or 0
        now_ms = int(time.time() * 1000)

        if access and expires_at > now_ms + 60_000:
            return access

        refresh = bundle.get("refreshToken")
        if not refresh:
            return access

        refreshed = _refresh_access_token(refresh, bundle)
        if refreshed:
            save_oauth_bundle(refreshed)
            return refreshed.get("accessToken")

        return access


def save_token(token: str) -> None:
    """Store a manually-entered access token.

    Used as a last-resort fallback when the native OAuth flow is unavailable
    (e.g. headless system). No refresh token is stored, so the widget will
    stop working when this token expires.
    """
    with _token_lock:
        bundle = load_oauth_bundle() or {}
        bundle["accessToken"] = token
        bundle.pop("refreshToken", None)
        bundle.pop("expiresAt", None)
        save_oauth_bundle(bundle)


# ── Token refresh ───────────────────────────────────────────────────────────


def _refresh_access_token(refresh_token: str, prev_bundle: dict) -> dict | None:
    """Exchange a refresh token for a new access token bundle.

    Returns a bundle merged on top of ``prev_bundle`` so that fields the
    refresh response omits (``subscriptionType`` etc.) are preserved.
    Returns ``None`` if the refresh call fails — the caller is expected
    to surface that condition to the user, not retry silently.
    """
    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "scope": " ".join(prev_bundle.get("scopes") or OAUTH_SCOPES),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"claude-usage-widget/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[claude-usage] Token refresh failed: {e}", file=sys.stderr)
        return None

    merged = dict(prev_bundle)
    merged.update(_token_response_to_bundle(data))
    if not merged.get("refreshToken"):
        merged["refreshToken"] = refresh_token
    return merged


def _token_response_to_bundle(data: dict) -> dict:
    scope = data.get("scope") or ""
    scopes = scope.split() if scope else list(OAUTH_SCOPES)
    expires_in = int(data.get("expires_in") or 0)
    expires_at = int(time.time() * 1000) + expires_in * 1000
    return {
        "accessToken": data.get("access_token"),
        "refreshToken": data.get("refresh_token"),
        "expiresAt": expires_at,
        "scopes": scopes,
        "subscriptionType": data.get("subscription_type") or "",
        "rateLimitTier": data.get("rate_limit_tier") or "",
    }


# ── API call ────────────────────────────────────────────────────────────────


class RateLimitError(Exception):
    def __init__(self, retry_after: int = 600):
        self.retry_after = retry_after


class AuthExpiredError(Exception):
    pass


def _fetch_usage_once(token: str) -> dict | None:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"claude-usage-widget/{__version__}",
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    }
    req = urllib.request.Request(USAGE_API_URL, headers=headers, method="GET")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", 120))
            if retry_after < 120:
                retry_after = 120
            raise RateLimitError(retry_after)
        if e.code in (401, 403):
            raise AuthExpiredError(f"HTTP {e.code}: {e.reason}")
        print(f"[claude-usage] HTTP {e.code}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[claude-usage] Error: {e}", file=sys.stderr)
        return None


def fetch_usage(token: str) -> dict | None:
    """Fetch usage data. On 401, refresh token once and retry transparently."""
    try:
        return _fetch_usage_once(token)
    except AuthExpiredError:
        bundle = load_oauth_bundle()
        refresh = bundle.get("refreshToken") if bundle else None
        if not refresh or not bundle:
            raise
        refreshed = _refresh_access_token(refresh, bundle)
        if not refreshed or not refreshed.get("accessToken"):
            raise
        save_oauth_bundle(refreshed)
        try:
            return _fetch_usage_once(refreshed["accessToken"])
        except AuthExpiredError:
            raise


# ── Time formatting ─────────────────────────────────────────────────────────


def format_reset_time(iso_str: str | None) -> str:
    if not iso_str:
        return "unknown"
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = reset_dt - now
        total_sec = int(delta.total_seconds())
        if total_sec <= 0:
            return "any moment"
        days, remainder = divmod(total_sec, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return iso_str


# ── Detail popup window ─────────────────────────────────────────────────────


class UsageDetailWindow(Gtk.Window):
    """Popup window showing detailed usage info."""

    def __init__(
        self,
        usage_data: dict | None,
        last_updated: str,
        token_status: str,
        user_info: dict | None = None,
        rate_limit_until: datetime | None = None,
    ):
        super().__init__(title="Claude AI Usage")
        self.set_default_size(380, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.MOUSE)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        self.set_decorated(True)

        # Apply CSS
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window { background-color: #1a1a2e; }
            .title-label { color: #e0e0ff; font-size: 16px; font-weight: bold; }
            .section-label { color: #a0a0c0; font-size: 11px; font-weight: bold; letter-spacing: 2px; }
            .metric-value { color: #ffffff; font-size: 28px; font-weight: bold; }
            .metric-sub { color: #8888aa; font-size: 11px; }
            .reset-label { color: #6b7280; font-size: 11px; }
            .status-ok { color: #22c55e; font-size: 11px; }
            .status-warn { color: #eab308; font-size: 11px; }
            .status-err { color: #ef4444; font-size: 11px; }
            .bar-bg { background-color: #2a2a4a; border-radius: 4px; }
            .separator { background-color: #2a2a4a; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(20)
        vbox.set_margin_end(20)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = Gtk.Label(label="⚡ Claude Usage")
        title.get_style_context().add_class("title-label")
        header.pack_start(title, False, False, 0)

        status_label = Gtk.Label(label=f"● {token_status}")
        sc = (
            "status-ok"
            if token_status == "Connected"
            else ("status-warn" if token_status == "Rate limited" else "status-err")
        )
        status_label.get_style_context().add_class(sc)
        status_label.set_halign(Gtk.Align.END)
        header.pack_end(status_label, False, False, 0)
        vbox.pack_start(header, False, False, 0)

        # Subscription info (if available)
        if user_info:
            sub_type = user_info.get("subscription_type")
            if sub_type:
                sub_label = Gtk.Label(label=f"Plan: {sub_type}")
                sub_label.get_style_context().add_class("metric-sub")
                sub_label.set_halign(Gtk.Align.START)
                vbox.pack_start(sub_label, False, False, 0)

        if usage_data:
            # Extra usage section (pay-as-you-go credits)
            extra = usage_data.get("extra_usage") or {}
            if extra and extra.get("is_enabled"):
                sep = Gtk.Separator()
                sep.get_style_context().add_class("separator")
                vbox.pack_start(sep, False, False, 4)

                extra_section = Gtk.Label(label="EXTRA USAGE (MONTHLY)")
                extra_section.get_style_context().add_class("section-label")
                extra_section.set_halign(Gtk.Align.START)
                vbox.pack_start(extra_section, False, False, 0)

                used = extra.get("used_credits") or 0
                limit = extra.get("monthly_limit") or 0
                utilization = extra.get("utilization") or 0
                extra_pct = min(int(utilization), 100)
                extra_decimal = extra_pct / 100
                extra_color = get_color_for_pct(extra_decimal)

                extra_val = Gtk.Label()
                extra_val.set_markup(
                    f'<span foreground="{extra_color}" font_weight="bold" font="28">{extra_pct}%</span>'
                )
                extra_val.set_halign(Gtk.Align.START)
                vbox.pack_start(extra_val, False, False, 0)

                credits_lbl = Gtk.Label(label=f"{used:.0f} / {limit:.0f} credits used")
                credits_lbl.get_style_context().add_class("metric-sub")
                credits_lbl.set_halign(Gtk.Align.START)
                vbox.pack_start(credits_lbl, False, False, 0)

            for key, label_text in [
                ("five_hour", "5-HOUR WINDOW"),
                ("seven_day", "7-DAY WINDOW"),
            ]:
                bucket = usage_data.get(key)
                if not bucket:
                    continue

                sep = Gtk.Separator()
                sep.get_style_context().add_class("separator")
                vbox.pack_start(sep, False, False, 4)

                section_label = Gtk.Label(label=label_text)
                section_label.get_style_context().add_class("section-label")
                section_label.set_halign(Gtk.Align.START)
                vbox.pack_start(section_label, False, False, 0)

                utilization = bucket.get("utilization", 0)
                utilization_decimal = utilization / 100
                rem = 100 - int(utilization)
                rem_decimal = 1.0 - utilization_decimal

                val = Gtk.Label(label=f"{rem}%")
                val.get_style_context().add_class("metric-value")
                color = get_color_for_pct(utilization_decimal)
                val.set_markup(
                    f'<span foreground="{color}" font_weight="bold" font="28">{rem}%</span>'
                )
                val.set_halign(Gtk.Align.START)
                vbox.pack_start(val, False, False, 0)

                bar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                bar_box.get_style_context().add_class("bar-bg")

                bar = Gtk.LevelBar()
                bar.set_min_value(0)
                bar.set_max_value(1.0)
                bar.set_value(rem_decimal)
                bar.set_size_request(-1, 8)

                # Remove default offset classes and add custom
                bar.remove_offset_value("low")
                bar.remove_offset_value("high")
                bar.remove_offset_value("full")
                bar_css = Gtk.CssProvider()
                bar_css.load_from_data(
                    f"""
                    levelbar trough {{
                        background-color: #2a2a4a;
                        border-radius: 4px;
                        min-height: 8px;
                    }}
                    levelbar trough block.filled {{
                        background-color: {color};
                        border-radius: 4px;
                        min-height: 8px;
                    }}
                """.encode()
                )
                bar.get_style_context().add_provider(
                    bar_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )

                bar_box.pack_start(bar, True, True, 0)
                vbox.pack_start(bar_box, False, False, 0)

                # Reset time
                resets = bucket.get("resets_at")
                reset_str = format_reset_time(resets)
                reset_lbl = Gtk.Label(label=f"Resets in {reset_str}")
                reset_lbl.get_style_context().add_class("reset-label")
                reset_lbl.set_halign(Gtk.Align.START)
                vbox.pack_start(reset_lbl, False, False, 0)
        else:
            err_label = Gtk.Label(
                label="Unable to fetch usage data.\nCheck token and connectivity."
            )
            err_label.get_style_context().add_class("status-err")
            vbox.pack_start(err_label, False, False, 8)

        if rate_limit_until:
            remaining = rate_limit_until - datetime.now()
            remaining_secs = max(int(remaining.total_seconds()), 0)
            mins, secs = divmod(remaining_secs, 60)
            retry_label = Gtk.Label(
                label=f"⏳ Rate limited — retry in {mins}m {secs:02d}s"
            )
            retry_label.get_style_context().add_class("status-warn")
            retry_label.set_halign(Gtk.Align.START)
            vbox.pack_start(retry_label, False, False, 0)

        # Footer
        sep = Gtk.Separator()
        sep.get_style_context().add_class("separator")
        vbox.pack_start(sep, False, False, 4)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        updated = Gtk.Label(label=f"Updated: {last_updated}")
        updated.get_style_context().add_class("metric-sub")
        footer.pack_start(updated, False, False, 0)

        refresh_btn = Gtk.Button(label="↻ Refresh")
        refresh_btn.set_relief(Gtk.ReliefStyle.NONE)
        refresh_css = Gtk.CssProvider()
        refresh_css.load_from_data(b"""
            button { color: #8888aa; background: transparent; border: none; padding: 2px 8px; }
            button:hover { color: #e0e0ff; }
        """)
        refresh_btn.get_style_context().add_provider(
            refresh_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        refresh_btn.connect("clicked", lambda _: (self.destroy(), app.force_refresh()))
        footer.pack_end(refresh_btn, False, False, 0)

        vbox.pack_start(footer, False, False, 0)

        # Version info
        version_label = Gtk.Label(label=f"v{__version__}")
        version_label.get_style_context().add_class("reset-label")
        version_label.set_halign(Gtk.Align.CENTER)
        version_label.set_margin_top(4)
        vbox.pack_start(version_label, False, False, 0)

        self.add(vbox)
        self.show_all()


# ── Token entry dialog ──────────────────────────────────────────────────────


class TokenDialog(Gtk.Dialog):
    def __init__(self, parent=None):
        super().__init__(title="Claude OAuth Token", transient_for=parent, flags=0)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        self.set_default_size(450, -1)

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label()
        label.set_markup(
            "Enter your Claude OAuth token.\n"
            "<small>Get it from <b>~/.claude/.credentials.json</b> (Claude Code)\n"
            "or browser DevTools → Network → api.anthropic.com headers.</small>"
        )
        label.set_line_wrap(True)
        box.pack_start(label, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("sk-ant-oat01-...")
        self.entry.set_visibility(False)
        box.pack_start(self.entry, False, False, 0)

        self.show_all()

    def get_token(self) -> str:
        return self.entry.get_text().strip()


# ── Main App ────────────────────────────────────────────────────────────────


class ClaudeUsageApp:
    def __init__(self):
        self.usage_data: dict | None = None
        self.subscription_info: dict | None = None
        self.last_updated: str = "never"
        self.token: str | None = None
        self.running = True
        self.rate_limit_until: datetime | None = None
        self.last_notification_threshold: int = (
            0  # Track last notified threshold (0, 75, 90, 100)
        )
        self.startup_notification_sent: bool = False
        self.detail_window: "UsageDetailWindow | None" = None

        Notify.init(APP_NAME)

        icon_path = write_icon(0.0, 0.0, error=True)
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title(APP_NAME)
        self.indicator.set_label("", "")

        # Build menu
        self.menu = Gtk.Menu()

        self.item_5h = Gtk.MenuItem(label="5h: --%")
        self.item_5h.set_sensitive(False)
        self.menu.append(self.item_5h)

        self.item_7d = Gtk.MenuItem(label="7d: --%")
        self.item_7d.set_sensitive(False)
        self.menu.append(self.item_7d)

        self.item_extra = Gtk.MenuItem(label="")
        self.item_extra.set_sensitive(False)
        self.item_extra.set_no_show_all(True)
        self.menu.append(self.item_extra)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_details = Gtk.MenuItem(label="Show Details…")
        item_details.connect("activate", self.on_show_details)
        self.menu.append(item_details)

        item_refresh = Gtk.MenuItem(label="Refresh Now")
        item_refresh.connect("activate", lambda _: self.force_refresh())
        self.menu.append(item_refresh)

        item_token = Gtk.MenuItem(label="Set Token…")
        item_token.connect("activate", self.on_set_token)
        self.menu.append(item_token)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self.on_quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        # Load token and subscription info
        self.token = load_token()
        self.subscription_info = load_subscription_info()
        if not self.token:
            GLib.timeout_add_seconds(2, self._prompt_token_once)

        # Start background polling
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()

    def _prompt_token_once(self):
        if not self.token:
            self.on_set_token(None)
        return False

    def _notify(self, title: str, body: str, urgent: bool = False) -> None:
        try:
            n = Notify.Notification.new(
                title,
                body,
                "dialog-warning" if urgent else "dialog-information",
            )
            if urgent:
                n.set_urgency(Notify.Urgency.CRITICAL)
            n.show()
        except Exception as e:
            print(f"[claude-usage] Notify failed: {e}", file=sys.stderr)

    def _poll_loop(self):
        """Background thread: fetch usage periodically."""
        while self.running:
            try:
                fresh = load_token()
                if fresh:
                    self.token = fresh
                if self.token:
                    data = fetch_usage(self.token)
                    GLib.idle_add(self._update_ui, data)
            except RateLimitError as e:
                self.rate_limit_until = datetime.now() + timedelta(
                    seconds=e.retry_after
                )
                print(
                    f"[claude-usage] Rate limited, retry after {e.retry_after}s",
                    file=sys.stderr,
                )
                GLib.idle_add(self._set_rate_limit_ui)
                time.sleep(e.retry_after)
                self.rate_limit_until = None
                continue
            except AuthExpiredError as e:
                print(f"[claude-usage] Auth expired: {e}", file=sys.stderr)
                self.token = None
                GLib.idle_add(self._update_ui, None)
                GLib.idle_add(self._notify_reauth_needed)
            except Exception as e:
                print(f"[claude-usage] Poll error: {e}", file=sys.stderr)
            time.sleep(REFRESH_INTERVAL_SEC)

    def _notify_reauth_needed(self) -> bool:
        self._notify(
            "Claude session expired",
            "Refresh token is no longer valid. Run `claude` once in a "
            "terminal to sign in again, or use 'Set Token…'.",
            urgent=True,
        )
        return False

    def force_refresh(self):
        """Immediate refresh triggered by user."""

        def _do():
            if self.token:
                try:
                    data = fetch_usage(self.token)
                    GLib.idle_add(self._update_ui, data)
                except RateLimitError as e:
                    self.rate_limit_until = datetime.now() + timedelta(
                        seconds=e.retry_after
                    )
                    print(
                        f"[claude-usage] Rate limited during refresh, retry after {e.retry_after}s",
                        file=sys.stderr,
                    )
                    GLib.idle_add(self._set_rate_limit_ui)
                except AuthExpiredError as e:
                    print(f"[claude-usage] Auth expired during refresh: {e}", file=sys.stderr)
                    self.token = None
                    GLib.idle_add(self._update_ui, None)
                    GLib.idle_add(self._notify_reauth_needed)
                except Exception as e:
                    print(f"[claude-usage] Refresh error: {e}", file=sys.stderr)

        threading.Thread(target=_do, daemon=True).start()

    def _set_rate_limit_ui(self):
        """On rate limit, show cached usage if available, otherwise ERR."""
        self.last_updated = datetime.now().strftime("%H:%M:%S")
        if self.usage_data:
            self._update_ui(self.usage_data)
        else:
            self.indicator.set_label("", "")
            icon_path = write_icon(0.0, 0.0, error=True)
            self.indicator.set_icon_full(icon_path, "Error")

            self.item_5h.set_label("5h: rate limited")
            self.item_7d.set_label("7d: rate limited")
        return False

    def _update_ui(self, data: dict | None):
        """Update indicator label + icon from fetched data (runs on GTK thread)."""
        self.usage_data = data
        self.last_updated = datetime.now().strftime("%H:%M:%S")

        if data:
            five = data.get("five_hour", {}) or {}
            seven = data.get("seven_day", {}) or {}
            u5 = five.get("utilization", 0)
            u7 = seven.get("utilization", 0)

            # API returns percentage (0-100)
            pct5 = int(u5)
            u5_decimal = u5 / 100

            pct7 = int(u7)
            u7_decimal = u7 / 100

            rem5 = 100 - pct5
            rem7 = 100 - pct7

            dominant = max(u5_decimal, u7_decimal)

            self.indicator.set_label("", "")
            icon_path = write_icon(u5_decimal, u7_decimal)
            self.indicator.set_icon_full(icon_path, f"{rem5}% | {rem7}%")

            self.item_5h.set_label(
                f"5h: {rem5}%  (resets {format_reset_time(five.get('resets_at'))})"
            )
            self.item_7d.set_label(
                f"7d: {rem7}%  (resets {format_reset_time(seven.get('resets_at'))})"
            )

            # Extra usage (pay-as-you-go credits)
            extra = data.get("extra_usage") or {}
            if extra and extra.get("is_enabled"):
                used = extra.get("used_credits") or 0
                limit = extra.get("monthly_limit") or 0
                self.item_extra.set_label(
                    f"Extra: {float(used):.0f}/{float(limit):.0f} credits"
                )
                self.item_extra.show()
            else:
                self.item_extra.hide()

            # Send notifications at specific thresholds only
            self._check_and_notify_threshold(pct5, pct7, dominant)
        else:
            self.indicator.set_label("", "")
            icon_path = write_icon(0.0, 0.0, error=True)
            tooltip = "No token — run Claude Login" if not self.token else "Error"
            self.indicator.set_icon_full(icon_path, tooltip)

            if not self.token:
                self.item_5h.set_label("5h: not logged in")
                self.item_7d.set_label("7d: run 'Claude Login…'")
            else:
                self.item_5h.set_label("5h: error")
                self.item_7d.set_label("7d: error")

        return False

    def _check_and_notify_threshold(self, pct5: int, pct7: int, dominant: float):
        """Send notifications only at specific thresholds: startup, 75%, 90%, 100%"""
        # Startup notification (first successful data fetch)
        if not self.startup_notification_sent:
            self.startup_notification_sent = True
            n = Notify.Notification.new(
                "✓ Claude Usage Widget Started",
                f"Current usage: 5h: {pct5}%  |  7d: {pct7}%",
                "dialog-information",
            )
            n.show()
            return

        # Determine current threshold
        pct_val = int(dominant * 100)
        current_threshold = 0

        if pct_val >= 100:
            current_threshold = 100
        elif pct_val >= 90:
            current_threshold = 90
        elif pct_val >= 75:
            current_threshold = 75

        # Only notify if crossing a new threshold
        if current_threshold > self.last_notification_threshold:
            if current_threshold == 75:
                n = Notify.Notification.new(
                    "⚠️ Claude Usage: 75%",
                    f"5h: {pct5}%  |  7d: {pct7}%\nApproaching rate limits.",
                    "dialog-warning",
                )
                n.set_urgency(Notify.Urgency.NORMAL)
            elif current_threshold == 90:
                n = Notify.Notification.new(
                    "⚠️ Claude Usage: 90%",
                    f"5h: {pct5}%  |  7d: {pct7}%\nClose to rate limits!",
                    "dialog-warning",
                )
                n.set_urgency(Notify.Urgency.CRITICAL)
            elif current_threshold == 100:
                n = Notify.Notification.new(
                    "🛑 Claude Usage: 100%",
                    f"5h: {pct5}%  |  7d: {pct7}%\nRate limit reached!",
                    "dialog-error",
                )
                n.set_urgency(Notify.Urgency.CRITICAL)
            else:
                return  # No notification for this threshold

            n.show()
            self.last_notification_threshold = current_threshold

    def on_show_details(self, _widget):
        if self.detail_window is not None:
            self.detail_window.destroy()
            return

        rate_limited = self.rate_limit_until and self.rate_limit_until > datetime.now()
        if rate_limited:
            token_status = "Rate limited"
        elif self.usage_data:
            token_status = "Connected"
        elif not self.token:
            token_status = "No token"
        else:
            token_status = "Error"

        window = UsageDetailWindow(
            self.usage_data,
            self.last_updated,
            token_status,
            self.subscription_info,
            self.rate_limit_until if rate_limited else None,
        )
        window.connect("destroy", self._on_detail_window_destroyed)
        self.detail_window = window
        window.show_all()

    def _on_detail_window_destroyed(self, _window):
        self.detail_window = None

    def on_set_token(self, _widget):
        dialog = TokenDialog()
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            token = dialog.get_token()
            if token:
                self.token = token
                save_token(token)
                self.force_refresh()
        dialog.destroy()

    def on_quit(self, _widget):
        self.running = False
        Notify.uninit()
        Gtk.main_quit()

    def run(self):
        Gtk.main()


if __name__ == "__main__":
    app = ClaudeUsageApp()
    app.run()
