"""The single, long-lived, logged-in Chromium.

A Playwright persistent context owns its ``user_data_dir`` exclusively, so this
service runs exactly one and serialises every tool call behind an
:class:`asyncio.Lock`. That is not a throughput compromise to apologise for: a
personal shopping session is inherently serial, and one page at a time is also
what keeps the traffic pattern human-shaped.

Three guardrails live here rather than in the tools, because all of them must
hold no matter which tool is calling:

* a token-bucket **rate limit** on browser-backed work;
* a **circuit breaker** that trips when Amazon serves an automation check, so a
  blocked service stops hammering and says so;
* **crash recovery**, because a Chromium that dies mid-call must not leave every
  later call raising ``TargetClosedError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .config import Settings
from .errors import BotWallError, BrowserUnavailableError, RateLimitedError
from .logging import get_logger

log = get_logger(__name__)

#: A recent, ordinary desktop Chrome UA. Overridable via config; the point is to
#: not ship Playwright's default, which advertises HeadlessChrome.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

#: Flags that keep a service-run Chromium from looking obviously scripted, and
#: from tripping over a container's small /dev/shm.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,AcceptCHFrame,MediaRouter,OptimizationHints",
]


class RateLimiter:
    """Sliding-window limiter over browser-backed calls."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: deque[float] = deque()

    def check(self, now: float | None = None) -> None:
        """Record one call, or raise :class:`RateLimitedError` if over budget."""
        now = time.monotonic() if now is None else now
        cutoff = now - 60.0
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self._per_minute:
            retry_after = 60.0 - (now - self._hits[0])
            raise RateLimitedError(max(retry_after, 1.0))
        self._hits.append(now)


class CircuitBreaker:
    """Refuses browser work for a cooldown after Amazon serves a bot wall."""

    def __init__(self, cooldown_s: float) -> None:
        self._cooldown_s = cooldown_s
        self._open_until: float | None = None
        self.trips = 0

    @property
    def is_open(self) -> bool:
        return self._open_until is not None and time.monotonic() < self._open_until

    def remaining_s(self) -> float:
        if self._open_until is None:
            return 0.0
        return max(0.0, self._open_until - time.monotonic())

    def trip(self) -> None:
        self.trips += 1
        self._open_until = time.monotonic() + self._cooldown_s
        log.warning("bot_wall_detected", cooldown_s=self._cooldown_s, trips=self.trips)

    def reset(self) -> None:
        self._open_until = None

    def raise_if_open(self) -> None:
        if self.is_open:
            raise BotWallError(
                f"amazon.nl served an automation check; backing off for another "
                f"{self.remaining_s():.0f}s before retrying."
            )


class BrowserSession:
    """Owns the persistent Chromium context for the life of the process."""

    def __init__(self, settings: Settings, *, headless: bool | None = None) -> None:
        self._settings = settings
        self._headless = settings.headless if headless is None else headless
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
        self.breaker = CircuitBreaker(settings.bot_wall_cooldown_s)
        self.started_at: float | None = None
        self.restarts = 0

    # -- lifecycle --------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def start(self) -> None:
        """Launch Chromium against the persistent profile. Idempotent."""
        async with self._lock:
            if self._context is not None:
                return
            await self._launch_locked()

    async def _launch_locked(self) -> None:
        profile = self._settings.profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            profile.chmod(0o700)
        _clear_stale_singleton_locks(profile)

        args = list(LAUNCH_ARGS)
        if self._settings.browser_no_sandbox:
            args.append("--no-sandbox")

        log.info("browser_starting", profile_dir=str(profile), headless=self._headless)
        self._playwright = await async_playwright().start()
        try:
            self._context = await asyncio.wait_for(
                self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    headless=self._headless,
                    channel=self._settings.browser_channel,
                    executable_path=(
                        str(self._settings.browser_executable_path)
                        if self._settings.browser_executable_path
                        else None
                    ),
                    args=args,
                    locale=self._settings.locale,
                    timezone_id=self._settings.timezone,
                    user_agent=self._settings.user_agent or DEFAULT_USER_AGENT,
                    viewport={"width": 1440, "height": 900},
                    accept_downloads=False,
                    ignore_default_args=["--enable-automation"],
                ),
                timeout=self._settings.browser_launch_timeout_s,
            )
        except BaseException as exc:
            # Includes CancelledError: a launch cancelled by a tool timeout must
            # still stop the driver it started, or the next launch overwrites the
            # reference and leaks the process.
            await self._teardown_locked()
            if not isinstance(exc, TimeoutError | PlaywrightError | OSError):
                raise
            raise BrowserUnavailableError(
                f"Could not start Chromium against {profile}: {exc}",
                hint=(
                    "Check that `playwright install --with-deps chromium` has been run as the "
                    "service user, and that no other process is using the profile directory."
                ),
            ) from exc

        self._context.set_default_navigation_timeout(self._settings.nav_timeout_ms)
        self._context.set_default_timeout(self._settings.nav_timeout_ms)
        await self._context.add_init_script(_STEALTH_INIT_SCRIPT)
        self.started_at = time.time()
        log.info("browser_started", restarts=self.restarts)

    async def stop(self) -> None:
        async with self._lock:
            await self._teardown_locked()

    async def _teardown_locked(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
            self._context = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
        self.started_at = None

    async def restart(self) -> None:
        """Tear the browser down and bring it back. Used after a crash."""
        async with self._lock:
            await self._teardown_locked()
            self.restarts += 1
            await self._launch_locked()

    # -- use --------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def page(self, *, rate_limited: bool = True) -> AsyncIterator[Page]:
        """Yield a fresh page, exclusively, with every guardrail applied.

        A page per call rather than one reused page: it costs a few hundred
        milliseconds and removes a whole class of stale-DOM bugs between tools.
        """
        async with self._lock:
            # Both guards are checked *after* the lock, not before it. Checked
            # before, a burst of calls would all pass while the first one was
            # still running, and the ones queued behind a call that hits a bot
            # wall would proceed straight into the wall the breaker just closed.
            self.breaker.raise_if_open()
            if rate_limited:
                self.rate_limiter.check()

            if self._context is None:
                await self._launch_locked()
            assert self._context is not None
            try:
                page = await self._context.new_page()
            except PlaywrightError:
                log.warning("browser_page_failed_restarting")
                await self._teardown_locked()
                self.restarts += 1
                await self._launch_locked()
                assert self._context is not None
                try:
                    page = await self._context.new_page()
                except PlaywrightError as exc:  # pragma: no cover - double failure
                    raise BrowserUnavailableError(
                        f"Chromium would not open a page even after a restart: {exc}"
                    ) from exc
            try:
                yield page
            finally:
                with contextlib.suppress(Exception):
                    await page.close()

    async def __aenter__(self) -> BrowserSession:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()


def _clear_stale_singleton_locks(profile: Path) -> None:
    """Remove Chromium's singleton locks, but only when they are actually stale.

    ``SingletonLock`` is a symlink to ``<host>-<pid>``. Removing one whose PID is
    still alive would let a second Chromium open the same profile, and two
    Chromiums on one profile corrupt the cookie database — which here means
    losing the login. So the PID is checked first, and a lock we cannot prove is
    dead is left alone: a failed launch with a clear error beats a corrupted
    profile.
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        candidate = profile / name
        if not (candidate.is_symlink() or candidate.exists()):
            continue
        if _lock_holder_is_alive(candidate):
            log.warning("singleton_lock_held", lock=name)
            continue
        with contextlib.suppress(OSError):
            candidate.unlink()


def _lock_holder_is_alive(lock: Path) -> bool:
    """Whether the process named in a singleton lock is still running.

    The link target is ``<hostname>-<pid>``. A lock from another host, or one we
    cannot parse, counts as alive: not deleting it is the safe mistake.
    """
    try:
        target = str(lock.readlink())
    except OSError:
        return False  # not a symlink at all: nothing holds it
    _, _, pid_part = target.rpartition("-")
    if not pid_part.isdigit():
        return True
    if target[: -len(pid_part) - 1] not in {socket.gethostname(), ""}:
        return True
    try:
        os.kill(int(pid_part), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    except OSError:
        return True
    return True


#: Papers over the two headless tells that cost nothing to fix. This is not an
#: anti-detection arms race — if Amazon blocks the session, the breaker trips and
#: the caller is told to sort it out in a real browser.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['nl-NL', 'nl', 'en-US', 'en']});
"""
