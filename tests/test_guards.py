"""Guardrails, config validation, and log redaction.

Pure logic — no browser, no network.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

import pytest
import structlog

from amazon_nl_mcp.browser import CircuitBreaker, RateLimiter, _clear_stale_singleton_locks
from amazon_nl_mcp.config import Settings
from amazon_nl_mcp.errors import BotWallError, RateLimitedError
from amazon_nl_mcp.logging import _redact, configure_logging


class TestRateLimiter:
    def test_allows_up_to_the_budget(self) -> None:
        limiter = RateLimiter(per_minute=3)
        for tick in range(3):
            limiter.check(now=100.0 + tick)

    def test_refuses_the_call_over_budget(self) -> None:
        limiter = RateLimiter(per_minute=2)
        limiter.check(now=100.0)
        limiter.check(now=101.0)
        with pytest.raises(RateLimitedError) as excinfo:
            limiter.check(now=102.0)
        assert excinfo.value.retry_after_s > 0

    def test_the_window_slides(self) -> None:
        limiter = RateLimiter(per_minute=2)
        limiter.check(now=100.0)
        limiter.check(now=101.0)
        # 61s after the first call, its slot is free again.
        limiter.check(now=161.5)

    def test_the_error_says_when_to_retry(self) -> None:
        limiter = RateLimiter(per_minute=1)
        limiter.check(now=100.0)
        with pytest.raises(RateLimitedError) as excinfo:
            limiter.check(now=110.0)
        assert "retry in" in str(excinfo.value)


class TestCircuitBreaker:
    def test_starts_closed(self) -> None:
        breaker = CircuitBreaker(cooldown_s=60.0)
        assert breaker.is_open is False
        breaker.raise_if_open()

    def test_trips_and_refuses_work(self) -> None:
        breaker = CircuitBreaker(cooldown_s=60.0)
        breaker.trip()
        assert breaker.is_open is True
        assert breaker.trips == 1
        with pytest.raises(BotWallError) as excinfo:
            breaker.raise_if_open()
        assert "backing off" in str(excinfo.value)

    def test_a_zero_cooldown_closes_immediately(self) -> None:
        breaker = CircuitBreaker(cooldown_s=0.0)
        breaker.trip()
        assert breaker.is_open is False

    def test_reset_reopens_the_service(self) -> None:
        breaker = CircuitBreaker(cooldown_s=60.0)
        breaker.trip()
        breaker.reset()
        assert breaker.is_open is False
        assert breaker.trips == 1, "the counter survives a reset; it is a health signal"


class TestSettings:
    def test_refuses_to_start_without_a_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AMAZON_MCP_AUTH_TOKEN", raising=False)
        with pytest.raises(ValueError, match="AMAZON_MCP_AUTH_TOKEN"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_refuses_a_short_token(self) -> None:
        with pytest.raises(ValueError, match="at least 16"):
            Settings(auth_token="short")

    def test_auth_can_be_switched_off_explicitly(self) -> None:
        assert Settings(auth_disabled=True, auth_token=None).auth_disabled is True

    def test_comma_separated_host_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AMAZON_MCP_ALLOWED_HOSTS", "172.17.0.1:8765, box.local")
        settings = Settings(auth_token="0123456789abcdef0")
        assert settings.allowed_hosts == ["172.17.0.1:8765", "box.local"]

    def test_paths_are_expanded(self) -> None:
        settings = Settings(auth_token="0123456789abcdef0", profile_dir=Path("~/x/profile"))
        assert "~" not in str(settings.profile_dir)

    def test_base_url_loses_its_trailing_slash(self) -> None:
        assert Settings(auth_token="0123456789abcdef0", base_url="https://www.amazon.nl/").base_url == (
            "https://www.amazon.nl"
        )

    def test_mcp_path_gains_a_leading_slash(self) -> None:
        assert Settings(auth_token="0123456789abcdef0", mcp_path="shop").mcp_path == "/shop"


class TestRedaction:
    def test_sensitive_keys_are_replaced(self) -> None:
        event = _redact(None, "info", {"auth_token": "s3cret", "account_name": "Chris", "asin": "B0"})
        assert event["auth_token"] == "[redacted]"
        assert event["account_name"] == "[redacted]"
        assert event["asin"] == "B0", "ordinary fields are untouched"

    def test_emails_and_bearer_tokens_are_scrubbed_from_free_text(self) -> None:
        event = _redact(
            None,
            "info",
            {"detail": "failed for chris@example.com with Authorization: Bearer abc.def-123"},
        )
        assert "chris@example.com" not in event["detail"]
        assert "abc.def-123" not in event["detail"]

    def test_the_nav_greeting_is_scrubbed(self) -> None:
        event = _redact(None, "info", {"page": "nav says Hallo, Christopher Peddle"})
        assert "Christopher" not in event["page"]

    def test_configure_logging_installs_the_redactor(self) -> None:
        original = structlog.get_config()
        try:
            configure_logging("WARNING", "json")
            assert logging.getLogger().level == logging.WARNING
            processors = structlog.get_config()["processors"]
            assert _redact in processors, "no redaction means account data reaches journald"
            # It must run before the renderer, or it would be scrubbing a string.
            assert processors.index(_redact) < len(processors) - 1
        finally:
            # Leave global logging as this suite found it; a later test that
            # captures output would otherwise inherit this configuration.
            structlog.configure(**original)
            logging.basicConfig(force=True)


class TestSingletonLocks:
    def test_a_live_holder_keeps_its_lock(self, tmp_path: Path) -> None:
        """Deleting a live lock would let two Chromiums share one profile and corrupt it."""
        lock = tmp_path / "SingletonLock"
        lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
        _clear_stale_singleton_locks(tmp_path)
        assert lock.is_symlink()

    def test_a_dead_holders_lock_is_cleared(self, tmp_path: Path) -> None:
        lock = tmp_path / "SingletonLock"
        lock.symlink_to(f"{socket.gethostname()}-4294967")  # above PID_MAX, cannot exist
        _clear_stale_singleton_locks(tmp_path)
        assert not lock.is_symlink()

    def test_a_lock_from_another_host_is_left_alone(self, tmp_path: Path) -> None:
        lock = tmp_path / "SingletonLock"
        lock.symlink_to("some-other-host-1")
        _clear_stale_singleton_locks(tmp_path)
        assert lock.is_symlink()

    def test_an_unparseable_lock_is_left_alone(self, tmp_path: Path) -> None:
        lock = tmp_path / "SingletonLock"
        lock.symlink_to("nonsense")
        _clear_stale_singleton_locks(tmp_path)
        assert lock.is_symlink()
