# amazon-nl-mcp

An MCP server that searches **amazon.nl** and fills your personal Amazon cart, running as a
service on your own Ubuntu box so anything else on that machine — Claude Code, Claude Desktop,
a script, a container — can shop through it.

It stops at the cart. There is no code path in this project that places an order.

```
 MCP client on your box ──HTTP+bearer──▶ FastAPI ──▶ MCP (streamable HTTP)
                                                       │
                                                       ▼
                                          one persistent Chromium,
                                          logged in to amazon.nl as you
```

## Why a browser

Amazon publishes no API that can put something in a consumer's cart. The Product Advertising API
covers product data for Associates, SP-API covers sellers; neither touches your basket. So the
service drives a real browser with a real, persistent login — the same session you would use by
hand, kept in a profile directory on your machine.

Two consequences worth knowing before you install it:

- **The profile directory is a credential.** Anyone who can read it can act as you on Amazon.
  It is created 0700, lives under `~/.local/state/amazon-nl-mcp/`, and is gitignored. Don't
  back it up to a cloud drive.
- **Amazon's Conditions of Use prohibit automated access**, including to your own account. The
  practical risk is account-level enforcement — CAPTCHAs, throttling, in the worst case
  suspension, which would take Prime, order history and digital purchases with it. This server
  keeps its traffic to a human pace (rate limit, one page at a time, backing off on the first
  automation check) because that is the mitigation that matters. Your machine, your account,
  your call.

## The tools

| Tool | What it does | Writes? |
|---|---|---|
| `amazon_search_products` | Search the storefront. Returns ASIN, title, price, rating, availability. Filters sponsored rows by default. | no |
| `amazon_get_product` | One product in full: buy-box price, stock, bullets, and the child ASINs for listings that come in sizes or colours. | no |
| `amazon_add_to_cart` | Add an ASIN to your cart, then **verify it by re-reading the cart**. | cart only |
| `amazon_view_cart` | Line items, quantities, subtotal, and the URL to check out by hand. | no |
| `amazon_session_status` | Whether the server can currently act: signed in, signed out, blocked, or browser down. Never raises. | no |

There is no remove, no quantity edit, and no checkout. If you want the cart emptied, do it in
your browser.

## Install

Ubuntu 22.04+, Python 3.11+, [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/CAPeddle/amazon-shopping.git ~/amazon-shopping
cd ~/amazon-shopping
./deploy/install.sh          # deps, Chromium, a bearer token, the systemd unit
```

Then sign in **once**, in a browser you can see:

```bash
uv run amazon-nl-mcp login
```

Tick *"Blijf ingelogd"* when you sign in — that checkbox is what makes the session last months
instead of days. Complete any 2FA, confirm the nav reads `Hallo, <your name>`, then press ENTER
in the terminal. Don't `kill -9` it: Chromium flushes its cookie store on a clean close.

On a headless box the login window needs somewhere to appear — see
[docs/runbook.md](docs/runbook.md#signing-in-on-a-headless-box) for the Xvfb + VNC recipe.

Finally:

```bash
systemctl --user enable --now amazon-nl-mcp.service
uv run amazon-nl-mcp doctor          # profile, browser, session, all in one line each
```

## Connect a client

The endpoint is `http://127.0.0.1:8765/mcp` and every request needs the bearer token from
`~/.config/amazon-nl-mcp/auth_token`.

**Claude Code**

```bash
claude mcp add --transport http amazon-nl http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $(cat ~/.config/amazon-nl-mcp/auth_token)"
```

**`.mcp.json`** (Claude Code project config, or `~/.claude.json`)

```json
{
  "mcpServers": {
    "amazon-nl": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${AMAZON_MCP_TOKEN}" }
    }
  }
}
```

`type` is required — a `url` entry without it is read as stdio and fails. Keep the token in the
environment rather than in the file; `${VAR}` is expanded on load.

**Claude Desktop** cannot reach a localhost HTTP server: its stdio config has no HTTP transport,
and custom connectors dial out from Anthropic's cloud. Bridge it with
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "amazon-nl": {
      "command": "npx",
      "args": [
        "mcp-remote", "http://127.0.0.1:8765/mcp",
        "--transport", "http-only",
        "--header-file", "/home/you/.config/amazon-nl-mcp/headers.txt"
      ]
    }
  }
}
```

where `headers.txt` is one `Authorization: Bearer <token>` line, mode 600. A header file rather
than `--header` keeps the token out of the process argv, where any local user can read it.

**Anything else on the box** — it is an ordinary HTTP service:

```bash
TOKEN=$(cat ~/.config/amazon-nl-mcp/auth_token)
curl -s localhost:8765/healthz
curl -s localhost:8765/readyz            # 503 until the amazon.nl session is valid
curl -s -H "Authorization: Bearer $TOKEN" localhost:8765/mcp -X POST \
     -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

**From a container on the same box**, `127.0.0.1` is the container's own loopback and can never
reach a host process bound to the host's loopback. Two ways out, best first:

1. Give the container host networking (`network_mode: host`) and change nothing here.
2. Bind the docker bridge and tell MCP's DNS-rebinding protection about it:

   ```ini
   AMAZON_MCP_HOST=172.17.0.1
   AMAZON_MCP_ALLOWED_HOSTS=172.17.0.1:8765,host.docker.internal:8765
   ```

   `172.17.0.1` is a host address, not a container-only one: under Linux's weak host model your
   box will answer for it on any interface, so pair this with the firewall rules in
   [docs/runbook.md](docs/runbook.md#exposing-it-to-containers-without-exposing-it-to-the-lan).
   `deploy/` also ships a socket-proxy unit that keeps the app itself on loopback.

Never bind `0.0.0.0`. The bearer token is the only thing between a caller and your Amazon
account.

## Using it

Search, look, add, confirm:

> *"Find me a 2 metre USB-C cable under €20 with good reviews and put it in my cart."*

The model calls `amazon_search_products`, picks a candidate, usually checks it with
`amazon_get_product`, calls `amazon_add_to_cart`, and gets back a result that was verified
against the cart page — not against Amazon's confirmation banner, which appears even for adds
that changed nothing.

When something fails, `amazon_session_status` tells you which of the four failures it is:

| state | meaning | fix |
|---|---|---|
| `authenticated` | working | — |
| `signed_out` | the stored session expired | `uv run amazon-nl-mcp login` |
| `blocked` | Amazon served an automation check | wait out the cooldown; if it persists, clear it by hand in `login` |
| `unreachable` | amazon.nl could not be reached from this host | check the box's own connectivity |
| `browser_down` | Chromium is not running | `systemctl --user restart amazon-nl-mcp` |

## Configuration

Every setting is an `AMAZON_MCP_`-prefixed environment variable, read from
`~/.config/amazon-nl-mcp/env`. [`deploy/env.example`](deploy/env.example) documents the ones you
are likely to touch; `src/amazon_nl_mcp/config.py` is the full list with defaults.

The ones that matter most:

| Variable | Default | Why you'd change it |
|---|---|---|
| `AMAZON_MCP_HOST` / `_PORT` | `127.0.0.1` / `8765` | Reaching the service from containers. |
| `AMAZON_MCP_ALLOWED_HOSTS` | *(none)* | Required whenever you bind anything but loopback. |
| `AMAZON_MCP_RATE_LIMIT_PER_MINUTE` | `20` | Lower is safer. Raising it is the quickest route to a CAPTCHA. |
| `AMAZON_MCP_WRITE_ENABLED` | `true` | Set `false` for a search-only deployment. |
| `AMAZON_MCP_BROWSER_CHANNEL` | *(bundled Chromium)* | `chrome` uses the real Google Chrome, which is flagged slightly less often. |
| `AMAZON_MCP_AUTH_DISABLED` | `false` | Only ever with `AMAZON_MCP_HOST=127.0.0.1`; `serve` refuses the combination otherwise. |

## Development

```bash
uv sync
uv run playwright install chromium
uv run pytest              # 128 tests, no network, no amazon.nl
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
```

The suite never touches Amazon. Parser tests load saved pages from `tests/fixtures/` into a real
Chromium; flow tests intercept every navigation with `page.route` and serve those same fixtures,
so the code under test is the production client all the way down to the DOM.

**When Amazon changes a layout** — which they will — re-capture the affected page into
`tests/fixtures/` and run the suite. The assertion that breaks names the selector that went
stale, and `src/amazon_nl_mcp/amazon/selectors.py` is the only file that should need editing.
Each field there is an ordered list of candidates, so a layout change usually degrades one field
rather than breaking the call.

## Layout

```
src/amazon_nl_mcp/
├── config.py       every setting, with its default and its reason
├── logging.py      structlog + the redaction that keeps your name and address out of journald
├── errors.py       the failures a caller should see, each with its remedy
├── models.py       the wire models, which are also the MCP output schemas
├── browser.py      the single persistent Chromium: lock, rate limit, circuit breaker, recovery
├── server.py       the five tools
├── app.py          FastAPI host: bearer gate, health endpoints, the mounted MCP app
├── __main__.py     serve | login | doctor | token
└── amazon/
    ├── selectors.py  the selector table — the one file layout changes touch
    ├── extract.py    DOM to models
    └── client.py     the page flows, and the cart-delta verification
```

## Licence

MIT. See [LICENSE](LICENSE).
