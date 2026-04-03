from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

ROOT_MAIN_PATH = Path(__file__).resolve().parent.parent.parent / "main.py"


def _load_core_app():
    spec = importlib.util.spec_from_file_location("sliv_core", ROOT_MAIN_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"Failed to load core module from {ROOT_MAIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


APP = _load_core_app()


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _extract_path(req: Any) -> str:
    path = _to_str(getattr(req, "path", ""))
    if path:
        return path

    url = _to_str(getattr(req, "url", ""))
    if url:
        parsed = urlparse(url)
        return parsed.path or "/"

    return "/"


def _extract_query(req: Any) -> str:
    query = getattr(req, "query", None)
    if isinstance(query, str):
        return query
    if isinstance(query, dict):
        items: list[tuple[str, str]] = []
        for key, value in query.items():
            if isinstance(value, list):
                for item in value:
                    items.append((_to_str(key), _to_str(item)))
            else:
                items.append((_to_str(key), _to_str(value)))
        return urlencode(items, doseq=True)

    query_string = _to_str(getattr(req, "queryString", ""))
    if query_string:
        return query_string

    url = _to_str(getattr(req, "url", ""))
    if url:
        parsed = urlparse(url)
        return parsed.query

    return ""


def _extract_headers(req: Any) -> dict[str, str]:
    headers = getattr(req, "headers", None)
    if isinstance(headers, dict):
        return {str(k): _to_str(v) for k, v in headers.items()}
    return {}


def _extract_body(req: Any) -> bytes:
    for attr in ("bodyBinary", "bodyRaw", "body"):
        value = getattr(req, attr, None)
        if value is None:
            continue
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        return _to_str(value).encode("utf-8")
    return b""


def _derive_base_url(req: Any, headers: dict[str, str]) -> str:
    configured = os.getenv("APPWRITE_FUNCTION_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/") + "/"

    url = _to_str(getattr(req, "url", ""))
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"

    lower_headers = {k.lower(): v for k, v in headers.items()}
    host = lower_headers.get("x-forwarded-host") or lower_headers.get("host")
    scheme = lower_headers.get("x-forwarded-proto", "https")
    if host:
        return f"{scheme}://{host}/"

    return "http://localhost/"


def _send_response(context: Any, body: bytes, status: int, headers: dict[str, str]):
    res = getattr(context, "res", None)
    if res is None:
        return {
            "statusCode": status,
            "headers": headers,
            "body": body.decode("utf-8", errors="replace"),
        }

    if hasattr(res, "binary"):
        return res.binary(body, status, headers)

    if hasattr(res, "send"):
        return res.send(body, status, headers)

    text_body = body.decode("utf-8", errors="replace")
    if hasattr(res, "text"):
        return res.text(text_body, status, headers)

    return {
        "statusCode": status,
        "headers": headers,
        "body": text_body,
    }


def main(context):
    req = getattr(context, "req", None)
    if req is None:
        return _send_response(
            context,
            b'{"status":"error","message":"missing request context"}',
            500,
            {"content-type": "application/json"},
        )

    method = _to_str(getattr(req, "method", "GET")).upper() or "GET"
    path = _extract_path(req)
    query = _extract_query(req)
    headers = _extract_headers(req)
    body = _extract_body(req)
    base_url = _derive_base_url(req, headers)

    with APP.test_client() as client:
        response = client.open(
            path=path,
            method=method,
            query_string=query,
            headers=headers,
            data=body,
            base_url=base_url,
        )

    response_headers = {}
    for key, value in response.headers.items():
        if key.lower() == "content-length":
            continue
        response_headers[key] = value

    return _send_response(
        context,
        response.get_data(),
        response.status_code,
        response_headers,
    )