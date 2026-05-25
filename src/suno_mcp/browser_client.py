"""
Browser-driven generation path. Uses CloakBrowser (stealth Chromium) to drive
suno.com/create directly. This is the fallback when the cookie/JWT API path
hits a captcha wall.

Strategy:
  1. Build a Playwright context with the user's suno.com cookies pre-loaded.
  2. Navigate to https://suno.com/create.
  3. Listen for POST /api/generate/v2/ responses (Suno's internal generation
     endpoint) so we can read back the resulting clip IDs.
  4. Fill the prompt textarea, click Create, and wait for the response.
  5. If hCaptcha appears, the headed window lets the user solve it manually
     (CloakBrowser's anti-fingerprinting often avoids triggering it at all).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

SUNO_CREATE_URL = "https://suno.com/create"
GENERATE_API_PATH = "/api/generate/v2/"


def _cookies_to_storage_state(cookie_jar: dict[str, str]) -> dict[str, Any]:
    """Convert our cookie dict into Playwright storage_state format."""
    return {
        "cookies": [
            {
                "name": k,
                "value": v,
                "domain": ".suno.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
            for k, v in cookie_jar.items()
        ],
        "origins": [],
    }


class BrowserGenerator:
    """One-shot browser session for a single generation call."""

    def __init__(self, cookie_jar: dict[str, str], headless: bool = False) -> None:
        self._cookies = cookie_jar
        self._headless = headless

    async def generate(
        self,
        prompt: str,
        *,
        custom_mode: bool = False,
        tags: str | None = None,
        title: str | None = None,
        make_instrumental: bool = False,
        timeout: float = 180.0,
    ) -> list[dict[str, Any]]:
        # Import lazily so the API-only path doesn't require cloakbrowser
        try:
            from cloakbrowser import launch_context_async  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "cloakbrowser is not installed. Install with: pip install cloakbrowser\n"
                "Or set SUNO_GENERATION_MODE=api to disable the browser fallback."
            ) from e

        storage_state = _cookies_to_storage_state(self._cookies)
        context = await launch_context_async(
            storage_state=storage_state,
            headless=self._headless,
            humanize=True,
        )
        try:
            page = await context.new_page()

            captured: list[dict[str, Any]] = []
            response_event = asyncio.Event()

            async def on_response(resp: Any) -> None:
                try:
                    if GENERATE_API_PATH in resp.url and resp.request.method == "POST":
                        if resp.status == 200:
                            data = await resp.json()
                            clips = data.get("clips", []) or []
                            if clips:
                                captured.extend(clips)
                                response_event.set()
                        else:
                            body = await resp.text()
                            logger.warning(
                                "generate/v2 returned %s: %s",
                                resp.status,
                                body[:300],
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("on_response handler error: %s", exc)

            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            logger.info("Navigating to %s", SUNO_CREATE_URL)
            await page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=60_000)

            # Wait for the create UI to settle
            await page.wait_for_load_state("networkidle", timeout=30_000)

            if custom_mode:
                await self._fill_custom_form(page, prompt, tags, title, make_instrumental)
            else:
                await self._fill_simple_form(page, prompt, make_instrumental)

            # Click the Create button
            create_btn = page.locator('button[aria-label="Create"]').first
            await create_btn.wait_for(state="visible", timeout=15_000)
            await create_btn.click()

            # Wait for generate/v2 response or timeout
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout)
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f"Timed out after {timeout}s waiting for Suno generation response. "
                    "If hCaptcha appeared, you may need to solve it manually in the "
                    "headed browser window (set SUNO_BROWSER_HEADLESS=false)."
                ) from e

            return _normalize_clips(captured)
        finally:
            await context.close()

    @staticmethod
    async def _fill_simple_form(page: Any, prompt: str, instrumental: bool) -> None:
        # Suno's "Simple" mode: single textarea labeled "Song Description"
        textarea = page.locator("textarea").first
        await textarea.wait_for(state="visible", timeout=15_000)
        await textarea.fill("")
        await textarea.type(prompt, delay=20)
        if instrumental:
            await _toggle_instrumental(page)

    @staticmethod
    async def _fill_custom_form(
        page: Any,
        lyrics: str,
        tags: str | None,
        title: str | None,
        instrumental: bool,
    ) -> None:
        # Switch to Custom mode
        try:
            custom_toggle = page.get_by_role("button", name="Custom").first
            if await custom_toggle.count() > 0:
                await custom_toggle.click()
        except Exception:  # noqa: BLE001
            logger.debug("Custom toggle not found — may already be in custom mode")

        # The form has 3 fields: Lyrics (large textarea), Style (input), Title (input)
        textareas = page.locator("textarea")
        inputs = page.locator('input[type="text"]')

        if await textareas.count() >= 1:
            await textareas.nth(0).fill(lyrics)
        if tags and await inputs.count() >= 1:
            await inputs.nth(0).fill(tags)
        if title and await inputs.count() >= 2:
            await inputs.nth(1).fill(title)
        if instrumental:
            await _toggle_instrumental(page)


async def _toggle_instrumental(page: Any) -> None:
    try:
        switch = page.get_by_role("switch", name="Instrumental").first
        if await switch.count() > 0:
            await switch.click()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not toggle instrumental: %s", exc)


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
