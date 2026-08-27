"""Command line entry points.

amazon-nl-mcp serve     run the MCP service (what systemd starts)
amazon-nl-mcp login     one-off interactive sign-in that seeds the profile
amazon-nl-mcp doctor    check the profile, the browser, and the session
amazon-nl-mcp token     print a fresh bearer token to put in the env file
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import secrets
import sys

import uvicorn

from amazon_nl_mcp.amazon.client import AmazonClient
from amazon_nl_mcp.app import build_app
from amazon_nl_mcp.browser import BrowserSession
from amazon_nl_mcp.config import Settings, get_settings
from amazon_nl_mcp.logging import configure_logging, get_logger

log = get_logger(__name__)

LOGIN_BANNER = """
────────────────────────────────────────────────────────────────────────
A browser window is open on amazon.nl. In it:

  1. Sign in with your personal Amazon account.
  2. Tick "Blijf ingelogd" / "Keep me signed in" — that checkbox is what
     makes the session survive; without it you will be signing in weekly.
  3. Complete any 2FA / one-time code.
  4. Check the top-right nav reads "Hallo, <your name>".

Then come back here and press ENTER. Do not kill this process: Chromium
flushes its cookie store on a clean close, and a hard kill loses the login
you just did.
────────────────────────────────────────────────────────────────────────
"""

HEADLESS_HINT = """
No X display found, so the login window has nowhere to appear. On a headless
Ubuntu box, give it one and look at it over VNC:

    sudo apt-get install -y xvfb x11vnc
    Xvfb :99 -screen 0 1600x1000x24 &
    x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever &

    # from your laptop:
    ssh -N -L 5900:localhost:5900 <user>@<this-box>
    # then point a VNC client at localhost:5900

    DISPLAY=:99 amazon-nl-mcp login
"""


def _settings_or_exit(*, require_auth: bool = True) -> Settings:
    try:
        return get_settings() if require_auth else Settings(auth_disabled=True)
    except Exception as exc:  # configuration errors should read as advice, not a traceback
        print(f"Configuration error:\n  {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_serve(_: argparse.Namespace) -> int:
    """Run the ASGI service under uvicorn."""
    settings = _settings_or_exit()
    configure_logging(settings.log_level, settings.log_format)
    if settings.auth_disabled and settings.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"Refusing to serve unauthenticated on {settings.host}. "
            "Set AMAZON_MCP_AUTH_TOKEN, or bind 127.0.0.1.",
            file=sys.stderr,
        )
        return 2
    app = build_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=20,
    )
    return 0


async def _login(settings: Settings, *, timeout_s: float) -> int:
    session = BrowserSession(settings, headless=False)
    client = AmazonClient(session, settings)
    try:
        await session.start()
    except Exception as exc:
        message = str(exc)
        print(f"Could not open a browser window: {message}", file=sys.stderr)
        if "XServer" in message or "DISPLAY" in message:
            print(HEADLESS_HINT, file=sys.stderr)
        return 1

    try:
        async with session.page(rate_limited=False) as page:
            await page.goto(settings.base_url, wait_until="domcontentloaded")
            print(LOGIN_BANNER, flush=True)
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(loop.run_in_executor(None, sys.stdin.readline), timeout=timeout_s)
            except TimeoutError:
                print(f"Timed out after {timeout_s / 60:.0f} minutes without confirmation.", file=sys.stderr)
                return 1
            signed_in = await client.is_signed_in(page)
    finally:
        # Close, never kill: this is what flushes the cookie database.
        await session.stop()

    if signed_in:
        print(f"Signed in. The profile at {settings.profile_dir} now holds the session.")
        print("Start the service with: systemctl --user restart amazon-nl-mcp")
        return 0
    print(
        "The nav still reads signed out. Nothing was saved — run `amazon-nl-mcp login` again.",
        file=sys.stderr,
    )
    return 1


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings_or_exit(require_auth=False)
    configure_logging(settings.log_level, settings.log_format)
    return asyncio.run(_login(settings, timeout_s=args.timeout))


async def _doctor(settings: Settings) -> int:
    session = BrowserSession(settings)
    client = AmazonClient(session, settings)
    print(f"profile dir     : {settings.profile_dir}")
    print(f"profile exists  : {settings.profile_dir.exists()}")
    print(f"storefront      : {settings.base_url}")
    print(f"bind            : {settings.host}:{settings.port}{settings.mcp_path}")
    print(f"auth            : {'disabled' if settings.auth_disabled else 'bearer token'}")
    print(f"cart writes     : {'enabled' if settings.write_enabled else 'disabled'}")
    try:
        status = await client.session_status()
    finally:
        with contextlib.suppress(Exception):
            await session.stop()
    print(f"session state   : {status.state}")
    print(f"cart items      : {status.cart_item_count}")
    print(f"next step       : {status.detail}")
    return 0 if status.state == "authenticated" else 1


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = _settings_or_exit(require_auth=False)
    configure_logging(settings.log_level, settings.log_format)
    return asyncio.run(_doctor(settings))


def cmd_token(_: argparse.Namespace) -> int:
    print(f"AMAZON_MCP_AUTH_TOKEN={secrets.token_urlsafe(32)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon-nl-mcp",
        description="MCP server for searching amazon.nl and filling a personal cart.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP service")
    serve.set_defaults(func=cmd_serve)

    login = sub.add_parser("login", help="sign in to amazon.nl interactively, once")
    login.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="seconds to wait for you to finish signing in (default: 900)",
    )
    login.set_defaults(func=cmd_login)

    doctor = sub.add_parser("doctor", help="check profile, browser and amazon.nl session")
    doctor.set_defaults(func=cmd_doctor)

    token = sub.add_parser("token", help="print a fresh bearer token line for the env file")
    token.set_defaults(func=cmd_token)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
