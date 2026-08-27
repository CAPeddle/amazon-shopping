#!/usr/bin/env bash
# One-shot setup for the systemd user service. Idempotent; safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/amazon-nl-mcp"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

command -v uv >/dev/null || { echo "uv is not installed: https://docs.astral.sh/uv/" >&2; exit 1; }

echo "==> Installing dependencies"
(cd "$REPO_DIR" && uv sync --frozen)

echo "==> Installing Chromium"
(cd "$REPO_DIR" && uv run playwright install chromium)

# The shared libraries Chromium links against are an apt install, so this step
# and only this step needs root. Skipped with a warning if sudo is unavailable:
# on a box that has run a browser before, they are usually already present.
if command -v sudo >/dev/null; then
  echo "==> Installing Chromium's system libraries (sudo)"
  (cd "$REPO_DIR" && sudo -E "$(uv run --frozen python -c 'import shutil; print(shutil.which("playwright"))')" install-deps chromium) \
    || echo "!! could not install system libraries; if the browser fails to start, run: sudo playwright install-deps chromium"
else
  echo "!! no sudo; skipping system libraries. If Chromium fails to start, install them by hand"
  echo "   (see docs/runbook.md) or run: uv run playwright install --with-deps chromium"
fi

install -d -m 700 "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/auth_token" ]]; then
  echo "==> Generating a bearer token"
  (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$CONFIG_DIR/auth_token")
fi
chmod 600 "$CONFIG_DIR/auth_token"
[[ -f "$CONFIG_DIR/env" ]] || install -m 600 "$REPO_DIR/deploy/env.example" "$CONFIG_DIR/env"

install -d -m 755 "$UNIT_DIR"
install -m 644 "$REPO_DIR/deploy/amazon-nl-mcp.service" "$UNIT_DIR/amazon-nl-mcp.service"

# Without linger the whole user manager — and the browser with it — is torn
# down when the last login session ends.
loginctl enable-linger "$USER" || echo "!! could not enable linger; the service will stop when you log out"
systemctl --user daemon-reload

cat <<EOF

Installed. Two steps left, in this order:

  1. Sign in to amazon.nl once, in a visible browser:

       cd "$REPO_DIR" && uv run amazon-nl-mcp login

     On a headless box, see docs/runbook.md for the Xvfb + VNC recipe.

  2. Start the service:

       systemctl --user enable --now amazon-nl-mcp.service
       systemctl --user status amazon-nl-mcp.service

Your bearer token (keep it out of shared configs):

  $(cat "$CONFIG_DIR/auth_token")

EOF
