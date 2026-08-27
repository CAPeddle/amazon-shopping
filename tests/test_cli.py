"""The CLI: argument parsing, exit codes, and the safety check on `serve`."""

from __future__ import annotations

import pytest

from amazon_nl_mcp.__main__ import build_parser, cmd_serve, cmd_token, main


def test_token_prints_an_env_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_token(build_parser().parse_args(["token"])) == 0
    line = capsys.readouterr().out.strip()
    key, _, value = line.partition("=")
    assert key == "AMAZON_MCP_AUTH_TOKEN"
    assert len(value) >= 32


def test_every_subcommand_is_wired_to_a_handler() -> None:
    parser = build_parser()
    for command in ("serve", "login", "doctor", "token"):
        assert callable(parser.parse_args([command]).func)


def test_a_missing_subcommand_is_an_error() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_serve_refuses_to_expose_an_unauthenticated_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Binding beyond loopback without a token would publish the Amazon session."""
    monkeypatch.setenv("AMAZON_MCP_AUTH_DISABLED", "true")
    monkeypatch.delenv("AMAZON_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("AMAZON_MCP_HOST", "0.0.0.0")
    from amazon_nl_mcp.config import get_settings

    get_settings.cache_clear()
    try:
        assert cmd_serve(build_parser().parse_args(["serve"])) == 2
        assert "Refusing to serve unauthenticated" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()
