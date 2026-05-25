# Suno AI MCP Connector

A self-contained Python [MCP](https://modelcontextprotocol.io) server that exposes
[Suno AI](https://suno.com) music generation to Claude Code, Claude Desktop,
Cursor, Codex, or any MCP-compatible client — using **your own Suno
subscription** via your browser session cookie (no third-party API key needed).

```
prompt ──▶ Claude ──▶ suno MCP ──▶ Suno  ──▶ 🎵 audio_url
                       (your cookie / subscription)
```

> ⚠️ **Unofficial.** Suno has no public API; this drives the same private
> endpoints the website uses. Suno's ToS prohibits automated access — use a
> burner account if you're worried about bans. It can break any time Suno
> changes their auth or web UI.

---

## How it works

Suno's web app authenticates through [Clerk](https://clerk.com). This server
reuses your logged-in `suno.com` cookies: it reads the `__client` JWT, exchanges
it for a short-lived bearer token, and calls Suno's `studio-api` endpoints — the
same flow as [gcui-art/suno-api](https://github.com/gcui-art/suno-api), reimplemented
in Python. There are two execution paths:

| Path | Used for | Captcha handling |
| --- | --- | --- |
| **API** (httpx) | lyrics, credits, status, feed, clip info, extend, concat | Fails fast if Suno demands hCaptcha |
| **Browser** (CloakBrowser, stealth Chromium) | song generation | Avoids most captchas via fingerprint stealth; falls back to a headed window where you can solve one manually |

By default (`SUNO_GENERATION_MODE=auto`) generation tries the API first and falls
back to the browser only when a captcha is required.

---

## Prerequisites

- **Python 3.11+**
- An active **Suno account** (free or paid) you're logged into in a browser
- ~200 MB free disk if you use the browser fallback (CloakBrowser downloads a patched Chromium on first launch)

---

## 1. Install

```bash
git clone https://github.com/roshank8s/suno-ai-mcp.git
cd suno-ai-mcp

python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -e .
```

This installs the package and two console scripts: `suno-mcp` (the server) and
`suno-mcp-extract-cookie` (the cookie helper).

---

## 2. Get your Suno cookie

You need the full `Cookie:` header from a logged-in `suno.com` session (it must
contain a `__client` value). Pick whichever option works for you.

> **Why the awkwardness?** Chrome 127+ (mid-2024) added app-bound encryption for
> its cookie store that needs SYSTEM-level impersonation to decrypt, and Chrome
> refuses `--remote-debugging-port` on signed-in default profiles. So plain disk
> extraction (Option C) usually fails on modern Chrome — prefer A or B.

### Option A — Manual paste (most reliable, ~30 seconds)

1. Open [suno.com](https://suno.com) and log in.
2. **F12** → **Network** tab → refresh the page.
3. Click any request to `suno.com` or `studio-api.prod.suno.com` → **Headers** →
   **Request Headers**.
4. Copy the entire value of the `Cookie:` header (one long string). Confirm it
   contains `__client=...`.
5. Copy the env template and paste it in:
   ```bash
   cp .env.example .env        # Windows: copy .env.example .env
   ```
   Set `SUNO_COOKIE=<the full cookie string>` in `.env` and save.

### Option B — Fresh login in a Playwright window (automated)

Launches a controlled Chromium window; you log in once and the cookies are
captured straight from the live context and written to `.env`:

```bash
python -m suno_mcp.extract_cookie --fresh-login --write
```

When the window opens, sign in to Suno. The script polls for the `__client`
cookie and exits once it appears. (Requires Playwright's Chromium:
`python -m playwright install chromium`.)

### Option C — Disk extraction (legacy browsers only)

Works on older Chrome / Edge / Firefox; usually reports "0 cookies" on Chrome 127+.

```bash
python -m suno_mcp.extract_cookie --write
```

Other helper modes: `--from-json <cookie-editor-export.json>` (supports encrypted
v2 exports with `--password`), `--cdp --auto-launch`, `--show`. Run
`python -m suno_mcp.extract_cookie -h` for the full list.

---

## 3. Wire it into your MCP client

Replace `/path/to/suno-ai-mcp` below with where you cloned the repo, and use the
Python from your `.venv`.

### Claude Code (CLI)

```bash
claude mcp add suno \
  -e SUNO_GENERATION_MODE=auto \
  -e SUNO_BROWSER_HEADLESS=false \
  -- /path/to/suno-ai-mcp/.venv/bin/python -m suno_mcp
```

On Windows use `...\.venv\Scripts\python.exe`. If `SUNO_COOKIE` is set in `.env`
the server will load it automatically; otherwise add `-e SUNO_COOKIE=...`.

### Claude Code / Claude Desktop / Cursor (JSON config)

Add to your MCP config — `~/.claude.json` or a project `.mcp.json` for Claude
Code, `%APPDATA%\Claude\claude_desktop_config.json` (or
`~/Library/Application Support/Claude/...` on macOS) for Claude Desktop, then
restart the app:

```json
{
  "mcpServers": {
    "suno": {
      "command": "/path/to/suno-ai-mcp/.venv/bin/python",
      "args": ["-m", "suno_mcp"],
      "env": {
        "SUNO_COOKIE": "<your full cookie header>",
        "SUNO_GENERATION_MODE": "auto",
        "SUNO_BROWSER_HEADLESS": "false"
      }
    }
  }
}
```

You can omit the `env` block entirely if you keep everything in `.env`.

---

## 4. Smoke test

Start the server directly — it will block on stdio (that's expected for an MCP
server). Ctrl+C to stop; the point is to confirm it starts with no errors:

```bash
python -m suno_mcp
```

To verify auth actually works, check your credit balance:

```bash
python -c "import asyncio, os; from dotenv import load_dotenv; from suno_mcp.suno_client import SunoClient; load_dotenv();\
exec('async def go():\n async with SunoClient(os.environ[\"SUNO_COOKIE\"]) as c:\n  print(await c.get_credits())'); asyncio.run(go())"
```

If your credit balance prints, you're good. (Inside Claude, just ask it to run
the `get_credits` tool.)

---

## Tools exposed

**Generation**

| Tool | Description |
| --- | --- |
| `generate_music` | Generate from a free-form prompt (Simple mode) — api → browser fallback |
| `custom_generate` | Advanced mode: explicit lyrics + style + title, plus Personalize options (`persona_id`, `artist_clip_id`, `cover_clip_id`) — api → browser fallback |
| `extend_song` | Extend an existing clip from a timestamp |
| `concatenate_song` | Stitch an extended clip into a full song |

**Lyrics**

| Tool | Description |
| --- | --- |
| `generate_lyrics` | Generate standalone lyrics from a theme |
| `generate_lyrics_pair` | "Generate lyrics" button: returns two A/B options |
| `get_song_lyrics` | Lyric text + style tags for a clip |
| `get_aligned_lyrics` | Word-level lyric timing + waveform |
| `cowrite_lyrics` | AI co-write: rewrite/continue selected lyrics in context |
| `lyrics_infill` | Fill a gap in lyrics given prefix/suffix |

**Library & discovery (read-only)**

| Tool | Description |
| --- | --- |
| `get_user` | Logged-in user's profile |
| `get_credits` | Credit balance, plan, monthly usage, free-gen allowances |
| `list_songs` / `get_clip` | List songs / single-clip metadata |
| `get_songs_by_ids` | Batch-fetch clips by ID |
| `get_similar_songs` | Songs similar to a clip |
| `search_songs` | Search public Suno content |
| `list_playlists` / `get_playlist` | Your playlists / one playlist with clips |
| `list_projects` | Your projects (workspaces) |
| `list_personas` | Your saved voice/style personas |
| `get_recommend_styles` | Suno's suggested style tags |

**Audio editing** *(creates new clips; may consume credits)*

| Tool | Description |
| --- | --- |
| `separate_stems` | Split a clip into stems (vocals / instrumental) |
| `crop_clip` | Keep or remove a `[start, end]` seconds range |
| `fade_clip` | Apply fade-in / fade-out |
| `adjust_speed` | Change playback speed (optionally keep pitch) |

**Downloads**

| Tool | Description |
| --- | --- |
| `download_clip` | Save a clip's audio to a local file |
| `download_lyrics` | Save lyrics to a file (`txt` plain or `lrc` timed) |

**Clip management** *(destructive)*

| Tool | Description |
| --- | --- |
| `trash_clip` | Move clips to trash, or restore (`trash=false`) |
| `delete_clip` | Permanently delete clips — **cannot be undone** |

All tools except generation use the cookie/JWT **API path**. Generation tries the
API first and falls back to the stealth browser on captcha.

### Advanced generation workflow

These map to the web Create page's **Advanced** mode (lyrics + style + title):

1. **Lyrics** — write your own, or use `generate_lyrics_pair` ("Generate lyrics",
   two options) / `cowrite_lyrics` ("Enhance lyrics", e.g. *"make it happier"*).
2. **Style** — pass `tags` (the styles box) and `negative_tags` (Exclude styles).
   Use `get_recommend_styles` for the suggested style chips.
3. **Personalize** — apply a saved voice/style with `persona_id` (see
   `list_personas`), personalize from a song with `artist_clip_id`
   (+ `artist_start_s`/`artist_end_s`, the "Add Voice" feature), or cover a song
   with `cover_clip_id`.
4. **Generate** — call `custom_generate` with the above, then `download_clip` /
   `download_lyrics` to save the result.

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SUNO_COOKIE` | *(required)* | Full `Cookie:` header from suno.com (must contain `__client`) |
| `SUNO_GENERATION_MODE` | `auto` | `auto` / `api` / `browser` |
| `SUNO_BROWSER_HEADLESS` | `false` | Hide the CloakBrowser window |
| `SUNO_DEFAULT_MODEL` | `chirp-fenix` | Model used when a tool call omits it |
| `SUNO_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Known model identifiers: `chirp-v3-5` (v3.5), `chirp-auk` / `chirp-bluejay`
(v4.5), `chirp-crow` (v5), `chirp-fenix` (v5.5).

---

## When generation fails with "captcha required"

1. Set `SUNO_BROWSER_HEADLESS=false` so the CloakBrowser window is visible, then
   solve any hCaptcha that appears — Suno trusts your fingerprint for a while
   afterward.
2. If CloakBrowser isn't installed, the fallback surfaces an install hint:
   `pip install cloakbrowser` (~200 MB, auto-downloads Chromium on first launch).
3. As a last resort, set `SUNO_GENERATION_MODE=browser` to always drive the web UI.

## Troubleshooting

- **`Failed to get session id` / `SUNO_COOKIE is stale`** — re-copy the cookie from
  a freshly-loaded suno.com tab (the session JWT is short-lived).
- **`SUNO_COOKIE missing or does not contain the __client value`** — you copied
  only part of the cookie. The full long string must be on one line.
- **Hangs on first browser run** — CloakBrowser is downloading the patched
  Chromium (~200 MB). Watch the terminal for progress.
- **`0 suno.com cookies` from disk extraction** — Chrome 127+ app-bound
  encryption; use Option A or B instead.

---

## License

MIT — see [LICENSE](LICENSE). Provided as-is, with no affiliation to or
endorsement by Suno. You are responsible for complying with Suno's Terms of
Service.
