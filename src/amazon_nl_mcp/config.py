"""Configuration surface for the amazon.nl MCP service.

Every setting is an environment variable prefixed ``AMAZON_MCP_``. Defaults are
chosen so that a bare ``amazon-nl-mcp serve`` on a personal Ubuntu box does the
safe thing: bind loopback only, refuse unauthenticated callers, never place an
order.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    """Runtime configuration, read once at process start."""

    model_config = SettingsConfigDict(
        env_prefix="AMAZON_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- HTTP -------------------------------------------------------------
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address. Keep 127.0.0.1 for host-local callers. Use 172.17.0.1 "
            "(the docker0 bridge) to also serve containers without exposing the LAN."
        ),
    )
    port: int = Field(default=8765, ge=1, le=65535, description="TCP port to listen on.")
    mcp_path: str = Field(default="/mcp", description="Path of the Streamable HTTP MCP endpoint.")
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Extra Host header values accepted by MCP's DNS-rebinding protection, on top of "
            "localhost. Required when reaching the service by IP, e.g. '172.17.0.1:8765'."
        ),
    )
    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Extra Origin header values accepted (browser callers only).",
    )

    # -- Auth -------------------------------------------------------------
    auth_token: str | None = Field(
        default=None,
        description=(
            "Static bearer token every caller must present as 'Authorization: Bearer <token>'. "
            "Required unless auth_disabled is set."
        ),
    )
    auth_token_file: Path | None = Field(
        default=None,
        description=(
            "File to read the bearer token from, in preference to the env var. Defaults to "
            "$CREDENTIALS_DIRECTORY/auth_token when systemd passes a credential, which keeps the "
            "token off the environment block that `systemctl show` prints."
        ),
    )
    auth_disabled: bool = Field(
        default=False,
        description="Serve without authentication. Only sane when bound to 127.0.0.1 on a single-user box.",
    )

    # -- Browser ----------------------------------------------------------
    profile_dir: Path = Field(
        default=Path("~/.local/share/amazon-nl-mcp/profile"),
        description=(
            "Chromium user-data-dir holding the logged-in amazon.nl session. "
            "SECRET: it grants full access to the account. chmod 700, never commit or back up."
        ),
    )
    headless: bool = Field(default=True, description="Run Chromium headless. 'login' always runs headful.")
    browser_channel: str | None = Field(
        default=None,
        description="Chromium channel to launch, e.g. 'chrome'. None uses Playwright's bundled build.",
    )
    browser_no_sandbox: bool = Field(
        default=False,
        description=(
            "Pass --no-sandbox to Chromium. Needed when the service runs as root or in a "
            "container without the required kernel namespaces; leave off for a normal user."
        ),
    )
    browser_executable_path: Path | None = Field(
        default=None,
        description=(
            "Explicit Chromium binary to launch. Escape hatch for hosts where the bundled build "
            "cannot be downloaded, e.g. /usr/bin/chromium."
        ),
    )
    locale: str = Field(default="nl-NL", description="Browser locale sent to Amazon.")
    timezone: str = Field(default="Europe/Amsterdam", description="Browser timezone.")
    user_agent: str | None = Field(default=None, description="Override the browser User-Agent.")
    nav_timeout_ms: int = Field(
        default=30_000, ge=1_000, le=180_000, description="Per-navigation timeout in milliseconds."
    )
    browser_launch_timeout_s: float = Field(
        default=60.0, gt=0, description="How long to wait for Chromium to start."
    )
    tool_timeout_s: float = Field(
        default=90.0, gt=0, description="Hard ceiling on a single tool call's browser work."
    )

    # -- Amazon -----------------------------------------------------------
    base_url: str = Field(default="https://www.amazon.nl", description="Amazon storefront root.")
    max_results: int = Field(default=10, ge=1, le=60, description="Default number of search results.")

    # -- Guardrails -------------------------------------------------------
    rate_limit_per_minute: int = Field(
        default=20, ge=1, le=600, description="Maximum browser-backed tool calls per rolling minute."
    )
    bot_wall_cooldown_s: float = Field(
        default=120.0,
        ge=0,
        description="After Amazon serves a bot wall, refuse browser work for this long.",
    )
    write_enabled: bool = Field(
        default=True, description="Allow cart mutations. Set false for a read-only deployment."
    )

    status_timeout_s: float = Field(
        default=45.0,
        gt=0,
        description=(
            "Ceiling on amazon_session_status. It is the tool you reach for when everything else "
            "is failing, so it degrades to a 'unknown' answer rather than hanging."
        ),
    )
    readiness_ttl_s: float = Field(
        default=30.0,
        ge=0,
        description=(
            "How long /readyz may serve a cached answer. It is unauthenticated, so this is what "
            "stops a monitoring loop from turning into amazon.nl traffic."
        ),
    )

    # -- Observability ----------------------------------------------------
    log_level: str = Field(default="INFO", description="Root log level.")
    log_format: LogFormat = Field(
        default="console", description="'json' for journald parsing, 'console' for humans."
    )
    debug_artifacts_dir: Path | None = Field(
        default=None,
        description="If set, failed page interactions dump a screenshot and HTML here for debugging.",
    )

    @field_validator(
        "profile_dir", "debug_artifacts_dir", "browser_executable_path", "auth_token_file", mode="before"
    )
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str) and v:
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("mcp_path")
    @classmethod
    def _leading_slash(cls, v: str) -> str:
        return v if v.startswith("/") else f"/{v}"

    @model_validator(mode="after")
    def _load_token_from_credential(self) -> Settings:
        """Prefer a token file over the environment.

        systemd's ``LoadCredential=`` drops the token into a 0400 file on tmpfs
        and exports ``CREDENTIALS_DIRECTORY``; reading it from there means the
        secret never appears in the unit's environment block.
        """
        path = self.auth_token_file
        if path is None:
            credentials = os.environ.get("CREDENTIALS_DIRECTORY")
            if credentials:
                candidate = Path(credentials) / "auth_token"
                path = candidate if candidate.is_file() else None
        if path is not None:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"Could not read the token file {path}: {exc}") from exc
            if token:
                object.__setattr__(self, "auth_token", token)
        return self

    @model_validator(mode="after")
    def _auth_is_configured(self) -> Settings:
        if not self.auth_disabled and not self.auth_token:
            raise ValueError(
                "No AMAZON_MCP_AUTH_TOKEN set. Generate one with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`, or set "
                "AMAZON_MCP_AUTH_DISABLED=true if you accept an unauthenticated loopback service."
            )
        if self.auth_token is not None and len(self.auth_token) < 16:
            raise ValueError("AMAZON_MCP_AUTH_TOKEN must be at least 16 characters.")
        return self

    @property
    def public_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.mcp_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
