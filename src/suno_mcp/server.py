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
    persona_id: str | None = None,
    artist_clip_id: str | None = None,
    artist_start_s: float | None = None,
    artist_end_s: float | None = None,
    cover_clip_id: str | None = None,
    cover_start_s: float | None = None,
    cover_end_s: float | None = None,
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
                    persona_id=persona_id,
                    artist_clip_id=artist_clip_id,
                    artist_start_s=artist_start_s,
                    artist_end_s=artist_end_s,
                    cover_clip_id=cover_clip_id,
                    cover_start_s=cover_start_s,
                    cover_end_s=cover_end_s,
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
    negative_tags: Annotated[str, Field(description="Comma-separated styles to avoid (the 'Exclude styles' field).")] = "",
    wait_for_audio: Annotated[bool, Field(description="Poll until clips finish.")] = False,
    persona_id: Annotated[
        str | None,
        Field(description="Apply a saved persona (voice/style). See list_personas."),
    ] = None,
    artist_clip_id: Annotated[
        str | None,
        Field(description="Personalize from an existing song ('Add Voice'): the source clip ID."),
    ] = None,
    artist_start_s: Annotated[float | None, Field(description="Start sec of the artist section.")] = None,
    artist_end_s: Annotated[float | None, Field(description="End sec of the artist section.")] = None,
    cover_clip_id: Annotated[
        str | None, Field(description="Cover an existing song: the source clip ID.")
    ] = None,
    cover_start_s: Annotated[float | None, Field(description="Start sec of the cover section.")] = None,
    cover_end_s: Annotated[float | None, Field(description="End sec of the cover section.")] = None,
) -> dict[str, Any]:
    """Generate a song in Custom/Advanced mode with explicit lyrics, style tags,
    and title. Supports the Personalize options: persona_id (saved voice/style),
    artist_clip_id (personalize from a song), and cover_clip_id (cover a song)."""
    return await _do_generate(
        lyrics,
        custom_mode=True,
        tags=tags,
        title=title,
        make_instrumental=make_instrumental,
        model=model,
        negative_tags=negative_tags or None,
        wait_for_audio=wait_for_audio,
        persona_id=persona_id,
        artist_clip_id=artist_clip_id,
        artist_start_s=artist_start_s,
        artist_end_s=artist_end_s,
        cover_clip_id=cover_clip_id,
        cover_start_s=cover_start_s,
        cover_end_s=cover_end_s,
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
async def generate_lyrics_pair(
    prompt: Annotated[str, Field(description="Theme or description for the lyrics.")],
) -> dict[str, Any]:
    """The Advanced Create page's 'Generate lyrics' button: returns TWO lyric
    options (A/B) to choose from."""
    async with SunoClient(_cookie()) as client:
        return await client.generate_lyrics_pair(prompt)


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


# --------------------------------------------------------------------------- #
# Account / library / discovery (read-only)                                    #
# --------------------------------------------------------------------------- #


@mcp.tool()
async def get_user() -> dict[str, Any]:
    """Return the logged-in Suno user's profile (id, handle, display name)."""
    async with SunoClient(_cookie()) as client:
        return await client.get_user()


@mcp.tool()
async def list_playlists(
    page: Annotated[int, Field(description="Page number (1-based).")] = 1,
) -> dict[str, Any]:
    """List your own playlists."""
    async with SunoClient(_cookie()) as client:
        return await client.list_playlists(page)


@mcp.tool()
async def get_playlist(
    playlist_id: Annotated[str, Field(description="The playlist ID.")],
    page: Annotated[int, Field(description="Page of clips to return.")] = 1,
) -> dict[str, Any]:
    """Get a single playlist and its clips."""
    async with SunoClient(_cookie()) as client:
        return await client.get_playlist(playlist_id, page)


@mcp.tool()
async def list_projects() -> dict[str, Any]:
    """List your projects (workspaces)."""
    async with SunoClient(_cookie()) as client:
        return await client.list_projects()


@mcp.tool()
async def list_personas(
    page: Annotated[int, Field(description="Page number (1-based).")] = 1,
) -> dict[str, Any]:
    """List your saved voice/style personas."""
    async with SunoClient(_cookie()) as client:
        return await client.list_personas(page)


@mcp.tool()
async def search_songs(
    term: Annotated[str, Field(description="Search text.")],
    search_type: Annotated[
        str, Field(description="API search enum, e.g. 'public_song'.")
    ] = "public_song",
    size: Annotated[int, Field(description="Number of results.")] = 20,
    from_index: Annotated[int, Field(description="Result offset for paging.")] = 0,
) -> dict[str, Any]:
    """Search public Suno songs (and other content types via search_type)."""
    async with SunoClient(_cookie()) as client:
        return await client.search_songs(
            term, search_type=search_type, size=size, from_index=from_index
        )


@mcp.tool()
async def get_similar_songs(
    clip_id: Annotated[str, Field(description="Clip ID to find similar songs for.")],
) -> dict[str, Any]:
    """Return songs similar to a given clip."""
    async with SunoClient(_cookie()) as client:
        return await client.get_similar(clip_id)


@mcp.tool()
async def get_songs_by_ids(
    song_ids: Annotated[list[str], Field(description="Clip IDs to fetch.")],
) -> list[dict[str, Any]]:
    """Batch-fetch full metadata for multiple clip IDs."""
    async with SunoClient(_cookie()) as client:
        return await client.get_songs_by_ids(song_ids)


@mcp.tool()
async def get_recommend_styles() -> dict[str, Any]:
    """Suno's recommended style tags (useful for crafting prompts)."""
    async with SunoClient(_cookie()) as client:
        return await client.get_recommend_styles()


# --------------------------------------------------------------------------- #
# Lyrics tooling                                                               #
# --------------------------------------------------------------------------- #


@mcp.tool()
async def get_aligned_lyrics(
    clip_id: Annotated[str, Field(description="Clip ID of a finished song.")],
) -> dict[str, Any]:
    """Word-level lyric timing (and waveform) for a finished clip."""
    async with SunoClient(_cookie()) as client:
        return await client.get_aligned_lyrics(clip_id)


@mcp.tool()
async def get_song_lyrics(
    clip_id: Annotated[str, Field(description="Clip ID.")],
) -> dict[str, Any]:
    """Return a clip's lyric text, title, and style tags."""
    async with SunoClient(_cookie()) as client:
        return await client.get_song_lyrics(clip_id)


@mcp.tool()
async def cowrite_lyrics(
    instruction: Annotated[str, Field(description="What to do, e.g. 'make the chorus punchier'.")],
    selected: Annotated[str, Field(description="The lyric text to rewrite/extend.")] = "",
    context_before: Annotated[str, Field(description="Lyrics before the selection.")] = "",
    context_after: Annotated[str, Field(description="Lyrics after the selection.")] = "",
    title: Annotated[str, Field(description="Song title for context.")] = "",
    style: Annotated[str, Field(description="Style tags for context.")] = "",
) -> dict[str, Any]:
    """AI co-write: rewrite or continue selected lyrics given surrounding context."""
    async with SunoClient(_cookie()) as client:
        return await client.cowrite_lyrics(
            instruction=instruction,
            selected=selected,
            context_before=context_before,
            context_after=context_after,
            title=title,
            style=style,
        )


@mcp.tool()
async def lyrics_infill(
    prompt: Annotated[str, Field(description="Instruction for the gap to fill.")],
    context_lyrics_prefix: Annotated[str, Field(description="Lyrics before the gap.")] = "",
    context_lyrics_edit: Annotated[str, Field(description="Existing text in the gap to replace.")] = "",
    context_lyrics_suffix: Annotated[str, Field(description="Lyrics after the gap.")] = "",
    title: Annotated[str, Field(description="Song title for context.")] = "",
) -> dict[str, Any]:
    """Fill in a gap in lyrics given the surrounding prefix/suffix."""
    async with SunoClient(_cookie()) as client:
        return await client.lyrics_infill(
            prompt=prompt,
            context_lyrics_prefix=context_lyrics_prefix,
            context_lyrics_edit=context_lyrics_edit,
            context_lyrics_suffix=context_lyrics_suffix,
            title=title,
        )


# --------------------------------------------------------------------------- #
# Audio editing (creates new clips/jobs; may consume credits)                  #
# --------------------------------------------------------------------------- #


@mcp.tool()
async def separate_stems(
    clip_id: Annotated[str, Field(description="Clip ID to split into stems.")],
) -> dict[str, Any]:
    """Separate a clip into stems (e.g. vocals / instrumental)."""
    async with SunoClient(_cookie()) as client:
        return await client.separate_stems(clip_id)


@mcp.tool()
async def crop_clip(
    clip_id: Annotated[str, Field(description="Clip ID to crop.")],
    crop_start_s: Annotated[float, Field(description="Start of range in seconds.")],
    crop_end_s: Annotated[float, Field(description="End of range in seconds.")],
    is_crop_remove: Annotated[
        bool, Field(description="If true, remove the range instead of keeping it.")
    ] = False,
    title: Annotated[str, Field(description="Title for the new clip.")] = "",
) -> dict[str, Any]:
    """Crop a clip to (or remove) a [start, end] seconds range."""
    async with SunoClient(_cookie()) as client:
        return await client.crop_clip(
            clip_id, crop_start_s, crop_end_s, is_crop_remove=is_crop_remove, title=title
        )


@mcp.tool()
async def fade_clip(
    clip_id: Annotated[str, Field(description="Clip ID to edit.")],
    fade_in_time: Annotated[float, Field(description="Fade-in length in seconds.")] = 0.0,
    fade_out_time: Annotated[float, Field(description="Fade-out length in seconds.")] = 0.0,
    title: Annotated[str, Field(description="Title for the new clip.")] = "",
) -> dict[str, Any]:
    """Apply fade-in / fade-out to a clip."""
    async with SunoClient(_cookie()) as client:
        return await client.fade_clip(
            clip_id, fade_in_time=fade_in_time, fade_out_time=fade_out_time, title=title
        )


@mcp.tool()
async def adjust_speed(
    clip_id: Annotated[str, Field(description="Clip ID to edit.")],
    speed_multiplier: Annotated[float, Field(description="Speed factor (1.0 = original).")],
    keep_pitch: Annotated[bool, Field(description="Preserve original pitch.")] = True,
    title: Annotated[str, Field(description="Title for the new clip.")] = "",
) -> dict[str, Any]:
    """Change a clip's playback speed."""
    async with SunoClient(_cookie()) as client:
        return await client.adjust_speed(
            clip_id, speed_multiplier, keep_pitch=keep_pitch, title=title
        )


# --------------------------------------------------------------------------- #
# Downloads                                                                     #
# --------------------------------------------------------------------------- #


@mcp.tool()
async def download_clip(
    clip_id: Annotated[str, Field(description="Clip ID to download.")],
    output_path: Annotated[str, Field(description="Local file path to write the audio to (e.g. C:/tmp/song.mp3).")],
) -> dict[str, Any]:
    """Download a clip's audio file to disk. Returns the saved path and size."""
    async with SunoClient(_cookie()) as client:
        return await client.download_clip(clip_id, output_path)


@mcp.tool()
async def download_lyrics(
    clip_id: Annotated[str, Field(description="Clip ID.")],
    output_path: Annotated[str, Field(description="Local file path to write lyrics to.")],
    fmt: Annotated[str, Field(description="'txt' for plain lyrics or 'lrc' for timed lyrics.")] = "txt",
) -> dict[str, Any]:
    """Save a clip's lyrics to a file (plain text or timed .lrc)."""
    async with SunoClient(_cookie()) as client:
        return await client.download_lyrics(clip_id, output_path, fmt=fmt)


# --------------------------------------------------------------------------- #
# Clip management (destructive)                                                 #
# --------------------------------------------------------------------------- #


@mcp.tool()
async def trash_clip(
    clip_ids: Annotated[list[str], Field(description="Clip IDs to trash or restore.")],
    trash: Annotated[bool, Field(description="True to move to trash, False to restore.")] = True,
) -> dict[str, Any]:
    """Move clips to trash (reversible) or restore them."""
    async with SunoClient(_cookie()) as client:
        return await client.trash_clip(clip_ids, trash=trash)


@mcp.tool()
async def delete_clip(
    clip_ids: Annotated[list[str], Field(description="Clip IDs to permanently delete.")],
    reason: Annotated[str, Field(description="Optional reason string.")] = "",
) -> dict[str, Any]:
    """Permanently delete clips. THIS CANNOT BE UNDONE."""
    async with SunoClient(_cookie()) as client:
        return await client.delete_clip(clip_ids, reason=reason)


def main() -> None:
    """Entry point used by `suno-mcp` script and `python -m suno_mcp`."""
    mcp.run()


if __name__ == "__main__":
    main()
