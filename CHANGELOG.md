# Changelog

All notable changes to this project will be documented in this file.

## [1.1.0] - 2026-04-20

### Fixed
- **"ERR" after a fresh boot when Claude Code hasn't been run yet** — the widget no longer needs you to launch `claude` once per boot just to prime its token. Access tokens in `~/.claude/.credentials.json` are usually expired by the time the widget reads them, and the old code blindly used them → instant `ERR`. The widget now transparently refreshes the token with the stored `refreshToken`, exactly the same way Claude Code does on its own startup.
- **Widget would silently stop working after ~1 hour** — expired tokens were previously trusted indefinitely. The widget now refreshes proactively (when the access token is within 60 s of expiry) and reactively (on 401/403 from the usage API, with a single silent retry).

### Added
- **Proactive + reactive token refresh** (`_refresh_access_token`) — hits the same `https://platform.claude.com/v1/oauth/token` endpoint Claude Code uses, with the same `client_id` (`9d1c250a-e61b-44d9-88ed-5944d1962f5e`), extracted directly from the installed Claude Code binary for auditability.
- **`AuthExpiredError`** — distinguishes 401/403 from transient errors so the poll loop knows when to stop retrying and notify the user.
- **Re-auth notification** — if the refresh token itself has expired (rare, happens after very long inactivity), the widget pops a desktop notification telling the user to run `claude` once in a terminal, rather than silently failing.

### Changed
- **Token storage format** — widget config now stores the full OAuth bundle (`accessToken`, `refreshToken`, `expiresAt`, `scopes`, `subscriptionType`, `rateLimitTier`) instead of a bare `oauth_token` string. Claude Code's `~/.claude/.credentials.json` is still read as a fallback on first launch but is never written to by the widget — avoiding refresh-token-rotation races with a running Claude Code process.
- **User-Agent** — now includes `__version__` instead of the hardcoded `1.0`.

### Internal
- `threading.Lock` around refresh to prevent concurrent poll + force-refresh from double-spending the single-use refresh token.
- The widget deliberately does **not** implement the full OAuth authorize/PKCE handshake. Initial sign-in is always expected to happen via `claude` (Claude Code). This avoids duplicating Anthropic's authorize UI and keeps the widget's scope minimal — refresh and fetch only.

---

## [1.0.10] - 2026-04-18

### Changed
- **Icon completely redesigned** — the tray icon is now a 153×32 self-contained composite PNG: `[Anthropic logo]  5H [progress bar]  1W [progress bar]`. Both bars are pill-shaped (Material Design 3 style, 38×16 px), color-coded by utilization (white → yellow → orange → red), and carry the remaining-% value (8 pt Bold) centered inside each bar with a one-character gap separating the logo from the `5H` label
- **Fill represents remaining quota** (100 % = full / safe, 0 % = empty / out); warning color threshold still keys off utilization so high usage still renders red even with a nearly-empty fill
- **`set_label()` always empty** — panel label was unreliable on GNOME Shell 46 with `ubuntu-appindicators` (non-ASCII characters silently dropped); all information is baked into the icon PNG

### Fixed
- **Tray showing only the Anthropic logo with no usage data** — two root causes eliminated:
  1. `AppIndicator3.set_label()` silently drops NerdFont PUA characters on GNOME Shell 46 + `ubuntu-appindicators`; usage data is now rendered directly into the icon PNG via Cairo + PangoCairo
  2. `libayatana-appindicator`'s `set_icon_full()` short-circuits on unchanged paths (`g_strcmp0` in `app-indicator.c:2051`), leaving the initial ERR icon pinned permanently; `write_icon()` now uses monotonically-numbered filenames (`icon-N.png`) so every update forces a reload
- **Chromatic fringing on icon text** — Cairo's default `ANTIALIAS_SUBPIXEL` encoded glyph edges as colored subpixel contributions, producing green/red halos on flat-color text in the PNG; `_apply_gray_antialias()` forces `cairo.ANTIALIAS_GRAY`

### Added / changed internal APIs
- `_rounded_rect_path()` — 4-arc canonical pill path (cairo.org / gPodder pattern)
- `_draw_progress_bar()` — pill bar with clipped fill and two-pass inverse text rendering
- `_apply_gray_antialias()` — grayscale AA for glyph rendering in icon PNGs
- `write_icon(pct5, pct7, error)` — accepts per-window utilization floats (0–1); uses sequential filenames
- Removed `_draw_battery()`, `get_icon_for_pct()`, dead `FiraCode Nerd Font` setup

### install.sh
- Added `gir1.2-rsvg-2.0`, `gir1.2-pango-1.0`, `python3-cairo` to dependency check and `apt install` suggestion — required for SVG rendering and Cairo text, were always used at runtime but previously omitted from the installer

---

## [1.0.3] - 2026-02-19

### Fixed
- **429 rate limit handling** — widget shows ERR and backs off 10 minutes before retrying instead of hammering the API every 2 minutes while rate limited

---

## [1.0.2] - 2026-02-19

### Fixed
- **ERR on startup / after idle** — widget now re-reads `~/.claude/.credentials.json` on every poll cycle so a token refreshed by Claude Code overnight is picked up automatically, instead of staying stuck on the expired token loaded at startup

---

## [1.0.1] - 2026-02-19

### Fixed
- **Weekly reset timer** now shows days correctly (e.g. `4d 23h` instead of `119h 0m`)
- **Poll thread** wrapped in exception handler so a transient error no longer silently kills background refresh
- **Extra usage** (pay-as-you-go credits) was present in the API response but never displayed — it now appears in both the tray menu and the "Show Details" window

### Added
- **Extra Usage section** in the detail popup: shows monthly credit utilization with a colour-coded percentage and `used / limit` credits breakdown
- **Extra credits menu item**: displayed in the tray menu when extra usage is enabled on the account

---

## [1.0.0] - 2026-02-15

### Added
- Initial release: Claude AI Usage Widget for Linux
- System tray indicator showing 5-hour utilisation percentage
- Colour-coded "C" icon (green → yellow → orange → red)
- Click menu with 5h and 7d utilisation + reset timers
- "Show Details" popup with progress bars, reset timers, and subscription plan
- Threshold-based desktop notifications: startup, 75%, 90%, 100%
- Auto-detection of OAuth token from `~/.claude/.credentials.json`
- Autostart on login via `.desktop` entry
- `install.sh` / `uninstall.sh` helper scripts
- `validate.sh` pre-release quality-check script
- MIT licence — open source by Statotech Systems
