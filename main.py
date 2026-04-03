from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape
import zlib as zlib_codec
from typing import Any
from Crypto.Cipher import AES

from flask import Flask, jsonify, make_response, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")


UPSTREAM_SOURCES = [
    {
        "template": "https://sliv.tulnit.fun/stream.php?id={id}&e=.m3u8",
        "referer": "https://tulnit.com/",
    },
    {
        "template": "https://playyonogames.in/sliv/stream.php?id={id}&e=.m3u8",
        "referer": "https://playyonogames.in/",
    },
    {
        "template": "https://mhdtvhub.com/sliv/stream.php?id={id}&e=.m3u8",
        "referer": "https://mhdtvhub.com/",
    },
]

TOKEN_TTL_SECONDS = 24 * 60 * 60
SIGNING_KEY = os.getenv("PROXY_SIGNING_KEY", "change-this-secret").encode("utf-8")

CACHE_STORE: dict[str, dict[str, Any]] = {}
CACHE_LOCK = threading.Lock()
CACHE_TTL_PLAYLIST = 2
CACHE_TTL_SEGMENT = 30
CACHE_TTL_KEY = 300
CACHE_TTL_DEFAULT = 10
CACHE_MAX_ENTRIES = 2000

def _cleanup_cache() -> None:
    with CACHE_LOCK:
        now = time.time()
        stale = [key for key, data in CACHE_STORE.items() if now - data["created_at"] > data["ttl"]]
        for key in stale:
            CACHE_STORE.pop(key, None)


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def _encode_target(url: str, referer: str | None, extras: dict[str, Any] | None = None) -> str:
    payload = {
        "u": url,
        "r": referer,
        "e": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    if extras:
        payload.update(extras)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    compressed = zlib_codec.compress(raw, level=9)
    signature = hmac.new(SIGNING_KEY, compressed, hashlib.sha256).digest()[:12]
    return _b64u_encode(signature + compressed)


def _decode_target(token: str) -> dict[str, Any] | None:
    try:
        packed = _b64u_decode(token)
        if len(packed) <= 12:
            return None
        signature = packed[:12]
        compressed = packed[12:]
        expected = hmac.new(SIGNING_KEY, compressed, hashlib.sha256).digest()[:12]
        if not hmac.compare_digest(signature, expected):
            return None

        payload = json.loads(zlib_codec.decompress(compressed).decode("utf-8"))
        expires_at = int(payload.get("e", 0))
        if expires_at < int(time.time()):
            return None

        return {
            "url": payload.get("u"),
            "referer": payload.get("r"),
            "key_url": payload.get("k"),
            "iv_hex": payload.get("iv"),
        }
    except Exception:  # noqa: BLE001
        return None


def _cache_key(url: str, referer: str | None) -> str:
    return hashlib.sha256(f"{referer or ''}\x00{url}".encode("utf-8")).hexdigest()


def _cache_get(url: str, referer: str | None) -> tuple[bytes, dict[str, str], str] | None:
    _cleanup_cache()
    key = _cache_key(url, referer)
    with CACHE_LOCK:
        cached = CACHE_STORE.get(key)
        if not cached:
            return None
        return cached["body"], cached["headers"], cached["final_url"]


def _cache_set(url: str, referer: str | None, body: bytes, headers: dict[str, str], final_url: str, ttl: int) -> None:
    key = _cache_key(url, referer)
    with CACHE_LOCK:
        CACHE_STORE[key] = {
            "body": body,
            "headers": headers,
            "final_url": final_url,
            "ttl": ttl,
            "created_at": time.time(),
        }

        while len(CACHE_STORE) > CACHE_MAX_ENTRIES:
            oldest_key = next(iter(CACHE_STORE), None)
            if oldest_key is None:
                break
            CACHE_STORE.pop(oldest_key, None)


def _cache_ttl_for(url: str, headers: dict[str, str] | None = None) -> int:
    lowered = url.lower()
    if "key=" in lowered:
        return CACHE_TTL_KEY
    if "segment=" in lowered or lowered.endswith(".ts") or lowered.endswith(".m4s"):
        return CACHE_TTL_SEGMENT
    if lowered.endswith(".m3u8") or (headers and "mpegurl" in headers.get("content-type", "")):
        return CACHE_TTL_PLAYLIST
    return CACHE_TTL_DEFAULT


def _build_headers(referer: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def _fetch(url: str, referer: str | None = None) -> tuple[bytes, dict[str, str], str]:
    url = _safe_url(url)
    cached = _cache_get(url, referer)
    if cached:
        logging.info("cache hit: %s", url)
        return cached

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request_obj = urllib.request.Request(url, headers=_build_headers(referer))
            with urllib.request.urlopen(request_obj, timeout=20) as response:
                body = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                final_url = response.geturl()

            encoding = headers.get("content-encoding", "").lower()
            if encoding == "gzip":
                body = gzip.decompress(body)
                headers.pop("content-encoding", None)
                headers.pop("content-length", None)
            elif encoding == "deflate":
                decompressed = False
                try:
                    body = zlib_codec.decompress(body)
                    decompressed = True
                except Exception:
                    try:
                        body = zlib_codec.decompress(body, -zlib_codec.MAX_WBITS)
                        decompressed = True
                    except Exception:
                        logging.warning("deflate decode failed, forwarding raw body: %s", final_url)
                if decompressed:
                    headers.pop("content-encoding", None)
                    headers.pop("content-length", None)

            ttl = _cache_ttl_for(final_url, headers)
            if ttl > 0:
                _cache_set(url, referer, body, headers, final_url, ttl)
            logging.info("fetch ok: %s -> %s", url, final_url)
            return body, headers, final_url
        except urllib.error.HTTPError as exc:
            last_error = exc
            status = getattr(exc, "code", 0) or 0
            if status < 500 or attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == 2:
                break
            time.sleep(0.5 * (attempt + 1))

    if last_error:
        raise last_error
    raise RuntimeError("fetch failed")


def _safe_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if "://" not in url:
        return f"https://{url.lstrip('/')}"
    stripped = url.split("://", 1)[1].lstrip("/") if "://" in url else url.lstrip("/")
    return f"https://{stripped}"


def _resolve_source(user_id: str) -> tuple[str, str]:
    if not UPSTREAM_SOURCES:
        raise RuntimeError("No upstream source configured")

    last_error: Exception | None = None
    sources = UPSTREAM_SOURCES[:]
    random.shuffle(sources)

    for source in sources:
        upstream_url = source["template"].format(id=urllib.parse.quote(user_id, safe=""))
        try:
            body, _, final_url = _fetch(upstream_url, source["referer"])
            if body:
                return final_url, source["referer"]
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if last_error:
        raise last_error
    raise RuntimeError("All upstream sources returned empty body")


def _proxy_url(url: str, referer: str | None, route_path: str = "stream", extras: dict[str, Any] | None = None) -> str:
    token = _encode_target(url, referer, extras=extras)
    try:
        return urllib.parse.urljoin(request.host_url, f"{route_path}/{token}")
    except Exception:  # noqa: BLE001
        return f"/{route_path}/{token}"


_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


def _safe_join(base_url: str, value: str) -> str:
    try:
        return urllib.parse.urljoin(base_url, value)
    except ValueError:
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("//"):
            return f"https:{value}"
        base = base_url.split("?", 1)[0].rsplit("/", 1)[0]
        return f"{base}/{value.lstrip('/')}"


def _rewrite_m3u8(text: str, base_url: str, referer: str | None, route_path: str = "stream") -> str:
    base_url = _safe_url(base_url)
    rewritten_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if "URI=\"" in line:
                def replace_uri(match: re.Match[str]) -> str:
                    target = _safe_join(base_url, match.group(1))
                    return f'URI="{_proxy_url(target, referer, route_path=route_path)}"'

                line = _URI_ATTR_RE.sub(replace_uri, line)
            rewritten_lines.append(line)
            continue

        if line.strip():
            target = _safe_join(base_url, line.strip())
            rewritten_lines.append(_proxy_url(target, referer, route_path=route_path))
        else:
            rewritten_lines.append(line)
    return "\n".join(rewritten_lines) + "\n"


def _is_m3u8_payload(body: bytes, final_url: str) -> bool:
    stripped = body.lstrip()
    if stripped.startswith(b"#EXTM3U"):
        return True

    # Some upstreams use `.m3u8` path even for binary segment responses.
    # Do not trust path suffix alone.
    if stripped.startswith(b"#") and b"#EXT" in stripped[:1024]:
        return True

    parsed = urllib.parse.urlparse(final_url)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("e", [""])[0] == ".m3u8":
        return True
    return False


def _is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF:" in text


def _parse_attr_list(line: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    payload = line.split(":", 1)[1] if ":" in line else ""
    for match in re.finditer(r'([A-Z0-9-]+)=(("[^"]*")|[^,]*)', payload):
        key = match.group(1)
        raw_value = match.group(2)
        value = raw_value[1:-1] if raw_value.startswith('"') and raw_value.endswith('"') else raw_value
        attrs[key] = value
    return attrs


def _pick_variant_from_master(master_text: str) -> tuple[str, dict[str, str]] | None:
    lines = [line.strip() for line in master_text.splitlines()]
    variants: list[tuple[int, str, dict[str, str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attr_list(line)
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                uri = lines[j]
                try:
                    bw = int(attrs.get("BANDWIDTH", "0"))
                except ValueError:
                    bw = 1000000
                if bw <= 0:
                    bw = 1000000
                variants.append((bw, uri, attrs))
            i = j
            continue
        i += 1

    if not variants:
        return None

    variants.sort(key=lambda item: item[0], reverse=True)
    best = variants[0]
    return best[1], best[2]


def _parse_master_playlist(master_text: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    lines = [line.strip() for line in master_text.splitlines()]
    variants: list[dict[str, Any]] = []
    audios: list[dict[str, str]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attr_list(line)
            if attrs.get("TYPE", "").upper() == "AUDIO":
                audios.append(attrs)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attr_list(line)
            j = i + 1
            while j < len(lines) and (not lines[j] or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                uri = lines[j]
                try:
                    bw = int(attrs.get("BANDWIDTH", "0"))
                except ValueError:
                    bw = 1000000
                if bw <= 0:
                    bw = 1000000
                variants.append({"uri": uri, "attrs": attrs, "bandwidth": bw})
            i = j
        i += 1

    return variants, audios


def _select_variants(variants: list[dict[str, Any]], bandwidth_mode: str | None) -> list[dict[str, Any]]:
    if not variants:
        return []
    if bandwidth_mode == "low":
        min_bw = min(v["bandwidth"] for v in variants)
        return [v for v in variants if v["bandwidth"] == min_bw]
    if bandwidth_mode == "high":
        max_bw = max(v["bandwidth"] for v in variants)
        return [v for v in variants if v["bandwidth"] == max_bw]
    return variants


def _filter_master_playlist(master_text: str, selected_variants: list[dict[str, Any]]) -> str:
    allowed_uris = {v["uri"] for v in selected_variants}
    allowed_audio_groups = {v["attrs"].get("AUDIO", "") for v in selected_variants if v["attrs"].get("AUDIO")}
    lines = master_text.splitlines()
    out: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attr_list(stripped)
            if attrs.get("TYPE", "").upper() == "AUDIO":
                group_id = attrs.get("GROUP-ID", "")
                if allowed_audio_groups and group_id not in allowed_audio_groups:
                    i += 1
                    continue
            out.append(line)
            i += 1
            continue

        if stripped.startswith("#EXT-X-STREAM-INF:"):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines) and lines[j].strip() in allowed_uris:
                out.extend(lines[i : j + 1])
            i = j + 1
            continue

        out.append(line)
        i += 1

    return "\n".join(out) + "\n"


def _media_m3u8_to_mpd(media_text: str, bandwidth: int = 1000000, codecs: str | None = None) -> str:
    target_duration = 4.0
    media_sequence = 0
    pending_duration: float | None = None
    segments: list[tuple[str, float]] = []

    for raw_line in media_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except Exception:
                target_duration = 4.0
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except Exception:
                media_sequence = 0
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except Exception:
                pending_duration = target_duration
            continue
        if line.startswith("#"):
            continue

        duration = pending_duration if pending_duration is not None else target_duration
        segments.append((line, duration))
        pending_duration = None

    if not segments:
        raise RuntimeError("No media segments found in m3u8")

    timescale = 1000
    timeline: list[tuple[int, int]] = []
    for _, dur in segments:
        d = max(1, int(round(dur * timescale)))
        if timeline and timeline[-1][0] == d:
            timeline[-1] = (d, timeline[-1][1] + 1)
        else:
            timeline.append((d, 1))

    timeline_xml = []
    for d, count in timeline:
        r = count - 1
        if r > 0:
            timeline_xml.append(f'<S d="{d}" r="{r}"/>')
        else:
            timeline_xml.append(f'<S d="{d}"/>')

    segment_urls_xml = [f'<SegmentURL media="{escape(url)}"/>' for url, _ in segments]

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    min_update = max(1, int(math.ceil(target_duration)))
    codec_attr = f' codecs="{escape(codecs)}"' if codecs else ""

    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        f"<MPD availabilityStartTime=\"{now_iso}\" xmlns=\"urn:mpeg:dash:schema:mpd:2011\" "
        "type=\"dynamic\" "
        "profiles=\"urn:mpeg:dash:profile:isoff-live:2011\" "
        f"minimumUpdatePeriod=\"PT{min_update}S\" "
        "timeShiftBufferDepth=\"PT120S\" "
        "suggestedPresentationDelay=\"PT8S\" "
        f"publishTime=\"{now_iso}\">\n"
        "  <Period id=\"1\" start=\"PT0S\">\n"
        "    <AdaptationSet id=\"1\" mimeType=\"video/mp2t\" segmentAlignment=\"true\">\n"
        f"      <Representation id=\"v1\" bandwidth=\"{bandwidth}\"{codec_attr}>\n"
        f"        <SegmentList timescale=\"{timescale}\" startNumber=\"{media_sequence}\">\n"
        "          <SegmentTimeline>\n"
        f"            {' '.join(timeline_xml)}\n"
        "          </SegmentTimeline>\n"
        f"          {' '.join(segment_urls_xml)}\n"
        "        </SegmentList>\n"
        "      </Representation>\n"
        "    </AdaptationSet>\n"
        "  </Period>\n"
        "</MPD>\n"
    )


def _parse_media_segments(media_text: str) -> tuple[int, float, list[tuple[str, float]]]:
    target_duration = 4.0
    media_sequence = 0
    pending_duration: float | None = None
    segments: list[tuple[str, float]] = []

    for raw_line in media_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except Exception:
                target_duration = 4.0
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except Exception:
                media_sequence = 0
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except Exception:
                pending_duration = target_duration
            continue
        if line.startswith("#"):
            continue
        duration = pending_duration if pending_duration is not None else target_duration
        segments.append((line, duration))
        pending_duration = None

    return media_sequence, target_duration, segments


def _segment_list_xml(media_sequence: int, target_duration: float, segments: list[tuple[str, float]], timescale: int = 1000) -> str:
    if not segments:
        raise ValueError("Cannot generate SegmentList with zero segments")

    timeline: list[tuple[int, int]] = []
    for segment in segments:
        if isinstance(segment, tuple):
            _, dur = segment
        else:
            dur = float(segment.get("duration", 4.0))
        d = max(1, int(round(dur * timescale)))
        if timeline and timeline[-1][0] == d:
            timeline[-1] = (d, timeline[-1][1] + 1)
        else:
            timeline.append((d, 1))

    timeline_xml = []
    t = int(round(media_sequence * target_duration * timescale))
    first = True
    for d, count in timeline:
        r = count - 1
        t_attr = f' t="{t}"' if first else ""
        first = False
        timeline_xml.append(f'<S{t_attr} d="{d}" r="{r}"/>' if r > 0 else f'<S{t_attr} d="{d}"/>')
        t += d * count

    segment_urls_xml: list[str] = []
    for segment in segments:
        if isinstance(segment, tuple):
            url = segment[0]
        elif isinstance(segment, dict):
            url = str(segment.get("url", ""))
        else:
            continue
        segment_urls_xml.append(f'<SegmentURL media="{escape(url)}"/>')
    return (
        f'<SegmentList timescale="{timescale}" startNumber="{media_sequence}">'
        f'<SegmentTimeline>{"".join(timeline_xml)}</SegmentTimeline>'
        f'{"".join(segment_urls_xml)}'
        '</SegmentList>'
    )


def _build_mpd_from_reps(video_reps: list[dict[str, Any]], audio_reps: list[dict[str, Any]], min_update: int) -> str:
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    video_xml = "".join(
        [
            (
                '<Representation id="' + escape(rep["id"]) + '" bandwidth="' + str(rep["bandwidth"]) + '"' +
                (f' codecs="{escape(rep["codecs"])}"' if rep.get("codecs") else "") +
                '>' +
                rep["segment_list_xml"] +
                '</Representation>'
            )
            for rep in video_reps
        ]
    )
    audio_xml = "".join(
        [
            (
                '<Representation id="' + escape(rep["id"]) + '" bandwidth="' + str(rep["bandwidth"]) + '"' +
                (f' codecs="{escape(rep["codecs"])}"' if rep.get("codecs") else "") +
                '>' +
                rep["segment_list_xml"] +
                '</Representation>'
            )
            for rep in audio_reps
        ]
    )

    audio_set = (
        f'<AdaptationSet id="2" mimeType="audio/mp2t" segmentAlignment="true">{audio_xml}</AdaptationSet>'
        if audio_reps
        else ""
    )

    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        f"<MPD availabilityStartTime=\"{now_iso}\" xmlns=\"urn:mpeg:dash:schema:mpd:2011\" "
        "type=\"dynamic\" profiles=\"urn:mpeg:dash:profile:isoff-live:2011\" "
        f"minimumUpdatePeriod=\"PT{max(1, min_update)}S\" "
        "timeShiftBufferDepth=\"PT120S\" suggestedPresentationDelay=\"PT8S\" "
        f"publishTime=\"{now_iso}\">"
        "<Period id=\"1\" start=\"PT0S\">"
        f"<AdaptationSet id=\"1\" mimeType=\"video/mp2t\" segmentAlignment=\"true\">{video_xml}</AdaptationSet>"
        f"{audio_set}"
        "</Period></MPD>\n"
    )


def _parse_media_segments_with_keys(media_text: str, base_url: str, referer: str | None) -> tuple[int, float, list[dict[str, Any]]]:
    media_sequence = 0
    target_duration = 4.0
    pending_duration: float | None = None
    current_key_url: str | None = None
    current_iv_hex: str | None = None
    segment_index = 0
    segments: list[dict[str, Any]] = []

    for raw_line in media_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except Exception:
                media_sequence = 0
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except Exception:
                target_duration = 4.0
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except Exception:
                pending_duration = target_duration
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attr_list(line)
            method = attrs.get("METHOD", "").strip().upper()

            # Only full-segment AES-128 can be transparently decrypted in proxy mode.
            # SAMPLE-AES and other methods should not trigger segment decryption here.
            if method == "AES-128":
                uri = attrs.get("URI")
                current_key_url = _safe_join(base_url, uri) if uri else None
                iv = attrs.get("IV")
                if iv and iv.lower().startswith("0x"):
                    current_iv_hex = iv[2:]
                elif iv:
                    current_iv_hex = iv
                else:
                    current_iv_hex = None
            else:
                current_key_url = None
                current_iv_hex = None
            continue
        if line.startswith("#"):
            continue

        duration = pending_duration if pending_duration is not None else target_duration
        raw_segment_url = _safe_join(base_url, line)
        iv_hex = current_iv_hex
        if current_key_url and not iv_hex:
            seq_num = media_sequence + segment_index
            if seq_num < 0:
                seq_num = 0
            seq_num = seq_num % (1 << 128)
            iv_hex = seq_num.to_bytes(16, byteorder="big", signed=False).hex()

        proxy_segment_url = _proxy_url(
            raw_segment_url,
            referer,
            route_path="dash",
            extras={"k": current_key_url, "iv": iv_hex} if current_key_url else None,
        )
        segments.append({"url": proxy_segment_url, "duration": duration})
        segment_index += 1
        pending_duration = None

    return media_sequence, target_duration, segments


def _count_keyed_segments(segments: list[dict[str, Any]]) -> int:
    count = 0
    for seg in segments:
        try:
            token = str(seg.get("url", "")).rsplit("/", 1)[-1]
            data = _decode_target(token)
            if data and data.get("key_url"):
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def _looks_like_mpeg_ts(payload: bytes) -> bool:
    if len(payload) < 188:
        return False
    if payload[0] != 0x47:
        return False

    packets = min(8, len(payload) // 188)

    sync_hits = 0
    for i in range(packets):
        if payload[i * 188] == 0x47:
            sync_hits += 1
    if packets <= 2:
        return sync_hits == packets
    return sync_hits >= max(2, int(packets * 0.75))


def _decrypt_dash_if_needed(body: bytes, target: dict[str, Any], referer: str | None) -> bytes:
    key_url = target.get("key_url")
    iv_hex = target.get("iv_hex")
    if not key_url:
        return body
    if len(body) == 0 or len(body) % 16 != 0:
        return body

    try:
        key_body, _, _ = _fetch(key_url, referer)
        if len(key_body) < 16:
            return body
        key = key_body[:16]
        iv = bytes.fromhex(iv_hex) if iv_hex else (0).to_bytes(16, byteorder="big")
        if len(iv) != 16:
            return body

        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(body)
        if len(decrypted) == 0:
            return body
        pad = decrypted[-1]
        if 1 <= pad <= 16 and decrypted.endswith(bytes([pad]) * pad):
            decrypted = decrypted[:-pad]

        # Guard against wrong key/IV producing garbage that breaks decoders.
        if _looks_like_mpeg_ts(decrypted):
            return decrypted
        return body
    except Exception:  # noqa: BLE001
        return body


def _make_response(body: bytes, content_type: str, status: int = 200):
    response = make_response(body, status)
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store"
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Proxy-Status"] = "ok"
    return response


@app.route("/playlist.m3u8", methods=["GET"])
def get_id():
    user_id = request.args.get("id")
    bandwidth_mode = (request.args.get("bandwidth") or "").strip().lower()
    if bandwidth_mode not in {"", "low", "high"}:
        bandwidth_mode = ""
    if not user_id:
        return _make_response(b"#EXTM3U\n", "application/vnd.apple.mpegurl", 400)

    try:
        upstream_url, referer = _resolve_source(user_id)
        body, headers, final_url = _fetch(upstream_url, referer)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 502

    content_type = headers.get("content-type", "application/octet-stream")
    is_playlist = _is_m3u8_payload(body, final_url)
    if is_playlist:
        playlist_text = body.decode("utf-8", errors="replace")
        if _is_master_playlist(playlist_text):
            variants, _ = _parse_master_playlist(playlist_text)
            selected = _select_variants(variants, bandwidth_mode or None)
            if selected:
                playlist_text = _filter_master_playlist(playlist_text, selected)
        rewritten = _rewrite_m3u8(playlist_text, final_url, referer, route_path="stream")
        response = _make_response(rewritten.encode("utf-8"), "application/vnd.apple.mpegurl")
        response.headers["X-Proxy-Playlist"] = "1"
        response.headers["X-Proxy-Absolute-Urls"] = "1"
        return response

    if "mpegurl" in content_type:
        logging.warning("upstream advertises mpegurl but payload is non-m3u8: %s", final_url)

    return _make_response(body, content_type)


@app.route("/playlist.mpd", methods=["GET"])
def get_mpd():
    user_id = request.args.get("id")
    bandwidth_mode = (request.args.get("bandwidth") or "").strip().lower()
    if bandwidth_mode not in {"", "low", "high"}:
        bandwidth_mode = ""
    if not user_id:
        return _make_response(b"", "application/dash+xml", 400)

    try:
        upstream_url, referer = _resolve_source(user_id)
        body, _, final_url = _fetch(upstream_url, referer)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 502

    if not _is_m3u8_payload(body, final_url):
        return jsonify({"status": "error", "message": "Upstream did not return an m3u8 playlist"}), 502

    master_text = body.decode("utf-8", errors="replace")
    video_reps: list[dict[str, Any]] = []
    audio_reps: list[dict[str, Any]] = []
    min_update = 4

    try:
        if _is_master_playlist(master_text):
            variants, audios = _parse_master_playlist(master_text)
            selected_variants = _select_variants(variants, bandwidth_mode or None)

            for idx, variant in enumerate(selected_variants, start=1):
                variant_url = _safe_join(final_url, variant["uri"])
                vb, _, vf = _fetch(variant_url, referer)
                if not _is_m3u8_payload(vb, vf):
                    continue
                v_text = vb.decode("utf-8", errors="replace")
                media_seq, target_dur, segs = _parse_media_segments_with_keys(v_text, vf, referer)
                if not segs:
                    continue
                min_update = min(min_update, max(1, int(math.ceil(target_dur))))
                video_reps.append(
                    {
                        "id": f"v{idx}",
                        "bandwidth": variant.get("bandwidth", 1000000) or 1000000,
                        "codecs": variant["attrs"].get("CODECS"),
                        "segment_list_xml": _segment_list_xml(media_seq, target_dur, segs),
                    }
                )
                logging.info("mpd video rep %s segments=%s keyed=%s", idx, len(segs), _count_keyed_segments(segs))

            allowed_groups = {v["attrs"].get("AUDIO", "") for v in selected_variants if v["attrs"].get("AUDIO")}
            selected_audios = [a for a in audios if a.get("URI") and (not allowed_groups or a.get("GROUP-ID", "") in allowed_groups)]
            for idx, audio in enumerate(selected_audios, start=1):
                a_url = _safe_join(final_url, audio["URI"])
                ab, _, af = _fetch(a_url, referer)
                if not _is_m3u8_payload(ab, af):
                    continue
                a_text = ab.decode("utf-8", errors="replace")
                media_seq, target_dur, segs = _parse_media_segments_with_keys(a_text, af, referer)
                if not segs:
                    continue
                min_update = min(min_update, max(1, int(math.ceil(target_dur))))
                audio_reps.append(
                    {
                        "id": f"a{idx}",
                        "bandwidth": 128000,
                        "codecs": None,
                        "segment_list_xml": _segment_list_xml(media_seq, target_dur, segs),
                    }
                )
                logging.info("mpd audio rep %s segments=%s keyed=%s", idx, len(segs), _count_keyed_segments(segs))
        else:
            media_seq, target_dur, segs = _parse_media_segments_with_keys(master_text, final_url, referer)
            if segs:
                min_update = max(1, int(math.ceil(target_dur)))
                video_reps.append(
                    {
                        "id": "v1",
                        "bandwidth": 1000000,
                        "codecs": None,
                        "segment_list_xml": _segment_list_xml(media_seq, target_dur, segs),
                    }
                )
                logging.info("mpd single playlist segments=%s keyed=%s", len(segs), _count_keyed_segments(segs))

        if not video_reps:
            raise RuntimeError("No playable variants found")

        mpd_text = _build_mpd_from_reps(video_reps, audio_reps, min_update)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 502

    response = _make_response(mpd_text.encode("utf-8"), "application/dash+xml")
    response.headers["X-Proxy-MPD"] = "1"
    return response


def _proxy_token_request(token: str):
    target = _decode_target(token)
    if not target:
        return jsonify({"status": "error", "message": "Invalid or expired token"}), 404

    url = target["url"]
    referer = target.get("referer")

    try:
        body, headers, final_url = _fetch(url, referer)
    except urllib.error.HTTPError as exc:
        return _make_response(exc.read() if exc.fp else b"", "text/plain; charset=utf-8", exc.code)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(exc)}), 502

    content_type = headers.get("content-type", "application/octet-stream")
    is_playlist = _is_m3u8_payload(body, final_url)
    if is_playlist:
        playlist_text = body.decode("utf-8", errors="replace")
        route_path = "dash" if request.path.startswith("/dash/") else "stream"
        rewritten = _rewrite_m3u8(playlist_text, final_url, referer, route_path=route_path)
        response = _make_response(rewritten.encode("utf-8"), "application/vnd.apple.mpegurl")
        response.headers["X-Proxy-Playlist"] = "1"
        response.headers["X-Proxy-Absolute-Urls"] = "1"
        return response

    if "mpegurl" in content_type:
        logging.warning("upstream advertises mpegurl but payload is non-m3u8: %s", final_url)
        content_type = "application/octet-stream"

    if request.path.startswith("/dash/"):
        had_key = bool(target.get("key_url"))
        body = _decrypt_dash_if_needed(body, target, referer)
        if content_type == "application/octet-stream":
            content_type = "video/mp2t"
    else:
        had_key = False

    response = _make_response(body, content_type)
    response.headers["X-Proxy-Stream"] = "1"
    if request.path.startswith("/dash/"):
        response.headers["X-Proxy-Dash-Key"] = "1" if had_key else "0"
        response.headers["X-Proxy-Dash-IV"] = "1" if bool(target.get("iv_hex")) else "0"
    return response


@app.route("/stream/<token>", methods=["GET"])
def stream_proxy(token: str):
    return _proxy_token_request(token)


@app.route("/dash/<token>", methods=["GET"])
def dash_proxy(token: str):
    return _proxy_token_request(token)


@app.route("/")
def index():
    return jsonify(
        {
            "status": "ok",
            "usage": "/playlist.m3u8?id=<id>",
            "dash": "/playlist.mpd?id=<id>",
        }
    )


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)
