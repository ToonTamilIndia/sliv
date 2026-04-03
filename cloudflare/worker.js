const UPSTREAM_SOURCES = [
  {
    template: "https://sliv.tulnit.fun/stream.php?id={id}&e=.m3u8",
    referer: "https://tulnit.com/",
  },
  {
    template: "https://playyonogames.in/sliv/stream.php?id={id}&e=.m3u8",
    referer: "https://playyonogames.in/",
  },
];

const TOKEN_TTL_SECONDS = 24 * 60 * 60;
const CACHE_TTL_PLAYLIST = 2;
const CACHE_TTL_SEGMENT = 30;
const CACHE_TTL_KEY = 300;
const CACHE_TTL_DEFAULT = 10;

const cacheStore = new Map();

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function cleanupCache() {
  const now = Date.now();
  for (const [key, value] of cacheStore.entries()) {
    if (now - value.createdAt > value.ttl * 1000) {
      cacheStore.delete(key);
    }
  }
}

function cacheKey(url, referer) {
  return `${referer || ""}\n${url}`;
}

function cacheGet(url, referer) {
  cleanupCache();
  return cacheStore.get(cacheKey(url, referer)) || null;
}

function cacheSet(url, referer, body, headers, finalUrl, ttl) {
  cacheStore.set(cacheKey(url, referer), {
    body,
    headers,
    finalUrl,
    ttl,
    createdAt: Date.now(),
  });
}

function cacheTtlFor(url, headers) {
  const lowered = url.toLowerCase();
  if (lowered.includes("key=")) return CACHE_TTL_KEY;
  if (lowered.includes("segment=") || lowered.endsWith(".ts") || lowered.endsWith(".m4s")) {
    return CACHE_TTL_SEGMENT;
  }
  const contentType = (headers["content-type"] || "").toLowerCase();
  if (lowered.endsWith(".m3u8") || contentType.includes("mpegurl")) return CACHE_TTL_PLAYLIST;
  return CACHE_TTL_DEFAULT;
}

function buildHeaders(referer, extra = {}) {
  const headers = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    Accept: "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
  };
  if (referer) headers.Referer = referer;
  return { ...headers, ...extra };
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol && parsed.host) return parsed.toString();
  } catch (_err) {
    // continue
  }

  if (url.startsWith("//")) return `https:${url}`;
  if (!url.includes("://")) return `https://${url.replace(/^\/+/, "")}`;
  return url;
}

function toBase64Url(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function fromBase64Url(text) {
  const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
  const base64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

function hex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(value) {
  if (!value || value.length % 2 !== 0) return null;
  const out = new Uint8Array(value.length / 2);
  for (let i = 0; i < value.length; i += 2) {
    const chunk = value.slice(i, i + 2);
    const parsed = Number.parseInt(chunk, 16);
    if (Number.isNaN(parsed)) return null;
    out[i / 2] = parsed;
  }
  return out;
}

function uint32ToBigEndian16(value) {
  const out = new Uint8Array(16);
  const n = BigInt(value);
  for (let i = 15; i >= 0; i -= 1) {
    out[i] = Number((n >> BigInt((15 - i) * 8)) & BigInt(0xff));
  }
  return out;
}

async function signPayload(payloadBytes, signingKey) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(signingKey), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", key, payloadBytes);
  return new Uint8Array(signature).slice(0, 12);
}

function concatBytes(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

async function encodeTarget(url, referer, signingKey, extras = null) {
  const payload = {
    u: url,
    r: referer,
    e: nowSeconds() + TOKEN_TTL_SECONDS,
  };
  if (extras) Object.assign(payload, extras);
  const payloadBytes = new TextEncoder().encode(JSON.stringify(payload));
  const signature = await signPayload(payloadBytes, signingKey);
  return toBase64Url(concatBytes(signature, payloadBytes));
}

async function decodeTarget(token, signingKey) {
  try {
    const packed = fromBase64Url(token);
    if (packed.length <= 12) return null;
    const signature = packed.slice(0, 12);
    const payloadBytes = packed.slice(12);
    const expected = await signPayload(payloadBytes, signingKey);
    if (signature.length !== expected.length) return null;
    for (let i = 0; i < signature.length; i += 1) {
      if (signature[i] !== expected[i]) return null;
    }

    const payload = JSON.parse(new TextDecoder().decode(payloadBytes));
    const expiresAt = Number.parseInt(payload.e || "0", 10);
    if (expiresAt < nowSeconds()) return null;

    return {
      url: payload.u,
      referer: payload.r,
      key_url: payload.k,
      iv_hex: payload.iv,
    };
  } catch (_err) {
    return null;
  }
}

function safeJoin(baseUrl, value) {
  try {
    return new URL(value, baseUrl).toString();
  } catch (_err) {
    if (value.startsWith("http://") || value.startsWith("https://")) return value;
    if (value.startsWith("//")) return `https:${value}`;
    const base = baseUrl.split("?", 1)[0].replace(/\/[^/]*$/, "");
    return `${base}/${value.replace(/^\/+/, "")}`;
  }
}

async function proxyUrl(url, referer, requestUrl, signingKey, routePath = "stream", extras = null) {
  const token = await encodeTarget(url, referer, signingKey, extras);
  const base = new URL(requestUrl);
  return `${base.origin}/${routePath}/${token}`;
}

const URI_ATTR_RE = /URI="([^"]+)"/g;

async function rewriteM3u8(text, baseUrl, referer, requestUrl, signingKey, routePath = "stream") {
  const rewritten = [];
  const lines = text.split(/\r?\n/);

  for (const line of lines) {
    if (line.startsWith("#")) {
      if (line.includes('URI="')) {
        let output = "";
        let last = 0;
        URI_ATTR_RE.lastIndex = 0;
        let match;
        while ((match = URI_ATTR_RE.exec(line)) !== null) {
          output += line.slice(last, match.index);
          const target = safeJoin(baseUrl, match[1]);
          const proxied = await proxyUrl(target, referer, requestUrl, signingKey, routePath);
          output += `URI="${proxied}"`;
          last = match.index + match[0].length;
        }
        output += line.slice(last);
        rewritten.push(output);
      } else {
        rewritten.push(line);
      }
      continue;
    }

    if (line.trim()) {
      const target = safeJoin(baseUrl, line.trim());
      rewritten.push(await proxyUrl(target, referer, requestUrl, signingKey, routePath));
    } else {
      rewritten.push(line);
    }
  }

  return `${rewritten.join("\n")}\n`;
}

function isM3u8Payload(bodyBytes, finalUrl) {
  const bodyText = new TextDecoder().decode(bodyBytes.slice(0, Math.min(bodyBytes.length, 1024)));
  const trimmed = bodyText.trimStart();
  if (trimmed.startsWith("#EXTM3U")) return true;
  if (trimmed.startsWith("#") && trimmed.includes("#EXT")) return true;

  try {
    const url = new URL(finalUrl);
    if (url.searchParams.get("e") === ".m3u8") return true;
  } catch (_err) {
    // continue
  }

  return false;
}

function isMasterPlaylist(text) {
  return text.includes("#EXT-X-STREAM-INF:");
}

function parseAttrList(line) {
  const attrs = {};
  const payload = line.includes(":") ? line.split(":", 2)[1] : "";
  const re = /([A-Z0-9-]+)=(("[^"]*")|[^,]*)/g;
  let match;
  while ((match = re.exec(payload)) !== null) {
    const key = match[1];
    const raw = match[2];
    attrs[key] = raw.startsWith('"') && raw.endsWith('"') ? raw.slice(1, -1) : raw;
  }
  return attrs;
}

function parseMasterPlaylist(masterText) {
  const lines = masterText.split(/\r?\n/).map((line) => line.trim());
  const variants = [];
  const audios = [];

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.startsWith("#EXT-X-MEDIA:")) {
      const attrs = parseAttrList(line);
      if ((attrs.TYPE || "").toUpperCase() === "AUDIO") audios.push(attrs);
      continue;
    }

    if (line.startsWith("#EXT-X-STREAM-INF:")) {
      const attrs = parseAttrList(line);
      let j = i + 1;
      while (j < lines.length && (!lines[j] || lines[j].startsWith("#"))) j += 1;
      if (j < lines.length) {
        const bw = Number.parseInt(attrs.BANDWIDTH || "0", 10);
        variants.push({
          uri: lines[j],
          attrs,
          bandwidth: Number.isNaN(bw) ? 0 : bw,
        });
      }
      i = j;
    }
  }

  return { variants, audios };
}

function selectVariants(variants, bandwidthMode) {
  if (!variants.length) return [];
  if (bandwidthMode === "low") {
    const min = Math.min(...variants.map((v) => v.bandwidth));
    return variants.filter((v) => v.bandwidth === min);
  }
  if (bandwidthMode === "high") {
    const max = Math.max(...variants.map((v) => v.bandwidth));
    return variants.filter((v) => v.bandwidth === max);
  }
  return variants;
}

function filterMasterPlaylist(masterText, selectedVariants) {
  const allowedUris = new Set(selectedVariants.map((v) => v.uri));
  const allowedAudioGroups = new Set(selectedVariants.map((v) => v.attrs.AUDIO).filter(Boolean));
  const lines = masterText.split(/\r?\n/);
  const out = [];

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const stripped = line.trim();

    if (stripped.startsWith("#EXT-X-MEDIA:")) {
      const attrs = parseAttrList(stripped);
      if ((attrs.TYPE || "").toUpperCase() === "AUDIO") {
        const groupId = attrs["GROUP-ID"] || "";
        if (allowedAudioGroups.size && !allowedAudioGroups.has(groupId)) continue;
      }
      out.push(line);
      continue;
    }

    if (stripped.startsWith("#EXT-X-STREAM-INF:")) {
      let j = i + 1;
      while (j < lines.length && (!lines[j].trim() || lines[j].trim().startsWith("#"))) j += 1;
      if (j < lines.length && allowedUris.has(lines[j].trim())) {
        out.push(line);
        out.push(lines[j]);
      }
      i = j;
      continue;
    }

    out.push(line);
  }

  return `${out.join("\n")}\n`;
}

function parseMediaSegmentsWithKeys(mediaText, baseUrl, referer, requestUrl, signingKey) {
  const lines = mediaText.split(/\r?\n/);
  let mediaSequence = 0;
  let targetDuration = 4.0;
  let pendingDuration = null;
  let currentKeyUrl = null;
  let currentIvHex = null;
  let segmentIndex = 0;
  const work = [];

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;

    if (line.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
      const value = Number.parseInt(line.split(":", 2)[1], 10);
      mediaSequence = Number.isNaN(value) ? 0 : value;
      continue;
    }

    if (line.startsWith("#EXT-X-TARGETDURATION:")) {
      const value = Number.parseFloat(line.split(":", 2)[1]);
      targetDuration = Number.isNaN(value) ? 4.0 : value;
      continue;
    }

    if (line.startsWith("#EXTINF:")) {
      const value = Number.parseFloat(line.split(":", 2)[1].split(",", 1)[0]);
      pendingDuration = Number.isNaN(value) ? targetDuration : value;
      continue;
    }

    if (line.startsWith("#EXT-X-KEY:")) {
      const attrs = parseAttrList(line);
      const method = (attrs.METHOD || "").trim().toUpperCase();

      // Only full-segment AES-128 can be transparently decrypted in proxy mode.
      // SAMPLE-AES and other methods should not trigger segment decryption here.
      if (method === "AES-128") {
        const uri = attrs.URI;
        currentKeyUrl = uri ? safeJoin(baseUrl, uri) : null;
        const iv = attrs.IV;
        if (iv && iv.toLowerCase().startsWith("0x")) {
          currentIvHex = iv.slice(2);
        } else {
          currentIvHex = iv || null;
        }
      } else {
        currentKeyUrl = null;
        currentIvHex = null;
      }
      continue;
    }

    if (line.startsWith("#")) continue;

    const duration = pendingDuration !== null ? pendingDuration : targetDuration;
    const rawSegmentUrl = safeJoin(baseUrl, line);
    let ivHex = currentIvHex;
    if (currentKeyUrl && !ivHex) {
      ivHex = hex(uint32ToBigEndian16(mediaSequence + segmentIndex));
    }

    work.push({
      rawSegmentUrl,
      duration,
      extras: currentKeyUrl ? { k: currentKeyUrl, iv: ivHex } : null,
    });

    segmentIndex += 1;
    pendingDuration = null;
  }

  return { mediaSequence, targetDuration, work, referer, requestUrl, signingKey };
}

async function finalizeSegmentWork(parsed) {
  const segments = [];
  for (const item of parsed.work) {
    const proxied = await proxyUrl(
      item.rawSegmentUrl,
      parsed.referer,
      parsed.requestUrl,
      parsed.signingKey,
      "dash",
      item.extras
    );
    segments.push({ url: proxied, duration: item.duration });
  }
  return {
    mediaSequence: parsed.mediaSequence,
    targetDuration: parsed.targetDuration,
    segments,
  };
}

function segmentListXml(mediaSequence, targetDuration, segments, timescale = 1000) {
  const timeline = [];
  for (const segment of segments) {
    const d = Math.max(1, Math.round(segment.duration * timescale));
    const last = timeline[timeline.length - 1];
    if (last && last[0] === d) {
      last[1] += 1;
    } else {
      timeline.push([d, 1]);
    }
  }

  let t = Math.round(mediaSequence * targetDuration * timescale);
  let first = true;
  const timelineXml = timeline
    .map(([d, count]) => {
      const r = count - 1;
      const tAttr = first ? ` t="${t}"` : "";
      first = false;
      t += d * count;
      return r > 0 ? `<S${tAttr} d="${d}" r="${r}"/>` : `<S${tAttr} d="${d}"/>`;
    })
    .join("");

  const segmentUrls = segments
    .map((segment) => `<SegmentURL media="${escapeXml(segment.url)}"/>`)
    .join("");

  return `<SegmentList timescale="${timescale}" startNumber="${mediaSequence}"><SegmentTimeline>${timelineXml}</SegmentTimeline>${segmentUrls}</SegmentList>`;
}

function escapeXml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function buildMpdFromReps(videoReps, audioReps, minUpdate) {
  const nowIso = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const videoXml = videoReps
    .map((rep) => {
      const codecs = rep.codecs ? ` codecs="${escapeXml(rep.codecs)}"` : "";
      return `<Representation id="${escapeXml(rep.id)}" bandwidth="${rep.bandwidth}"${codecs}>${rep.segmentListXml}</Representation>`;
    })
    .join("");

  const audioXml = audioReps
    .map((rep) => {
      const codecs = rep.codecs ? ` codecs="${escapeXml(rep.codecs)}"` : "";
      return `<Representation id="${escapeXml(rep.id)}" bandwidth="${rep.bandwidth}"${codecs}>${rep.segmentListXml}</Representation>`;
    })
    .join("");

  const audioSet = audioReps.length
    ? `<AdaptationSet id="2" mimeType="audio/mp2t" segmentAlignment="true">${audioXml}</AdaptationSet>`
    : "";

  return (
    '<?xml version="1.0" encoding="UTF-8"?>' +
    `<MPD availabilityStartTime="${nowIso}" xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" profiles="urn:mpeg:dash:profile:isoff-live:2011" ` +
    `minimumUpdatePeriod="PT${Math.max(1, minUpdate)}S" timeShiftBufferDepth="PT120S" suggestedPresentationDelay="PT8S" publishTime="${nowIso}">` +
    '<Period id="1" start="PT0S">' +
    `<AdaptationSet id="1" mimeType="video/mp2t" segmentAlignment="true">${videoXml}</AdaptationSet>` +
    `${audioSet}</Period></MPD>\n`
  );
}

function looksLikeMpegTs(payload) {
  if (!payload || payload.length < 188) return false;
  if (payload[0] !== 0x47) return false;

  const packets = Math.min(8, Math.floor(payload.length / 188));
  if (packets <= 1) return payload[0] === 0x47;

  let syncHits = 0;
  for (let i = 0; i < packets; i += 1) {
    if (payload[i * 188] === 0x47) syncHits += 1;
  }
  return syncHits >= Math.max(2, Math.floor(packets * 0.75));
}

async function fetchWithRetry(url, referer) {
  const normalized = safeUrl(url);
  const cached = cacheGet(normalized, referer);
  if (cached) return cached;

  let lastErr = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(normalized, { headers: buildHeaders(referer) });
      if (!response.ok) {
        if (response.status < 500 || attempt === 2) {
          const body = new Uint8Array(await response.arrayBuffer());
          const error = new Error(`upstream status ${response.status}`);
          error.status = response.status;
          error.body = body;
          throw error;
        }
        await sleep((attempt + 1) * 500);
        continue;
      }

      const body = new Uint8Array(await response.arrayBuffer());
      const headers = {};
      response.headers.forEach((value, key) => {
        headers[key.toLowerCase()] = value;
      });
      const finalUrl = response.url || normalized;
      const ttl = cacheTtlFor(finalUrl, headers);
      if (ttl > 0) cacheSet(normalized, referer, body, headers, finalUrl, ttl);
      return { body, headers, finalUrl };
    } catch (err) {
      lastErr = err;
      if (attempt === 2) break;
      await sleep((attempt + 1) * 500);
    }
  }
  throw lastErr || new Error("fetch failed");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function resolveSource(userId) {
  const sources = [...UPSTREAM_SOURCES];
  for (let i = sources.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [sources[i], sources[j]] = [sources[j], sources[i]];
  }

  let lastErr = null;
  for (const source of sources) {
    const upstreamUrl = source.template.replace("{id}", encodeURIComponent(userId));
    try {
      const { body, finalUrl } = await fetchWithRetry(upstreamUrl, source.referer);
      if (body && body.length) return { upstreamUrl: finalUrl, referer: source.referer };
    } catch (err) {
      lastErr = err;
    }
  }

  throw lastErr || new Error("all upstream sources returned empty body");
}

function makeResponse(body, contentType, status = 200, extraHeaders = {}) {
  const headers = new Headers(extraHeaders);
  headers.set("Content-Type", contentType);
  headers.set("Cache-Control", "no-store");
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("X-Proxy-Status", "ok");
  return new Response(body, { status, headers });
}

async function decryptDashIfNeeded(body, target, referer) {
  if (!target.key_url) return body;
  if (!body || body.length === 0 || body.length % 16 !== 0) return body;

  try {
    const { body: keyBody } = await fetchWithRetry(target.key_url, referer);
    if (keyBody.length < 16) return body;
    const keyBytes = keyBody.slice(0, 16);

    let iv = target.iv_hex ? hexToBytes(target.iv_hex) : new Uint8Array(16);
    if (!iv || iv.length !== 16) return body;

    const cryptoKey = await crypto.subtle.importKey("raw", keyBytes, { name: "AES-CBC" }, false, ["decrypt"]);
    const decrypted = new Uint8Array(await crypto.subtle.decrypt({ name: "AES-CBC", iv }, cryptoKey, body));

    const pad = decrypted[decrypted.length - 1];
    if (pad >= 1 && pad <= 16) {
      let validPad = true;
      for (let i = decrypted.length - pad; i < decrypted.length; i += 1) {
        if (decrypted[i] !== pad) {
          validPad = false;
          break;
        }
      }
      if (validPad) {
        const unpadded = decrypted.slice(0, decrypted.length - pad);
        return looksLikeMpegTs(unpadded) ? unpadded : body;
      }
    }

    return looksLikeMpegTs(decrypted) ? decrypted : body;
  } catch (_err) {
    return body;
  }
}

async function proxyTokenRequest(request, token, signingKey, routePrefix) {
  const target = await decodeTarget(token, signingKey);
  if (!target) {
    return new Response(JSON.stringify({ status: "error", message: "Invalid or expired token" }), {
      status: 404,
      headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
    });
  }

  let body;
  let headers;
  let finalUrl;
  try {
    const fetched = await fetchWithRetry(target.url, target.referer);
    body = fetched.body;
    headers = fetched.headers;
    finalUrl = fetched.finalUrl;
  } catch (err) {
    const status = err && err.status ? err.status : 502;
    const payload = err && err.body ? err.body : new TextEncoder().encode(String(err));
    return makeResponse(payload, "text/plain; charset=utf-8", status);
  }

  let contentType = headers["content-type"] || "application/octet-stream";
  if (isM3u8Payload(body, finalUrl)) {
    const playlistText = new TextDecoder().decode(body);
    const routePath = routePrefix === "dash" ? "dash" : "stream";
    const rewritten = await rewriteM3u8(playlistText, finalUrl, target.referer, request.url, signingKey, routePath);
    return makeResponse(new TextEncoder().encode(rewritten), "application/vnd.apple.mpegurl", 200, {
      "X-Proxy-Playlist": "1",
      "X-Proxy-Absolute-Urls": "1",
    });
  }

  let hadKey = false;
  if (routePrefix === "dash") {
    hadKey = Boolean(target.key_url);
    body = await decryptDashIfNeeded(body, target, target.referer);
    if (contentType === "application/octet-stream") contentType = "video/mp2t";
  }

  const response = makeResponse(body, contentType, 200, {
    "X-Proxy-Stream": "1",
  });

  if (routePrefix === "dash") {
    response.headers.set("X-Proxy-Dash-Key", hadKey ? "1" : "0");
    response.headers.set("X-Proxy-Dash-IV", target.iv_hex ? "1" : "0");
  }
  return response;
}

async function handlePlaylist(request, signingKey, format) {
  const url = new URL(request.url);
  const userId = url.searchParams.get("id");
  let bandwidthMode = (url.searchParams.get("bandwidth") || "").trim().toLowerCase();
  if (!["", "low", "high"].includes(bandwidthMode)) bandwidthMode = "";

  if (!userId) {
    if (format === "m3u8") return makeResponse(new TextEncoder().encode("#EXTM3U\n"), "application/vnd.apple.mpegurl", 400);
    return makeResponse(new Uint8Array(0), "application/dash+xml", 400);
  }

  let upstreamUrl;
  let referer;
  try {
    const source = await resolveSource(userId);
    upstreamUrl = source.upstreamUrl;
    referer = source.referer;
  } catch (err) {
    return new Response(JSON.stringify({ status: "error", message: String(err) }), {
      status: 502,
      headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
    });
  }

  let body;
  let headers;
  let finalUrl;
  try {
    const fetched = await fetchWithRetry(upstreamUrl, referer);
    body = fetched.body;
    headers = fetched.headers;
    finalUrl = fetched.finalUrl;
  } catch (err) {
    return new Response(JSON.stringify({ status: "error", message: String(err) }), {
      status: 502,
      headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
    });
  }

  if (format === "m3u8") {
    const contentType = headers["content-type"] || "application/octet-stream";
    if (isM3u8Payload(body, finalUrl)) {
      let playlistText = new TextDecoder().decode(body);
      if (isMasterPlaylist(playlistText)) {
        const { variants } = parseMasterPlaylist(playlistText);
        const selected = selectVariants(variants, bandwidthMode || null);
        if (selected.length) playlistText = filterMasterPlaylist(playlistText, selected);
      }
      const rewritten = await rewriteM3u8(playlistText, finalUrl, referer, request.url, signingKey, "stream");
      return makeResponse(new TextEncoder().encode(rewritten), "application/vnd.apple.mpegurl", 200, {
        "X-Proxy-Playlist": "1",
        "X-Proxy-Absolute-Urls": "1",
      });
    }

    return makeResponse(body, contentType);
  }

  if (!isM3u8Payload(body, finalUrl)) {
    return new Response(JSON.stringify({ status: "error", message: "Upstream did not return an m3u8 playlist" }), {
      status: 502,
      headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
    });
  }

  const masterText = new TextDecoder().decode(body);
  const videoReps = [];
  const audioReps = [];
  let minUpdate = 4;

  try {
    if (isMasterPlaylist(masterText)) {
      const { variants, audios } = parseMasterPlaylist(masterText);
      const selectedVariants = selectVariants(variants, bandwidthMode || null);

      let vIndex = 1;
      for (const variant of selectedVariants) {
        const variantUrl = safeJoin(finalUrl, variant.uri);
        const vfetched = await fetchWithRetry(variantUrl, referer);
        if (!isM3u8Payload(vfetched.body, vfetched.finalUrl)) continue;

        const text = new TextDecoder().decode(vfetched.body);
        const parsed = parseMediaSegmentsWithKeys(text, vfetched.finalUrl, referer, request.url, signingKey);
        const finalized = await finalizeSegmentWork(parsed);
        if (!finalized.segments.length) continue;

        minUpdate = Math.min(minUpdate, Math.max(1, Math.round(finalized.targetDuration)));
        videoReps.push({
          id: `v${vIndex}`,
          bandwidth: variant.bandwidth || 1000000,
          codecs: variant.attrs.CODECS || null,
          segmentListXml: segmentListXml(finalized.mediaSequence, finalized.targetDuration, finalized.segments),
        });
        vIndex += 1;
      }

      const allowedGroups = new Set(selectedVariants.map((v) => v.attrs.AUDIO).filter(Boolean));
      const selectedAudios = audios.filter((a) => a.URI && (!allowedGroups.size || allowedGroups.has(a["GROUP-ID"] || "")));

      let aIndex = 1;
      for (const audio of selectedAudios) {
        const audioUrl = safeJoin(finalUrl, audio.URI);
        const afetched = await fetchWithRetry(audioUrl, referer);
        if (!isM3u8Payload(afetched.body, afetched.finalUrl)) continue;

        const text = new TextDecoder().decode(afetched.body);
        const parsed = parseMediaSegmentsWithKeys(text, afetched.finalUrl, referer, request.url, signingKey);
        const finalized = await finalizeSegmentWork(parsed);
        if (!finalized.segments.length) continue;

        minUpdate = Math.min(minUpdate, Math.max(1, Math.round(finalized.targetDuration)));
        audioReps.push({
          id: `a${aIndex}`,
          bandwidth: 128000,
          codecs: null,
          segmentListXml: segmentListXml(finalized.mediaSequence, finalized.targetDuration, finalized.segments),
        });
        aIndex += 1;
      }
    } else {
      const parsed = parseMediaSegmentsWithKeys(masterText, finalUrl, referer, request.url, signingKey);
      const finalized = await finalizeSegmentWork(parsed);
      if (finalized.segments.length) {
        minUpdate = Math.max(1, Math.round(finalized.targetDuration));
        videoReps.push({
          id: "v1",
          bandwidth: 1000000,
          codecs: null,
          segmentListXml: segmentListXml(finalized.mediaSequence, finalized.targetDuration, finalized.segments),
        });
      }
    }

    if (!videoReps.length) throw new Error("No playable variants found");

    const mpd = buildMpdFromReps(videoReps, audioReps, minUpdate);
    return makeResponse(new TextEncoder().encode(mpd), "application/dash+xml", 200, {
      "X-Proxy-MPD": "1",
    });
  } catch (err) {
    return new Response(JSON.stringify({ status: "error", message: String(err) }), {
      status: 502,
      headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
    });
  }
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
    },
  });
}

function notFound() {
  return jsonResponse({ status: "error", message: "Not found" }, 404);
}

function methodNotAllowed() {
  return jsonResponse({ status: "error", message: "Method not allowed" }, 405);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const pathname = url.pathname;
    const signingKey = env.PROXY_SIGNING_KEY || "change-this-secret";

    if (request.method !== "GET") return methodNotAllowed();

    if (pathname === "/") {
      return jsonResponse({ status: "ok", usage: "/playlist.m3u8?id=<id>", dash: "/playlist.mpd?id=<id>" });
    }

    if (pathname === "/playlist.m3u8") {
      return handlePlaylist(request, signingKey, "m3u8");
    }

    if (pathname === "/playlist.mpd") {
      return handlePlaylist(request, signingKey, "mpd");
    }

    if (pathname.startsWith("/stream/")) {
      const token = pathname.slice("/stream/".length);
      if (!token) return notFound();
      return proxyTokenRequest(request, token, signingKey, "stream");
    }

    if (pathname.startsWith("/dash/")) {
      const token = pathname.slice("/dash/".length);
      if (!token) return notFound();
      return proxyTokenRequest(request, token, signingKey, "dash");
    }

    return notFound();
  },
};
