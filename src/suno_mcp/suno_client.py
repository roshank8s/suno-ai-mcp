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
            "period": data.get("period"),
            "monthly_limit": data.get("monthly_limit"),
            "monthly_usage": data.get("monthly_usage"),
        }

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
    ) -> list[dict[str, Any]]:
        """
        Send the generation request to Suno.
        Raises SunoCaptchaRequired if Suno demands a captcha and we don't have one.
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
