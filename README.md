# sliv

sliv is a streaming proxy that turns an upstream HLS source identified by `id` into browser-friendly HLS and DASH outputs. It resolves an upstream playlist, rewrites media and key references to signed local proxy URLs, and serves the underlying segments through controlled proxy endpoints.

The repository supports three execution modes:

- Local Python using Flask in [main.py](main.py)
- Appwrite Functions using the Python runtime in [Appwrite/](Appwrite)
- Cloudflare Workers using the native Worker implementation in [cloudflare/worker.js](cloudflare/worker.js)

## Architecture overview

### Request flow

1. A client requests `GET /playlist.m3u8?id=<id>` or `GET /playlist.mpd?id=<id>`.
2. The service selects an upstream template from `UPSTREAM_SOURCES` and fills in the requested `id`.
3. The upstream request is sent with a realistic browser user agent and the referer required by that source.
4. The response is cached briefly in memory so repeated segment and playlist requests do not hammer the upstream.
5. If the response is a playlist, media URIs, key URIs, and playlist-relative paths are rewritten to local signed proxy URLs.
6. Clients then request `/stream/<token>` or `/dash/<token>`, and the proxy fetches the original resource on their behalf.

### Token system

Proxy URLs contain a compact signed token. Each token stores:

- the target URL
- the referer to send to the upstream source
- an expiration timestamp
- optional DASH key metadata and IV information

The token payload is signed with HMAC-SHA256 and truncated for compactness. Expired or tampered tokens are rejected before any upstream fetch happens.

### Upstream source handling

The upstream list is defined in code as a small set of templates. Each request shuffles the list and tries the sources in a different order. This gives the proxy a fallback path when one source is unavailable or rate-limited.

The proxy also preserves upstream-specific referer values, which is necessary for sources that validate hotlinking or origin context.

### Playlist and DASH behavior

- HLS master playlists are parsed so `bandwidth=low` or `bandwidth=high` can narrow the variant set.
- Media playlists are rewritten so every segment becomes a signed proxy URL.
- DASH manifests are synthesized from HLS playlists by converting segments into `SegmentList` representations.
- When DASH segment encryption metadata is present, AES-CBC decryption is applied before the response is returned.

## Repository layout

- [main.py](main.py): local Flask implementation and shared proxy logic
- [requirements.txt](requirements.txt): Python dependencies for local execution
- [Appwrite/](Appwrite): Appwrite Function adapter and deployment notes
- [cloudflare/](cloudflare): Cloudflare Worker implementation and deployment notes

## Features

- HLS playlist rewriting with absolute proxy URLs
- DASH manifest generation from upstream HLS sources
- Signed, expiring proxy tokens
- Upstream retry logic
- Short-lived in-memory caching for playlists, segments, and keys
- CORS-enabled responses for browser playback
- Appwrite Functions support
- Cloudflare Workers support

## Requirements

- Python 3.11 or newer for local development
- `pip` and virtual environment support
- Appwrite CLI for Appwrite deployment
- Wrangler for Cloudflare deployment

## Local development

```bash
cd /home/toontamilindia/Projects/sliv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PROXY_SIGNING_KEY="replace-with-a-long-random-secret"
python3 main.py
```

The Flask server listens on `http://0.0.0.0:5000`.

## Environment variables

### `PROXY_SIGNING_KEY`

Required for production deployments. This key signs proxy tokens and must be identical for all requests handled by the same deployment.

If omitted during local development, the code falls back to a default value. Do not rely on the default in production.

### `APPWRITE_FUNCTION_BASE_URL`

Optional for Appwrite deployments. Set this to the public function URL so rewritten playlist URLs use the correct absolute origin.

## Appwrite deployment

Detailed Appwrite notes are in [Appwrite/README.md](Appwrite/README.md).

### Prerequisites

- An Appwrite project
- The Appwrite CLI installed and authenticated
- A Python function runtime supported by your Appwrite installation

### CLI setup

```bash
npm install -g appwrite-cli
appwrite login
cd /home/toontamilindia/Projects/sliv
appwrite init project
```

### Function structure

The Appwrite function is organized around [Appwrite/src/main.py](Appwrite/src/main.py), with [Appwrite/main.py](Appwrite/main.py) kept as a compatibility entrypoint. Both paths use the same request-to-response adapter and reuse the shared Flask application defined at the repository root.

### Deploy

1. Configure the function in the Appwrite console or via the CLI.
2. Set the entrypoint to `Appwrite/src/main.py`.
3. Install dependencies with `pip install -r Appwrite/requirements.txt`.
4. Set `PROXY_SIGNING_KEY` and, if needed, `APPWRITE_FUNCTION_BASE_URL`.
5. Push the function:

```bash
appwrite push function
```

## Cloudflare Workers deployment

Detailed Worker notes are in [cloudflare/README.md](cloudflare/README.md).

### Install Wrangler

```bash
npm install -g wrangler
wrangler login
```

### Configure `wrangler.toml`

The Worker entrypoint is [cloudflare/worker.js](cloudflare/worker.js). Review [cloudflare/wrangler.toml](cloudflare/wrangler.toml) before deploying and replace any development-only signing values with a secret.

### Deploy

```bash
cd /home/toontamilindia/Projects/sliv/cloudflare
wrangler secret put PROXY_SIGNING_KEY
wrangler deploy
```

### Route configuration

- Use `workers_dev = true` for the default `workers.dev` subdomain.
- For a custom domain, add a route in Cloudflare or define a `routes` entry in `wrangler.toml`.

## Usage

### API endpoints

- `GET /`
- `GET /playlist.m3u8?id=<id>[&bandwidth=low|high]`
- `GET /playlist.mpd?id=<id>[&bandwidth=low|high]`
- `GET /stream/<token>`
- `GET /dash/<token>`

### Example calls

```bash
curl -i "http://127.0.0.1:5000/"
curl -i "http://127.0.0.1:5000/playlist.m3u8?id=example"
curl -i "http://127.0.0.1:5000/playlist.mpd?id=example&bandwidth=high"
```

### Fetching streams

Use the playlist URL returned by the proxy in your player or downloader. The rewritten playlist already points segment requests back to the proxy, so the client does not need to know the upstream source URLs.

### Headers

The proxy sets these response headers on streamable output:

- `Access-Control-Allow-Origin: *`
- `Cache-Control: no-store`
- `X-Proxy-Status: ok`
- `X-Proxy-Playlist: 1` when a playlist is rewritten
- `X-Proxy-MPD: 1` when a DASH manifest is synthesized
- `X-Proxy-Stream: 1` on proxied media responses

Upstream requests include a source-specific `Referer` header. Clients usually do not need to set a referer when calling the proxy itself.

## Troubleshooting

### CORS errors

- Make sure the player uses the proxy URL, not the upstream URL.
- Confirm the response includes `Access-Control-Allow-Origin: *`.

### HTTP 403 from upstream

- Verify the upstream source template and referer in `UPSTREAM_SOURCES`.
- Some upstream providers block requests without the expected referer or user agent.

### Stream does not load

- Confirm `PROXY_SIGNING_KEY` is set consistently in the running environment.
- Check that the requested `id` is valid for at least one configured upstream source.
- Inspect the rewritten playlist output to ensure segment URLs were replaced with `/stream/<token>` or `/dash/<token>`.

### DASH manifest issues

- The upstream playlist must be a playable HLS playlist.
- If no variants are found, the source may be returning an empty or unsupported playlist.
- For encrypted DASH segments, make sure the upstream key URL is reachable from the deployment.

### Debug tips

- Review application logs for upstream fetch failures and cache hits.
- Use `curl -i` to inspect headers and status codes.
- In Appwrite, check the function logs for request transformation failures.
- In Cloudflare, use `wrangler tail` to watch live Worker logs.

## Demo sites

These links are provided for demonstration only. If you want to deploy your own copy, follow the Appwrite or Cloudflare deployment instructions above.

- Appwrite demo: https://69cf53fb0006fe84bb89.fra.appwrite.run/
- Cloudflare Workers demo: https://sliv.toontamilindia.workers.dev/
