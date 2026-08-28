# Runbook

Everything that is not "install it and use it": the headless login, what the health endpoints
mean, and the failures you will actually hit.

---

## Signing in

`amazon-nl-mcp login` opens a real browser window, because you have to type a password and
probably a one-time code into it. Where that window appears depends on what the box has.

### The box has a desktop session (the common case)

If the machine runs a graphical session — even one nobody is sitting at — the window can go on
the display it already has. Over SSH:

```bash
cd ~/amazon-shopping
DISPLAY=:0 uv run amazon-nl-mcp login
```

The window opens on the machine's own screen. To *see* it from elsewhere, either use whatever
remote desktop you already reach the box with, or forward X to the machine you are sitting at:

```bash
ssh -X you@the-box                        # or `ssh -Y` if -X is refused
cd ~/amazon-shopping && uv run amazon-nl-mcp login
```

With `ssh -X` the browser renders on *your* screen and `DISPLAY` is already set for you. Chrome
over forwarded X11 is sluggish, which does not matter for a one-off sign-in.

Check what the box actually has before choosing:

```bash
ls /tmp/.X11-unix/                        # X0 => a session on :0
loginctl list-sessions                    # Type=x11/wayland => graphical
echo "$XDG_SESSION_TYPE"                  # from inside a session
```

On Wayland, `DISPLAY=:0` still works through Xwayland. If it does not, use `ssh -X`.

### The box has no display at all

Give it one and look at it over an SSH tunnel:

```bash
sudo apt-get install -y xvfb x11vnc

Xvfb :99 -screen 0 1600x1000x24 &
x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever &

# from your laptop:
ssh -N -L 5900:localhost:5900 you@the-box
# then point any VNC client at localhost:5900
```

```bash
cd ~/amazon-shopping
DISPLAY=:99 uv run amazon-nl-mcp login
```

`x11vnc -localhost` binds loopback only, so the VNC port is reachable through the tunnel and
nothing else. Kill both processes when you are done.

### Whichever route you took

Three things decide whether the login sticks:

1. **Tick "Blijf ingelogd" / "Keep me signed in."** It is what promotes the session to the
   long-lived cookie. Without it you will be repeating this weekly.
2. **Press ENTER in the terminal, and let the command exit.** Chromium writes its cookie
   database on a clean close; `kill -9` loses the last few minutes of it and can leave a stale
   `SingletonLock` that blocks the next start.
3. **Confirm the nav reads `Hallo, <your name>`** before you walk away. The command checks this
   too and exits non-zero if it does not.

---

## Running the browser headful

Headless Chromium is one of the loudest signals Amazon has: it renders differently, reports no
GPU, and skips work a real compositor does. If the box has a display, you can spend it and take
that signal away.

```ini
# ~/.config/amazon-nl-mcp/env
AMAZON_MCP_HEADLESS=false
AMAZON_MCP_BROWSER_CHANNEL=chrome     # real Google Chrome, not the bundled build
```

and uncomment the two display lines in the unit, so the service worker can reach the session:

```ini
# ~/.config/systemd/user/amazon-nl-mcp.service
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
```

```bash
uv run playwright install chrome      # only if you set BROWSER_CHANNEL
systemctl --user daemon-reload && systemctl --user restart amazon-nl-mcp
```

What you are trading:

- The service now **depends on a graphical session existing**. Reboot to a display manager with
  nobody logged in and the browser cannot start; `/readyz` reports `browser_down` and stays there
  until someone logs in. Headless has no such dependency. Enable autologin if you want headful
  without that fragility.
- A browser window exists on the desktop. It is not in your way unless you are sitting there,
  but it is visible to anyone who is.
- Slightly more memory, and a real GPU process.

This does not make the traffic undetectable — the profile is still a dedicated one and Playwright
still drives it over CDP. It removes the single cheapest tell, for a config change.

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

## Reaching it from your laptop over Tailscale

If you reach the box on a tailnet, that is the right boundary to serve on: the address only
exists inside your tailnet, Tailscale ACLs gate who can route to it, and the bearer token is the
second lock rather than the only one.

```bash
tailscale ip -4                           # e.g. 100.101.102.103
tailscale status --json | jq -r '.Self.DNSName'   # e.g. the-box.tailnet-1234.ts.net.
```

```ini
# ~/.config/amazon-nl-mcp/env
AMAZON_MCP_HOST=100.101.102.103
AMAZON_MCP_ALLOWED_HOSTS=100.101.102.103:8765,the-box.tailnet-1234.ts.net:8765
```

The allowlist is not optional. MCP's DNS-rebinding protection checks the `Host` header and knows
only about loopback by default, so a client dialling the MagicDNS name gets a bare
`421 Misdirected Request` until that name is listed. List both the IP and the DNS name — clients
differ in which they send.

Then from your laptop:

```bash
claude mcp add --transport http amazon-nl http://the-box.tailnet-1234.ts.net:8765/mcp \
  --header "Authorization: Bearer $TOKEN"
```

Two things to get right:

- **Bind the tailnet address, not `0.0.0.0`.** Binding the Tailscale IP puts the listener on that
  interface alone. `0.0.0.0` also publishes it to whatever LAN the box is on, where nothing gates
  it but the token.
- **`tailscale serve`, never `tailscale funnel`.** `serve` puts it behind HTTPS with a real
  certificate for clients that insist on TLS, still tailnet-only:

  ```bash
  tailscale serve --bg --https=443 http://127.0.0.1:8765
  ```

  `funnel` is the same command aimed at the public internet. Do not point it at a service holding
  a logged-in Amazon session.

With `serve` the app stays on `127.0.0.1` and Tailscale terminates TLS, so the Host header becomes
the MagicDNS name — list it in `AMAZON_MCP_ALLOWED_HOSTS` exactly as above.

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

The proxy forwards the request verbatim, `Host: 172.17.0.1:8765` included, and MCP's
DNS-rebinding protection checks that header — so this route still needs the address in the
allowlist, even though the app is bound to loopback:

```ini
# ~/.config/amazon-nl-mcp/env
AMAZON_MCP_ALLOWED_HOSTS=172.17.0.1:8765,host.docker.internal:8765
```

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
