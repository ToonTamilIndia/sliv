# Cloudflare Workers deployment

This directory contains the native Cloudflare Workers implementation of the proxy.

The Worker follows the same public contract as the Python service, but it runs entirely at the edge:

- incoming requests are handled by `fetch`
- upstream playlists are fetched with retry and short-lived caching
- playlists are rewritten to signed proxy URLs
- DASH manifests are synthesized from upstream HLS playlists
- encrypted DASH segments are decrypted with AES-CBC when key metadata is available

## Worker architecture

### Request rewriting

The Worker inspects the request path and routes it to one of the following handlers:

- `/` for a JSON status response
- `/playlist.m3u8?id=<id>` for rewritten HLS playlists
- `/playlist.mpd?id=<id>` for synthesized DASH manifests
- `/stream/<token>` for proxied media resources
- `/dash/<token>` for proxied DASH resources

The implementation uses the request URL and the `fetch` API to perform upstream requests. This avoids any dependency on an origin server or runtime-specific framework.

### Streaming and proxy handling

When a playlist is returned, every media URI and key URI is rewritten to a signed local proxy URL. The token stores the upstream target, the referer to use, and any DASH decryption metadata.

For media requests, the Worker fetches the original resource, forwards the expected referer, and returns the upstream bytes directly. For DASH requests, it can also decrypt AES-CBC segments when the upstream playlist exposes a key URL and IV.

### Cache strategy

The Worker keeps a small in-memory cache for:

- playlists
- media segments
- key responses

The cache is intentionally short-lived so the Worker stays responsive without retaining stale manifests for long periods.

## Configuration

The Worker is configured by [cloudflare/wrangler.toml](wrangler.toml).

Current settings:

- `name = "sliv"`
- `main = "worker.js"`
- `compatibility_date = "2026-04-03"`
- `workers_dev = true`

For production, set `PROXY_SIGNING_KEY` as a secret rather than leaving it as a plaintext variable.

## Environment bindings

Required binding:

- `PROXY_SIGNING_KEY`: HMAC key used to sign and validate proxy tokens

No additional bindings are required for the current implementation.

## Deployment with Wrangler

### 1. Install Wrangler

```bash
npm install -g wrangler
wrangler login
```

### 2. Review configuration

Confirm that [cloudflare/wrangler.toml](wrangler.toml) points to [cloudflare/worker.js](worker.js) and that the compatibility date is appropriate for your deployment.

### 3. Set the signing secret

```bash
cd /home/toontamilindia/Projects/sliv/cloudflare
wrangler secret put PROXY_SIGNING_KEY
```

### 4. Deploy

```bash
cd /home/toontamilindia/Projects/sliv/cloudflare
wrangler deploy
```

## Route configuration

- Keep `workers_dev = true` if you want the default `workers.dev` hostname.
- Add a custom domain route in the Cloudflare dashboard if you want production traffic on your own hostname.
- If you prefer explicit routing in Wrangler, add a `routes` entry to [cloudflare/wrangler.toml](wrangler.toml).

## Example requests

```bash
curl -i "https://<worker-subdomain>.workers.dev/"
curl -i "https://<worker-subdomain>.workers.dev/playlist.m3u8?id=example"
curl -i "https://<worker-subdomain>.workers.dev/playlist.mpd?id=example&bandwidth=high"
```

## Operational notes

- Use `wrangler tail` to inspect live logs.
- Verify that the Worker secret matches the signing key used to generate proxy URLs.
- If a playlist response is not rewritten, inspect the upstream content type and confirm that the source returned a valid M3U8 payload.
- If a stream fails only in the Worker deployment, compare the upstream referer and request headers with the local Python implementation.

## Validation checklist

- The root route returns a JSON status response.
- The M3U8 endpoint rewrites media URLs to Worker `/stream/<token>` URLs.
- The MPD endpoint returns a synthesized DASH manifest.
- `/dash/<token>` decrypts AES-CBC payloads when key metadata is present.
