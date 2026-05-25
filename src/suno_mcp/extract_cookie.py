"""
Pull suno.com cookies from an already-running browser profile and either
print them or write them straight into the project's `.env` file.

Usage:
    python -m suno_mcp.extract_cookie              # print to stdout
    python -m suno_mcp.extract_cookie --write      # write SUNO_COOKIE into .env
    python -m suno_mcp.extract_cookie --browser chrome --profile "Profile 1"

Tries Chrome, Edge, then Brave by default. Works while the browser is open
(browser_cookie3 copies the SQLite store to a temp file before reading).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

REQUIRED_COOKIE = "__client"
SUNO_DOMAINS = ("suno.com", ".suno.com", "studio-api.prod.suno.com", "auth.suno.com")
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_CDP_PORT = 9222


def _loaders():
    """Return the (name, loader) pairs we'll try in order."""
    import browser_cookie3 as bc3  # imported lazily so import errors surface nicely

    return [
        ("chrome", bc3.chrome),
        ("edge", bc3.edge),
        ("brave", bc3.brave),
        ("chromium", bc3.chromium),
        ("opera", bc3.opera),
        ("firefox", bc3.firefox),
    ]


def _extract_one(
    loader: Callable, *, profile: str | None
) -> dict[str, str]:
    """Run one browser_cookie3 loader and return suno.com cookies as a dict."""
    kwargs: dict = {"domain_name": "suno.com"}
    if profile:
        kwargs["profile"] = profile
    jar = loader(**kwargs)
    out: dict[str, str] = {}
    for c in jar:
        if any(d in c.domain for d in SUNO_DOMAINS):
            out[c.name] = c.value
    return out


def extract(
    browsers: Iterable[str] | None = None, profile: str | None = None
) -> tuple[str, dict[str, str]]:
    """
    Try each browser in turn; return (browser_name, cookies_dict) for the
    first one that yields a `__client` cookie. Raise RuntimeError if none do.
    """
    loaders = _loaders()
    if browsers:
        wanted = {b.lower() for b in browsers}
        loaders = [(n, fn) for n, fn in loaders if n in wanted]

    errors: list[str] = []
    for name, fn in loaders:
        try:
            cookies = _extract_one(fn, profile=profile)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"  - {name}: {exc}")
            continue
        if REQUIRED_COOKIE in cookies:
            return name, cookies
        elif cookies:
            errors.append(
                f"  - {name}: found {len(cookies)} suno cookies but no `__client` "
                "(you may not be logged in on this browser)"
            )
    detail = "\n".join(errors) if errors else "  (no browsers checked)"
    raise RuntimeError(
        "Could not find a valid Suno session cookie in any browser.\n"
        f"Tried:\n{detail}\n"
        "Make sure you're logged in to https://suno.com in your browser."
    )


def build_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_chrome_exe() -> str | None:
    """Locate chrome.exe on Windows. Returns None if not found."""
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return shutil.which("chrome.exe") or shutil.which("chrome")


def _default_chrome_profile_dir() -> str:
    return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")


async def _extract_via_cdp(port: int, profile: str | None) -> dict[str, str]:
    """Connect to a Chrome running with --remote-debugging-port and read suno.com cookies."""
    from playwright.async_api import async_playwright  # type: ignore

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        try:
            # Pick the context for the right profile if there are multiple
            contexts = browser.contexts
            if not contexts:
                raise RuntimeError(
                    "Chrome is reachable on CDP but has no browser contexts. "
                    "Open at least one tab in that Chrome window."
                )
            ctx = contexts[0]
            if profile and len(contexts) > 1:
                # Heuristic: contexts are returned in profile order; try to match
                for c in contexts:
                    pages = c.pages
                    if pages and profile.lower() in (await pages[0].title()).lower():
                        ctx = c
                        break

            cookies = await ctx.cookies(["https://suno.com", "https://studio-api.prod.suno.com",
                                          "https://auth.suno.com"])
            jar: dict[str, str] = {}
            for c in cookies:
                if any(d in c.get("domain", "") for d in SUNO_DOMAINS):
                    jar[c["name"]] = c["value"]
            return jar
        finally:
            await browser.close()


def extract_from_cookie_editor_json(
    path: str | Path, password: str | None = None
) -> dict[str, str]:
    """
    Parse a JSON export from the Cookie-Editor browser extension
    (hotcleaner.com fork). Supports both plain exports (a JSON array of
    cookie objects) and the encrypted v2 export format
    ({url, version: 2, data: <base64>}).

    Encrypted v2 spec (reverse-engineered from extension source):
        - AES-GCM, 128-bit tag
        - IV = first 12 bytes of base64-decoded data
        - Ciphertext+tag = remaining bytes
        - Key = PBKDF2-HMAC-SHA256(password, salt=password*2,
                                    iterations=2**10=1024, keylen=32)
    """
    import json
    raw = Path(path).read_text(encoding="utf-8")
    obj = json.loads(raw)

    if isinstance(obj, list):
        cookie_list = obj
    elif isinstance(obj, dict) and obj.get("version") == 2 and "data" in obj:
        if not password:
            raise RuntimeError(
                "This is an encrypted Cookie-Editor v2 export. "
                "Pass the password with --password."
            )
        import base64
        import hashlib
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

        blob = base64.b64decode(obj["data"])
        salt = (password + password).encode("utf-8")
        key = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, 1024, dklen=32
        )
        plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
        cookie_list = json.loads(plain.decode("utf-8"))
    else:
        raise RuntimeError(
            "Unrecognized JSON shape — expected either a list of cookie "
            "objects or a Cookie-Editor encrypted v2 export."
        )

    jar: dict[str, str] = {}
    for c in cookie_list:
        domain = c.get("domain", "")
        if not any(d in domain for d in SUNO_DOMAINS):
            continue
        jar[c["name"]] = c["value"]
    return jar


async def _fresh_login_extract(headless: bool, timeout: float) -> dict[str, str]:
    """
    Launch a controlled Chromium window, navigate to suno.com, wait for the
    user to log in, then read suno.com cookies from the context.
    Detection: keeps polling cookies until `__client` is present.
    """
    from playwright.async_api import async_playwright  # type: ignore

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="",  # ephemeral
            headless=headless,
        )
        try:
            page = await context.new_page()
            await page.goto("https://suno.com", wait_until="domcontentloaded",
                            timeout=60_000)

            print("A Chromium window is now open. Log in to Suno.")
            print("Waiting for the __client session cookie to appear "
                  f"(timeout: {int(timeout)}s)...")

            import time
            deadline = time.time() + timeout
            while time.time() < deadline:
                cookies = await context.cookies([
                    "https://suno.com",
                    "https://studio-api.prod.suno.com",
                    "https://auth.suno.com",
                ])
                jar = {
                    c["name"]: c["value"]
                    for c in cookies
                    if any(d in c.get("domain", "") for d in SUNO_DOMAINS)
                }
                if "__client" in jar:
                    return jar
                await asyncio.sleep(2)
            raise RuntimeError(
                "Timed out waiting for login. Re-run --fresh-login to retry."
            )
        finally:
            await context.close()


def extract_via_fresh_login(headless: bool = False, timeout: float = 300.0) -> dict[str, str]:
    return asyncio.run(_fresh_login_extract(headless, timeout))


def extract_via_cdp(
    port: int = DEFAULT_CDP_PORT,
    profile: str | None = None,
    auto_launch: bool = False,
    profile_dir: str | None = None,
) -> dict[str, str]:
    """
    Read suno.com cookies from a Chrome running with --remote-debugging-port=<port>.
    If auto_launch=True and the port is closed, attempts to launch chrome.exe with
    the flag pointing at the user's profile dir (only works if no other Chrome
    instance is already using that profile, due to Chrome's profile lock).
    """
    if not _port_open("127.0.0.1", port):
        if not auto_launch:
            raise RuntimeError(
                f"No Chrome listening on 127.0.0.1:{port}.\n"
                "Either launch Chrome yourself with the debug flag (close ALL Chrome\n"
                "windows first), e.g.:\n\n"
                f'  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `\n'
                f'      --remote-debugging-port={port} `\n'
                f'      --user-data-dir="{_default_chrome_profile_dir()}"\n\n'
                "...or re-run with --auto-launch to try to do this for you."
            )
        chrome = _find_chrome_exe()
        if not chrome:
            raise RuntimeError(
                "Could not locate chrome.exe. Set CHROME_PATH env var to its full path."
            )
        udd = profile_dir or _default_chrome_profile_dir()
        cmd = [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={udd}"]
        print(f"Launching: {' '.join(cmd)}")
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        # Wait for the port to open (up to ~15s)
        deadline = time.time() + 15
        while time.time() < deadline:
            if _port_open("127.0.0.1", port):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"Launched Chrome but port {port} never opened. Another Chrome "
                "instance is probably already using that profile — close all Chrome "
                "windows and try again."
            )

    return asyncio.run(_extract_via_cdp(port, profile))


def write_to_env(cookie_header: str, env_path: Path = ENV_PATH) -> None:
    """Set or replace SUNO_COOKIE in the project's .env file."""
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith("SUNO_COOKIE="):
            lines[i] = f"SUNO_COOKIE={cookie_header}"
            found = True
            break
    if not found:
        lines.append(f"SUNO_COOKIE={cookie_header}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract suno.com cookies from a running browser profile."
    )
    parser.add_argument(
        "--from-json",
        help="Path to a JSON export from the Cookie-Editor browser extension. "
        "Supports plain exports and encrypted v2 (use --password).",
    )
    parser.add_argument(
        "--password",
        help="Password for an encrypted Cookie-Editor v2 export.",
    )
    parser.add_argument(
        "--fresh-login",
        action="store_true",
        help="Launch a controlled Chromium window, wait for you to log in to "
        "Suno, then save the cookies. The recommended automated path on "
        "Chrome 127+ where disk-decryption and CDP attach are blocked.",
    )
    parser.add_argument(
        "--fresh-login-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for login in --fresh-login mode (default 300).",
    )
    parser.add_argument(
        "--cdp",
        action="store_true",
        help="Use Chrome DevTools Protocol on a Chrome launched with "
        "--remote-debugging-port. Blocked by Chrome on signed-in default "
        "profiles — usually unhelpful on modern Chrome.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help=f"Port to connect to in --cdp mode (default {DEFAULT_CDP_PORT}).",
    )
    parser.add_argument(
        "--auto-launch",
        action="store_true",
        help="In --cdp mode, automatically launch Chrome with the debug flag if "
        "no instance is reachable. Requires no Chrome to currently be running "
        "(profile lock).",
    )
    parser.add_argument(
        "--profile-dir",
        help='In --cdp --auto-launch, the Chrome --user-data-dir path. '
        'Defaults to your standard Chrome User Data folder.',
    )
    parser.add_argument(
        "--browser",
        action="append",
        help="Disk-mode browsers to try (chrome, edge, brave, chromium, opera, firefox). "
        "Repeat the flag to try multiple. Default: all.",
    )
    parser.add_argument(
        "--profile",
        help='Profile name, e.g. "Default" or "Profile 1". '
        "Only used if your browser has multiple profiles.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"Write SUNO_COOKIE into {ENV_PATH} (creates the file if missing).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print the full cookie value (default truncates for safety).",
    )
    args = parser.parse_args(argv)

    if args.from_json:
        try:
            cookies = extract_from_cookie_editor_json(args.from_json, args.password)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"Failed to parse/decrypt {args.from_json}: {e}", file=sys.stderr)
            return 1
        if REQUIRED_COOKIE not in cookies:
            print(
                "Decrypted successfully but no __client cookie present for suno.com. "
                "Re-export from Cookie-Editor while logged in to suno.com.",
                file=sys.stderr,
            )
            return 1
        browser_name = f"cookie-editor json ({args.from_json})"
    elif args.fresh_login:
        try:
            cookies = extract_via_fresh_login(
                headless=False, timeout=args.fresh_login_timeout
            )
        except ImportError as e:
            print(f"playwright is not installed. Run: pip install playwright && "
                  f"python -m playwright install chromium\n({e})", file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        browser_name = "fresh-login (Playwright)"
    elif args.cdp:
        try:
            cookies = extract_via_cdp(
                port=args.cdp_port,
                profile=args.profile,
                auto_launch=args.auto_launch,
                profile_dir=args.profile_dir,
            )
        except ImportError as e:
            print(f"playwright is not installed. Run: pip install playwright\n({e})",
                  file=sys.stderr)
            return 2
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 1
        if REQUIRED_COOKIE not in cookies:
            print(
                "Connected to Chrome but no __client cookie found for suno.com. "
                "Open https://suno.com in that Chrome and log in, then retry.",
                file=sys.stderr,
            )
            return 1
        browser_name = f"chrome (CDP :{args.cdp_port})"
    else:
        try:
            browser_name, cookies = extract(args.browser, profile=args.profile)
        except ImportError as e:
            print(
                f"browser-cookie3 is not installed. Run: pip install browser-cookie3\n({e})",
                file=sys.stderr,
            )
            return 2
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            print(
                "\nIf Chrome 127+ is blocking due to app-bound encryption, try:\n"
                "  python -m suno_mcp.extract_cookie --cdp --auto-launch --write\n"
                "(close all Chrome windows first).",
                file=sys.stderr,
            )
            return 1

    header = build_cookie_header(cookies)
    print(f"Found Suno session in: {browser_name}")
    print(f"Cookies captured: {len(cookies)} ({', '.join(sorted(cookies))})")

    if args.write:
        write_to_env(header)
        print(f"Wrote SUNO_COOKIE to {ENV_PATH}")
    else:
        if args.show:
            print()
            print(f"SUNO_COOKIE={header}")
        else:
            preview = header[:60] + "..." + header[-20:] if len(header) > 100 else header
            print()
            print(f"SUNO_COOKIE={preview}")
            print("(use --show to print the full value, or --write to save into .env)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
