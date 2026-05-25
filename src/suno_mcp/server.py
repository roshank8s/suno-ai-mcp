"""FastMCP server exposing Suno tools."""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .browser_client import BrowserGenerator
from .suno_client import (
    DEFAULT_MODEL,
    SunoAuthError,
    SunoCaptchaRequired,
    SunoClient,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("SUNO_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("suno_mcp")

mcp = FastMCP("suno-ai")


def _cookie() -> str:
    c = os.environ.get("SUNO_COOKIE", "").strip()
    if not c:
        raise SunoAuthError(
            "SUNO_COOKIE is not set. Add it to your .env file or the env block "
            "of your Claude Code MCP config."
        )
    return c


def _gen_mode() -> str:
    return os.environ.get("SUNO_GENERATION_MODE", "auto").lower()


def _headless() -> bool:
    return os.environ.get("SUNO_BROWSER_HEADLESS", "false").lower() in {"1", "true", "yes"}


def _default_model() -> str:
    return os.environ.get("SUNO_DEFAULT_MODEL", DEFAULT_MODEL)


async def _do_generate(
    prompt: str,
    *,
    custom_mode: bool,
    tags: str | None,
    title: str | None,
    make_instrumental: bool,
    model: str | None,
    negative_tags: str | None,
    wait_for_audio: bool,
) -> dict[str, Any]:
    """Shared generate path used by both simple + custom tools."""
    mode = _gen_mode()
    chosen_model = model or _default_model()
    api_error: str | None = None
    clips: list[dict[str, Any]] = []

    if mode in {"auto", "api"}:
        try:
            async with SunoClient(_cookie()) as client:
                clips = await client.generate(
                    prompt,
                    custom_mode=custom_mode,
                    tags=tags,
                    title=title,
                    make_instrumental=make_instrumental,
                    model=chosen_model,
                    negative_tags=negative_tags,
                )
                if wait_for_audio and clips:
                    ids = [c["id"] for c in clips if c.get("id")]
                    clips = await client.wait_for_completion(ids)
                return {"source": "api", "clips": clips}
        except SunoCaptchaRequired as exc:
            api_error = str(exc)
            if mode == "api":
                raise
            logger.info("Falling back to browser: %s", exc)
        except SunoAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            api_error = f"API path failed: {exc}"
            if mode == "api":
                raise
            logger.warning(api_error)

    # Browser fallback (mode == "browser" or "auto" after captcha/error)
    cookies = SunoClient(_cookie()).cookies  # parse only, no init
    browser = BrowserGenerator(cookies, headless=_headless())
    clips = await browser.generate(
        prompt,
        custom_mode=custom_mode,
        tags=tags,
        title=title,
        make_instrumental=make_instrumental,
    )
    if wait_for_audio and clips:
        async with SunoClient(_cookie()) as client:
            ids = [c["id"] for c in clips if c.get("id")]
            clips = await client.wait_for_completion(ids)
    return {"source": "browser", "api_error": api_error, "clips": clips}


@mcp.tool()
async def generate_music(
    prompt: Annotated[
        str,
        Field(description="Description of the song to generate (e.g. 'upbeat pop song about coding at midnight')."),
    ],
    make_instrumental: Annotated[bool, Field(description="Generate without vocals.")] = False,
    model: Annotated[
        str | None,
        Field(description="Model name: chirp-v3-5, chirp-v4, chirp-v4-5, chirp-v5. Defaults to SUNO_DEFAULT_MODEL."),
    ] = None,
    wait_for_audio: Annotated[
        bool,
        Field(description="If true, poll until each clip is streaming/complete/error before returning."),
    ] = False,
) -> dict[str, Any]:
    """Generate a song from a free-form description (Simple mode)."""
    return await _do_generate(
        prompt,
        custom_mode=False,
        tags=None,
        title=None,
        make_instrumental=make_instrumental,
        model=model,
        negative_tags=None,
        wait_for_audio=wait_for_audio,
    )


@mcp.tool()
async def custom_generate(
    lyrics: Annotated[str, Field(description="Lyrics for the song. Can be empty if instrumental.")],
    tags: Annotated[str, Field(description="Style tags, e.g. 'lofi hiphop, mellow, jazz piano'.")],
    title: Annotated[str, Field(description="Title of the song.")] = "",
    make_instrumental: Annotated[bool, Field(description="Generate without vocals.")] = False,
    model: Annotated[str | None, Field(description="Model name.")] = None,
    negative_tags: Annotated[str, Field(description="Comma-separated styles to avoid.")] = "",
    wait_for_audio: Annotated[bool, Field(description="Poll until clips finish.")] = False,
) -> dict[str, Any]:
    """Generate a song in Custom mode with explicit lyrics, tags, and title."""
    return await _do_generate(
        lyrics,
        custom_mode=True,
        tags=tags,
        title=title,
        make_instrumental=make_instrumental,
        model=model,
        negative_tags=negative_tags or None,
        wait_for_audio=wait_for_audio,
    )


@mcp.tool()
async def get_credits() -> dict[str, Any]:
    """Return your Suno credit balance and monthly usage."""
    async with SunoClient(_cookie()) as client:
        return await client.get_credits()


@mcp.tool()
async def list_songs(
    song_ids: Annotated[
        list[str] | None,
        Field(description="Optional list of clip IDs to fetch. If omitted, returns your recent feed."),
    ] = None,
) -> list[dict[str, Any]]:
    """List your songs / poll status for specific clip IDs."""
    async with SunoClient(_cookie()) as client:
        return await client.get_songs(song_ids)


@mcp.tool()
async def get_clip(
    clip_id: Annotated[str, Field(description="The clip ID to fetch full metadata for.")],
) -> dict[str, Any]:
    """Get detailed information about a single clip."""
    async with SunoClient(_cookie()) as client:
        return await client.get_clip(clip_id)


@mcp.tool()
async def generate_lyrics(
    prompt: Annotated[str, Field(description="Theme or description for lyric generation.")],
) -> dict[str, Any]:
    """Generate lyrics from a theme. Returns the completed lyric text."""
    async with SunoClient(_cookie()) as client:
        return await client.generate_lyrics(prompt)


@mcp.tool()
async def extend_song(
    clip_id: Annotated[str, Field(description="ID of the existing clip to extend.")],
    prompt: Annotated[str, Field(description="Lyrics or description for the extension.")] = "",
    continue_at: Annotated[
        float,
        Field(description="Seconds into the source clip to start extending from. 0 = from the end."),
    ] = 0,
    tags: Annotated[str, Field(description="Style tags for the extension.")] = "",
    title: Annotated[str, Field(description="Title for the new clip.")] = "",
    negative_tags: Annotated[str, Field(description="Styles to avoid.")] = "",
    model: Annotated[str | None, Field(description="Model name.")] = None,
) -> list[dict[str, Any]]:
    """Extend an existing song from a given timestamp."""
    async with SunoClient(_cookie()) as client:
        return await client.extend_song(
            clip_id=clip_id,
            prompt=prompt,
            continue_at=continue_at,
            tags=tags,
            title=title,
            negative_tags=negative_tags,
            model=model,
        )


@mcp.tool()
async def concatenate_song(
    clip_id: Annotated[str, Field(description="ID of an extended clip to stitch into a full song.")],
) -> dict[str, Any]:
    """Concatenate an extended clip into a single full-length song."""
    async with SunoClient(_cookie()) as client:
        return await client.concatenate(clip_id)


def main() -> None:
    """Entry point used by `suno-mcp` script and `python -m suno_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
