# Runbook

Everything that is not "install it and use it": the headless login, what the health endpoints
mean, and the failures you will actually hit.

---

## Signing in on a headless box

`amazon-nl-mcp login` opens a real browser window, because you have to type a password and
probably a one-time code into it. On a box with no display, give it one and look at it over an
SSH tunnel:

```bash
sudo apt-get install -y xvfb x11vnc

Xvfb :99 -screen 0 1600x1000x24 &
x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever -shared &

# from your laptop:
ssh -N -L 5900:localhost:5900 you@the-box
# then point any VNC client at localhost:5900
```

Then, on the box:

```bash
cd ~/amazon-shopping
DISPLAY=:99 uv run amazon-nl-mcp login
```

`x11vnc -localhost` binds loopback only, so the VNC port is reachable through the SSH tunnel and
nothing else. Kill both processes when you are done — you only need them again when the session
expires.

Three things decide whether the login sticks:

1. **Tick "Blijf ingelogd" / "Keep me signed in."** It is what promotes the session to the
   long-lived cookie. Without it you will be repeating this weekly.
2. **Press ENTER in the terminal, and let the command exit.** Chromium writes its cookie
   database on a clean close; `kill -9` loses the last few minutes of it and can leave a stale
   `SingletonLock` that blocks the next start.
3. **Confirm the nav reads `Hallo, <your name>`** before you walk away. The command checks this
   too and exits non-zero if it does not.

---

## Health endpoints

Both are unauthenticated — a watchdog has to reach them before any token exists — and neither
returns anything about the account.

| Endpoint | Meaning | Use it for |
|---|---|---|
| `GET /healthz` | The process is up. Does no browser work. | systemd/container liveness |
| `GET /readyz` | Chromium is alive **and** the amazon.nl session is valid. `503` otherwise. | readiness, monitoring |

```bash
curl -s localhost:8765/healthz | jq
curl -s -o /dev/null -w '%{http_code}\n' localhost:8765/readyz
```

`/readyz` is unauthenticated, so its answer is cached for `AMAZON_MCP_READINESS_TTL_S` (30s by
default) and it will never start a stopped browser. Polling it harder than that changes nothing:
it cannot be used to drive amazon.nl traffic, and it reports only whether the service works, never
anything about the account.

---

## The failure states

`amazon_session_status` (and `amazon-nl-mcp doctor`) collapses everything into one of these:

**`signed_out`** — the stored session expired, or Amazon invalidated it.
Run `amazon-nl-mcp login` again. If it keeps expiring in days rather than months, the
"Blijf ingelogd" checkbox was not ticked.

**`blocked`** — Amazon served a CAPTCHA or an "are you a robot" wall. The circuit breaker is now
open and every browser-backed tool refuses for `AMAZON_MCP_BOT_WALL_COOLDOWN_S`. It clears
itself. If it keeps happening:

- lower `AMAZON_MCP_RATE_LIMIT_PER_MINUTE` — 20/minute is already brisk for one person;
- run `amazon-nl-mcp login` and clear the challenge by hand in the visible browser, which
  usually settles the profile down;
- consider `AMAZON_MCP_BROWSER_CHANNEL=chrome` (after `uv run playwright install chrome`), which
  is flagged less often than the bundled Chromium.

**`browser_down`** — Chromium is not running. `systemctl --user restart amazon-nl-mcp`, then
check the journal. The common causes are a missing browser build after a `playwright` upgrade
(`uv run playwright install chromium`) and a stale singleton lock (the unit's `ExecStartPre`
clears those, but only for the profile path it knows about).

**`unreachable`** — amazon.nl could not be reached at all: no DNS, no route, or an outbound proxy
in the way. Nothing to do with Amazon or the login; check the box's own connectivity.

**`unknown`** — the page loaded but looked like nothing the selector table recognises. Almost
always an Amazon layout change: see below.

---

## When Amazon changes the layout

Symptoms: searches return zero products on a query that obviously has results, prices come back
`null`, or `amazon_add_to_cart` starts failing verification.

1. Capture the page that broke, signed in, from the same profile:

   ```bash
   uv run python - <<'PY'
   import asyncio, pathlib
   from amazon_nl_mcp.browser import BrowserSession
   from amazon_nl_mcp.config import get_settings

   async def main():
       settings = get_settings()
       async with BrowserSession(settings) as session:
           async with session.page() as page:
               await page.goto("https://www.amazon.nl/s?k=usb+c+kabel&language=nl_NL")
               pathlib.Path("tests/fixtures/search_results.html").write_text(await page.content())
   asyncio.run(main())
   PY
   ```

2. Run `uv run pytest tests/test_extract_fixtures.py`. The assertion that fails names the field
   whose selector went stale.
3. Add the new selector to the **front** of that field's list in
   `src/amazon_nl_mcp/amazon/selectors.py`. Leave the old ones behind it — Amazon A/B tests, and
   the layout you just lost may come back tomorrow.
4. Re-run the suite. Nothing outside `selectors.py` should need to change.

Scrub the captured HTML before committing it: a signed-in page carries your name in the nav and
your address in the delivery block.

---

## Exposing it to containers without exposing it to the LAN

`127.0.0.1` inside a container is the container's own loopback, so a container can never reach a
host service bound to the host's loopback. `--add-host=host.docker.internal:host-gateway` only
writes the bridge address (`172.17.0.1`) into `/etc/hosts`; it is a name for the host, not a
tunnel, and the connection is still refused if nothing is listening there.

In order of preference:

**1. Host networking on the caller.** Nothing about the service changes.

```yaml
services:
  agent:
    network_mode: host      # http://127.0.0.1:8765/mcp just works
```

**2. A socket proxy on the bridge.** The service itself stays on loopback; only a tiny,
auditable proxy is exposed.

```bash
install -Dm644 deploy/amazon-nl-mcp-bridge.socket  ~/.config/systemd/user/amazon-nl-mcp-bridge.socket
install -Dm644 deploy/amazon-nl-mcp-bridge.service ~/.config/systemd/user/amazon-nl-mcp-bridge.service
dpkg -L systemd | grep proxyd          # check the binary path matches the unit
systemctl --user daemon-reload
systemctl --user enable --now amazon-nl-mcp-bridge.socket
```

**3. Bind the bridge directly.** Simplest, and fine on a machine you control:

```ini
AMAZON_MCP_HOST=172.17.0.1
AMAZON_MCP_ALLOWED_HOSTS=172.17.0.1:8765,host.docker.internal:8765
```

Be aware that `172.17.0.1` is one of your host's addresses, and Linux's weak host model means the
box will answer for it on **any** interface — including the LAN, if something routes a frame for
it to your NIC. So scope firewall rules by address, not by interface:

```bash
sudo ufw allow from 172.16.0.0/12 to 172.17.0.1 port 8765 proto tcp
sudo ufw deny to any port 8765 proto tcp
sudo ufw status numbered
```

Never `0.0.0.0`.

---

## Rotating the bearer token

```bash
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/amazon-nl-mcp/auth_token
systemctl --user restart amazon-nl-mcp
```

Then update every client. The token is passed by systemd as a credential — a 0400 file on tmpfs
— so it never appears in the unit's environment block and `systemctl --user show amazon-nl-mcp`
will not print it.

---

## Memory limits that actually apply

The unit sets `MemoryHigh=2G` / `MemoryMax=3G`, but those are silently ignored in a user unit
unless the memory controller is delegated:

```bash
cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers
```

If `memory` is missing:

```ini
# /etc/systemd/system/user@.service.d/delegate.conf   (root-owned)
[Service]
Delegate=cpu cpuset io memory pids
```

```bash
sudo systemctl daemon-reload      # takes effect at next login / linger restart
systemctl --user show amazon-nl-mcp -p MemoryMax -p MemoryHigh
```

---

## Logs

```bash
journalctl --user -u amazon-nl-mcp -f -o cat | jq .
journalctl --user -u amazon-nl-mcp --since -1h -o cat | jq -r 'select(.level=="warning")'
```

Log records are JSON, one object per line, and pass through a redaction step first: the account
greeting, email addresses and bearer tokens are replaced with `[redacted]` before anything is
written. Page text is never logged at all. If you are debugging a parsing problem and need the
page, capture it deliberately with the recipe above rather than turning up the log level.

---

## Uninstalling

```bash
systemctl --user disable --now amazon-nl-mcp.service
rm -f ~/.config/systemd/user/amazon-nl-mcp*.{service,socket}
systemctl --user daemon-reload

# The profile is a credential for your Amazon account. Deleting it signs this
# machine's copy of the session out; it does not touch the account itself.
rm -rf ~/.local/state/amazon-nl-mcp ~/.config/amazon-nl-mcp
```

To be thorough, sign the session out from Amazon's side too, under
*Account → Login & security → Secure your account → Sign out of all devices*.
