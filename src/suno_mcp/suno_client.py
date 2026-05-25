"""
Suno HTTP client — replicates gcui-art/suno-api auth flow in Python.

Auth flow (Clerk-based):
  1. User supplies full cookie header containing `__client` JWT
  2. GET https://auth.suno.com/v1/client?... with Authorization: <__client>
     -> response.last_active_session_id
  3. POST https://auth.suno.com/v1/client/sessions/{sid}/tokens?...
     -> response.jwt (the bearer token for studio-api calls)
  4. JWT expires quickly — call keep_alive() before every studio-api call

studio-api.prod.suno.com endpoints used:
  POST /api/generate/v2/             -> generate songs (requires captcha token)
  POST /api/generate/lyrics/         -> kick off lyrics generation
  GET  /api/generate/lyrics/{id}     -> poll lyrics
  GET  /api/feed/v2?ids=a,b,c        -> song status / metadata
  GET  /api/clip/{id}                -> single clip detail
  GET  /api/billing/info/            -> credits
  POST /api/c/check                  -> is captcha required?
  POST /api/generate/concat/v2/      -> concatenate clips into full song
  GET  /api/gen/{id}/aligned_lyrics/v2/  -> word-level lyric timing
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

STUDIO_BASE = "https://studio-api.prod.suno.com"
CLERK_BASE = "https://auth.suno.com"
CLERK_VERSION = "5.117.0"
CLERK_API_VERSION = "2025-11-10"
DEFAULT_MODEL = "chirp-fenix"  # Suno v5.5
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class SunoAuthError(RuntimeError):
    """Raised when cookie is missing/invalid or Clerk auth fails."""


class SunoCaptchaRequired(RuntimeError):
    """Raised when an endpoint demands a captcha token and we don't have one."""


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    """Parse a raw `Cookie:` header value into a dict."""
    jar: dict[str, str] = {}
    c = SimpleCookie()
    c.load(cookie_header)
    for key, morsel in c.items():
        jar[key] = morsel.value
    # SimpleCookie misses some values when keys have unusual characters — fall back
    if not jar:
        for part in cookie_header.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                jar[k.strip()] = v.strip()
    return jar


def _jwt_exp(token: str) -> int | None:
    """Return the `exp` claim (epoch seconds) from a JWT, or None on parse failure."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        return int(payload.get("exp", 0)) or None
    except Exception:  # noqa: BLE001
        return None


def _pick_active_clerk_cookies(cookies: dict[str, str]) -> tuple[str | None, str | None]:
    """
    Clerk now uses multi-session cookies with random suffixes
    (e.g. __client_Jnxw-muT, __session_Jnxw-muT). Pick the freshest unexpired
    __session JWT and the matching __client. Returns (client, session) values
    or (None, None) if nothing usable is found.
    """
    now = int(time.time())
    best: tuple[int, str, str | None] | None = None  # (exp, session, client)
    for name, value in cookies.items():
        if not name.startswith("__session"):
            continue
        if name.startswith("__session_uat"):
            continue
        exp = _jwt_exp(value)
        if not exp or exp < now + 30:  # need at least 30s of life left
            continue
        suffix = name[len("__session"):]  # "" or "_<id>"
        client_name = f"__client{suffix}"
        client_val = cookies.get(client_name) or cookies.get("__client")
        if best is None or exp > best[0]:
            best = (exp, value, client_val)
    if best:
        return best[2], best[1]
    return cookies.get("__client"), None


def _build_lrc(aligned: dict[str, Any], title: str | None) -> str:
    """
    Best-effort LRC (timed lyrics) from an aligned_lyrics/v2 response.
    Falls back to the plain aligned_lyrics text if no per-word timing is found.
    """
    words = aligned.get("aligned_words") or []

    def _start(word: dict[str, Any]) -> float | None:
        for key in ("start_s", "start_time", "start", "begin_s", "p_start"):
            val = word.get(key)
            if isinstance(val, (int, float)):
                return float(val)
        return None

    if not any(_start(w) is not None for w in words):
        return aligned.get("aligned_lyrics") or ""

    lines: list[str] = []
    if title:
        lines.append(f"[ti:{title}]")
    for w in words:
        start = _start(w)
        text = (w.get("word") or w.get("text") or "").strip()
        if start is None or not text:
            continue
        minutes = int(start // 60)
        seconds = start - minutes * 60
        lines.append(f"[{minutes:02d}:{seconds:05.2f}]{text}")
    return "\n".join(lines)


class SunoClient:
    """Async client for the Suno studio API. Use as `async with SunoClient(...)`."""

    def __init__(self, cookie_header: str) -> None:
        if not cookie_header or "__client" not in cookie_header:
            raise SunoAuthError(
                "SUNO_COOKIE missing or does not contain a `__client` value. "
                "Copy the full Cookie header from a logged-in suno.com session."
            )
        self._cookies: dict[str, str] = _parse_cookie_header(cookie_header)
        self._device_id = self._cookies.get("ajs_anonymous_id") or str(uuid.uuid4())
        self._sid: str | None = None
        self._jwt: str | None = None
        self._jwt_exp: int | None = None
        # Identify the active multi-session __client / __session pair
        self._client_cookie, session_jwt = _pick_active_clerk_cookies(self._cookies)
        if session_jwt:
            # We already have a fresh bearer — use it directly, skip Clerk dance.
            self._jwt = session_jwt
            self._jwt_exp = _jwt_exp(session_jwt)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SunoClient:
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": USER_AGENT,
                "Affiliate-Id": "undefined",
                "Device-Id": f'"{self._device_id}"',
                "x-suno-client": "Android prerelease-4nt180t 1.0.42",
                "X-Requested-With": "com.suno.android",
            },
        )
        await self._init_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def jwt(self) -> str | None:
        return self._jwt

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def _apply_set_cookie(self, resp: httpx.Response) -> None:
        for k, v in resp.cookies.items():
            self._cookies[k] = v

    async def _init_session(self) -> None:
        """
        Set up auth. If a fresh __session JWT is already in the cookies, use
        it directly and skip Clerk. Otherwise mint a new JWT via Clerk's
        /v1/client + /sessions/{sid}/tokens flow.
        """
        if self._jwt and self._jwt_exp and self._jwt_exp > time.time() + 30:
            logger.debug("Using __session JWT directly (skips Clerk)")
            return
        await self._clerk_renew()

    async def _clerk_renew(self) -> None:
        """Resolve the active session id and mint a fresh JWT via Clerk."""
        assert self._client is not None
        if not self._client_cookie:
            raise SunoAuthError(
                "No __client cookie found and no usable __session JWT in the "
                "cookies — cannot authenticate. Re-export your suno.com cookies."
            )
        if not self._sid:
            url = (
                f"{CLERK_BASE}/v1/client"
                f"?__clerk_api_version={CLERK_API_VERSION}"
                f"&_clerk_js_version={CLERK_VERSION}"
            )
            resp = await self._client.get(
                url,
                headers={
                    "Cookie": self._cookie_header(),
                    "Authorization": self._client_cookie,
                },
            )
            self._apply_set_cookie(resp)
            if resp.status_code != 200:
                raise SunoAuthError(
                    f"Clerk /v1/client returned {resp.status_code}: {resp.text[:300]}"
                )
            data = resp.json()
            resp_obj = data.get("response") or {}
            sid = resp_obj.get("last_active_session_id")
            if not sid:
                # Multi-session: pick the freshest session
                sessions = resp_obj.get("sessions") or []
                for s in sessions:
                    if s.get("status") == "active":
                        sid = s.get("id")
                        break
            if not sid:
                raise SunoAuthError(
                    "Failed to resolve session id from Clerk — your SUNO_COOKIE "
                    "is stale or the session has been signed out."
                )
            self._sid = sid

        url = (
            f"{CLERK_BASE}/v1/client/sessions/{self._sid}/tokens"
            f"?__clerk_api_version={CLERK_API_VERSION}"
            f"&_clerk_js_version={CLERK_VERSION}"
        )
        resp = await self._client.post(
            url,
            headers={
                "Cookie": self._cookie_header(),
                "Authorization": self._client_cookie,
            },
        )
        self._apply_set_cookie(resp)
        if resp.status_code != 200:
            raise SunoAuthError(
                f"Clerk token renewal returned {resp.status_code}: {resp.text[:300]}"
            )
        jwt = resp.json().get("jwt")
        if not jwt:
            raise SunoAuthError("Clerk token renewal returned no jwt")
        self._jwt = jwt
        self._jwt_exp = _jwt_exp(jwt)

    async def keep_alive(self) -> None:
        """Renew the JWT only if it's about to expire."""
        if self._jwt and self._jwt_exp and self._jwt_exp > time.time() + 30:
            return
        await self._clerk_renew()

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        assert self._client is not None
        await self.keep_alive()
        url = f"{STUDIO_BASE}{path}"
        resp = await self._client.request(
            method,
            url,
            json=json_body,
            params=params,
            headers={
                "Cookie": self._cookie_header(),
                "Authorization": f"Bearer {self._jwt}",
            },
            timeout=timeout,
        )
        self._apply_set_cookie(resp)
        return resp

    async def captcha_required(self) -> bool:
        resp = await self._api(
            "POST", "/api/c/check", json_body={"ctype": "generation"}
        )
        if resp.status_code != 200:
            logger.warning("captcha check %s: %s", resp.status_code, resp.text[:200])
            return True  # be conservative
        return bool(resp.json().get("required", False))

    async def get_credits(self) -> dict[str, Any]:
        resp = await self._api("GET", "/api/billing/info/")
        resp.raise_for_status()
        data = resp.json()
        return {
            "credits_left": data.get("total_credits_left"),
            "credits": data.get("credits"),
            "monthly_limit": data.get("monthly_limit"),
            "monthly_usage": data.get("monthly_usage"),
            "period": data.get("period"),
            "period_end": data.get("period_end"),
            "renews_on": data.get("renews_on"),
            "plan": data.get("plan"),
            "subscription_type": data.get("subscription_type"),
            "is_active": data.get("is_active"),
            "free_credits": {
                "persona_clips": data.get("free_persona_clips_remaining"),
                "cover_clips": data.get("free_cover_clips_remaining"),
                "remasters": data.get("free_remasters_remaining"),
            },
        }

    # ------------------------------------------------------------------ #
    # Generic JSON helpers used by the endpoints added below             #
    # ------------------------------------------------------------------ #

    async def _get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await self._api("GET", path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post_json(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        resp = await self._api("POST", path, json_body=body or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Account / library / discovery (read-only)                          #
    # ------------------------------------------------------------------ #

    async def get_user(self) -> dict[str, Any]:
        """Return the logged-in user's profile (id, handle, display name, email)."""
        return await self._get_json("/api/user/me")

    async def list_playlists(self, page: int = 1) -> dict[str, Any]:
        """List the user's own playlists."""
        return await self._get_json("/api/playlist/me", params={"page": page})

    async def get_playlist(self, playlist_id: str, page: int = 1) -> dict[str, Any]:
        """Get a single playlist with its clips."""
        return await self._get_json(
            f"/api/playlist/{playlist_id}", params={"page": page}
        )

    async def list_projects(self) -> dict[str, Any]:
        """List the user's projects (workspaces)."""
        return await self._get_json("/api/project/me")

    async def list_personas(self, page: int = 1) -> dict[str, Any]:
        """List the user's voice/style personas."""
        return await self._get_json("/api/persona/get-personas/", params={"page": page})

    async def search_songs(
        self,
        term: str,
        search_type: str = "public_song",
        size: int = 20,
        from_index: int = 0,
        rank_by: str = "most_relevant",
    ) -> dict[str, Any]:
        """Search public Suno content. search_type is the API enum (e.g. public_song)."""
        body = {
            "search_queries": [
                {
                    "name": search_type,
                    "search_type": search_type,
                    "term": term,
                    "from_index": from_index,
                    "size": size,
                    "rank_by": rank_by,
                }
            ]
        }
        return await self._post_json("/api/search/", body)

    async def get_similar(self, clip_id: str) -> dict[str, Any]:
        """Return clips similar to the given clip."""
        return await self._get_json("/api/clips/get_similar/", params={"id": clip_id})

    async def get_songs_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Batch-fetch clips by id (normalized like list_songs)."""
        data = await self._get_json(
            "/api/clips/get_songs_by_ids", params={"ids": ",".join(ids)}
        )
        return self._normalize_clips(data.get("clips", []))

    async def get_recommend_styles(self) -> dict[str, Any]:
        """Suno's suggested style tags (default_styles + co-occurring styles)."""
        return await self._get_json("/api/generate/get_recommend_styles")

    # ------------------------------------------------------------------ #
    # Lyrics tooling                                                     #
    # ------------------------------------------------------------------ #

    async def get_aligned_lyrics(self, clip_id: str) -> dict[str, Any]:
        """Word-level lyric timing + waveform for a finished clip."""
        return await self._get_json(f"/api/gen/{clip_id}/aligned_lyrics/v2/")

    async def get_song_lyrics(self, clip_id: str) -> dict[str, Any]:
        """Return the lyric text + style metadata for a clip."""
        clip = await self.get_clip(clip_id)
        md = clip.get("metadata") or {}
        return {
            "id": clip.get("id"),
            "title": clip.get("title"),
            "lyrics": md.get("prompt"),
            "description_prompt": md.get("gpt_description_prompt"),
            "tags": md.get("tags"),
        }

    async def cowrite_lyrics(
        self,
        instruction: str,
        selected: str = "",
        context_before: str = "",
        context_after: str = "",
        title: str = "",
        style: str = "",
    ) -> dict[str, Any]:
        """AI co-writing: rewrite/continue selected lyrics given surrounding context."""
        body = {
            "selected": selected,
            "context_before": context_before,
            "context_after": context_after,
            "instruction": instruction,
            "title": title,
            "style": style,
        }
        return await self._post_json("/api/generate/cowrite-lyrics/", body)

    async def lyrics_infill(
        self,
        prompt: str,
        context_lyrics_prefix: str = "",
        context_lyrics_edit: str = "",
        context_lyrics_suffix: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """Fill in lyrics for a gap given prefix/edit/suffix context."""
        body = {
            "prompt": prompt,
            "context_lyrics_prefix": context_lyrics_prefix,
            "context_lyrics_edit": context_lyrics_edit,
            "context_lyrics_suffix": context_lyrics_suffix,
            "title": title,
        }
        return await self._post_json("/api/generate/lyrics-infill/", body)

    # ------------------------------------------------------------------ #
    # Audio editing (these create new clips / jobs and may use credits)  #
    # ------------------------------------------------------------------ #

    async def separate_stems(self, clip_id: str) -> dict[str, Any]:
        """Split a clip into stems (e.g. vocals / instrumental)."""
        return await self._post_json(f"/api/edit/stems/{clip_id}")

    async def crop_clip(
        self,
        clip_id: str,
        crop_start_s: float,
        crop_end_s: float,
        is_crop_remove: bool = False,
        title: str = "",
    ) -> dict[str, Any]:
        """Crop a clip to (or remove) the [start, end] seconds range."""
        body = {
            "crop_start_s": crop_start_s,
            "crop_end_s": crop_end_s,
            "is_crop_remove": is_crop_remove,
            "title": title,
        }
        return await self._post_json(f"/api/edit/crop/{clip_id}", body)

    async def fade_clip(
        self,
        clip_id: str,
        fade_in_time: float = 0.0,
        fade_out_time: float = 0.0,
        title: str = "",
    ) -> dict[str, Any]:
        """Apply fade-in / fade-out (seconds) to a clip."""
        body = {
            "fade_in_time": fade_in_time,
            "fade_out_time": fade_out_time,
            "title": title,
        }
        return await self._post_json(f"/api/edit/fade/{clip_id}", body)

    async def adjust_speed(
        self,
        clip_id: str,
        speed_multiplier: float,
        keep_pitch: bool = True,
        title: str = "",
    ) -> dict[str, Any]:
        """Change playback speed (1.0 = original). keep_pitch preserves pitch."""
        body = {
            "clip_id": clip_id,
            "speed_multiplier": speed_multiplier,
            "keep_pitch": keep_pitch,
            "title": title,
        }
        return await self._post_json("/api/clips/adjust-speed/", body)

    # ------------------------------------------------------------------ #
    # Downloads                                                          #
    # ------------------------------------------------------------------ #

    async def get_download_url(self, clip_id: str) -> str:
        """Resolve the official download URL for a clip's audio."""
        data = await self._get_json(f"/api/download/clip/{clip_id}")
        url = data.get("download_url")
        if not url:
            raise RuntimeError(f"No download_url returned for clip {clip_id}: {data}")
        return url

    async def download_clip(self, clip_id: str, output_path: str) -> dict[str, Any]:
        """Download a clip's audio to a local file."""
        assert self._client is not None
        url = await self.get_download_url(clip_id)
        resp = await self._client.get(url, timeout=120.0)
        resp.raise_for_status()
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return {
            "clip_id": clip_id,
            "path": str(path.resolve()),
            "bytes": len(resp.content),
            "download_url": url,
        }

    async def download_lyrics(
        self, clip_id: str, output_path: str, fmt: str = "txt"
    ) -> dict[str, Any]:
        """Save a clip's lyrics to a file. fmt='txt' (plain) or 'lrc' (timed)."""
        info = await self.get_song_lyrics(clip_id)
        if fmt == "lrc":
            aligned = await self.get_aligned_lyrics(clip_id)
            content = _build_lrc(aligned, info.get("title"))
        else:
            content = info.get("lyrics") or ""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "clip_id": clip_id,
            "path": str(path.resolve()),
            "format": fmt,
            "title": info.get("title"),
            "chars": len(content),
        }

    # ------------------------------------------------------------------ #
    # Clip management (destructive)                                      #
    # ------------------------------------------------------------------ #

    async def trash_clip(
        self, clip_ids: list[str], trash: bool = True
    ) -> dict[str, Any]:
        """Move clips to trash (trash=True) or restore them (trash=False)."""
        return await self._post_json(
            "/api/gen/trash", {"trash": trash, "clip_ids": clip_ids}
        )

    async def delete_clip(self, ids: list[str], reason: str = "") -> dict[str, Any]:
        """Permanently delete clips. This cannot be undone."""
        return await self._post_json("/api/clips/delete/", {"ids": ids, "reason": reason})

    async def get_songs(self, song_ids: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if song_ids:
            params["ids"] = ",".join(song_ids)
        resp = await self._api("GET", "/api/feed/v2", params=params)
        resp.raise_for_status()
        return self._normalize_clips(resp.json().get("clips", []))

    async def get_clip(self, clip_id: str) -> dict[str, Any]:
        resp = await self._api("GET", f"/api/clip/{clip_id}")
        resp.raise_for_status()
        return resp.json()

    async def generate_lyrics(self, prompt: str, poll_interval: float = 2.0,
                              max_wait: float = 60.0) -> dict[str, Any]:
        resp = await self._api(
            "POST", "/api/generate/lyrics/", json_body={"prompt": prompt}
        )
        resp.raise_for_status()
        lyric_id = resp.json()["id"]
        waited = 0.0
        while waited < max_wait:
            poll = await self._api("GET", f"/api/generate/lyrics/{lyric_id}")
            if poll.status_code == 200:
                data = poll.json()
                if data.get("status") == "complete":
                    return data
            await asyncio.sleep(poll_interval)
            waited += poll_interval
        raise RuntimeError(f"Lyrics generation timed out after {max_wait}s")

    async def generate_lyrics_pair(
        self, prompt: str, poll_interval: float = 2.0, max_wait: float = 60.0
    ) -> dict[str, Any]:
        """
        The Create page "Generate lyrics" button: returns TWO lyric options to
        pick from. Polls both until complete and returns the finished text.
        """
        resp = await self._api(
            "POST",
            "/api/generate/lyrics-pair",
            json_body={"prompt": prompt, "create_session_token": str(uuid.uuid4())},
        )
        resp.raise_for_status()
        data = resp.json()
        ids = [i for i in (data.get("lyrics_a_id"), data.get("lyrics_b_id")) if i]

        async def _poll(lid: str) -> dict[str, Any]:
            waited = 0.0
            last: dict[str, Any] = {}
            while waited < max_wait:
                p = await self._api("GET", f"/api/generate/lyrics/{lid}")
                if p.status_code == 200:
                    last = p.json()
                    if last.get("status") == "complete":
                        return last
                await asyncio.sleep(poll_interval)
                waited += poll_interval
            return last

        options = []
        for lid in ids:
            d = await _poll(lid)
            options.append(
                {
                    "id": lid,
                    "title": d.get("title"),
                    "text": d.get("text"),
                    "tags": d.get("tags"),
                    "status": d.get("status"),
                }
            )
        return {"request_id": data.get("lyrics_request_id"), "options": options}

    async def generate(
        self,
        prompt: str,
        *,
        custom_mode: bool = False,
        tags: str | None = None,
        title: str | None = None,
        make_instrumental: bool = False,
        model: str | None = None,
        negative_tags: str | None = None,
        captcha_token: str | None = None,
        task: str | None = None,
        continue_clip_id: str | None = None,
        continue_at: float | None = None,
        persona_id: str | None = None,
        artist_clip_id: str | None = None,
        artist_start_s: float | None = None,
        artist_end_s: float | None = None,
        cover_clip_id: str | None = None,
        cover_start_s: float | None = None,
        cover_end_s: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Send the generation request to Suno.
        Raises SunoCaptchaRequired if Suno demands a captcha and we don't have one.

        Advanced "Personalize" options (match the web Create page):
          - persona_id: apply a saved persona (voice/style).
          - artist_clip_id (+ artist_start_s/end_s): personalize from a section
            of an existing song ("Add Voice").
          - cover_clip_id (+ cover_start_s/end_s): cover an existing song.
        """
        if captcha_token is None and await self.captcha_required():
            raise SunoCaptchaRequired(
                "Suno requires a captcha for this generation. Use the browser "
                "client (CloakBrowser) to complete it."
            )

        payload: dict[str, Any] = {
            "make_instrumental": make_instrumental,
            "mv": model or DEFAULT_MODEL,
            "prompt": "",
            "generation_type": "TEXT",
            "continue_at": continue_at,
            "continue_clip_id": continue_clip_id,
            "task": task,
            "token": captcha_token,
        }
        if custom_mode:
            payload["tags"] = tags or ""
            payload["title"] = title or ""
            payload["negative_tags"] = negative_tags or ""
            payload["prompt"] = prompt
        else:
            payload["gpt_description_prompt"] = prompt

        if persona_id:
            payload["persona_id"] = persona_id
        if artist_clip_id:
            payload["artist_clip_id"] = artist_clip_id
            payload["artist_start_s"] = artist_start_s
            payload["artist_end_s"] = artist_end_s
        if cover_clip_id:
            payload["cover_clip_id"] = cover_clip_id
            payload["cover_start_s"] = cover_start_s
            payload["cover_end_s"] = cover_end_s

        resp = await self._api(
            "POST", "/api/generate/v2/", json_body=payload, timeout=15.0
        )
        if resp.status_code == 402 or "captcha" in resp.text.lower():
            raise SunoCaptchaRequired(
                f"Suno rejected generation (likely captcha): {resp.status_code} {resp.text[:200]}"
            )
        resp.raise_for_status()
        return self._normalize_clips(resp.json().get("clips", []))

    async def extend_song(
        self,
        clip_id: str,
        prompt: str = "",
        continue_at: float = 0,
        tags: str = "",
        title: str = "",
        negative_tags: str = "",
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.generate(
            prompt=prompt,
            custom_mode=True,
            tags=tags,
            title=title,
            negative_tags=negative_tags,
            model=model,
            task="extend",
            continue_clip_id=clip_id,
            continue_at=continue_at,
        )

    async def concatenate(self, clip_id: str) -> dict[str, Any]:
        resp = await self._api(
            "POST",
            "/api/generate/concat/v2/",
            json_body={"clip_id": clip_id},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def wait_for_completion(
        self,
        song_ids: list[str],
        max_wait: float = 180.0,
        poll_interval: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Poll until every clip is `streaming`, `complete`, or `error`."""
        waited = 0.0
        last: list[dict[str, Any]] = []
        while waited < max_wait:
            last = await self.get_songs(song_ids)
            done_states = {"streaming", "complete", "error"}
            if last and all(c.get("status") in done_states for c in last):
                return last
            await asyncio.sleep(poll_interval)
            waited += poll_interval
        return last

    @staticmethod
    def _normalize_clips(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for c in clips:
            md = c.get("metadata") or {}
            out.append({
                "id": c.get("id"),
                "title": c.get("title"),
                "status": c.get("status"),
                "audio_url": c.get("audio_url"),
                "video_url": c.get("video_url"),
                "image_url": c.get("image_url"),
                "model_name": c.get("model_name"),
                "created_at": c.get("created_at"),
                "prompt": md.get("prompt"),
                "gpt_description_prompt": md.get("gpt_description_prompt"),
                "tags": md.get("tags"),
                "negative_tags": md.get("negative_tags"),
                "duration": md.get("duration"),
                "type": md.get("type"),
                "error_message": md.get("error_message"),
            })
        return out
