"""Passive mitmproxy evidence collection; raw bodies remain in this workspace."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "04-capture/decoded/http"
OUTPUT.mkdir(parents=True, exist_ok=True)


def response(flow):
    host = flow.request.pretty_host
    if not host.endswith("megagamelog.com"):
        return
    ident = flow.id
    dest = OUTPUT / ident
    dest.mkdir(exist_ok=True)
    request = flow.request.content or b""
    response = flow.response.content or b""
    (dest / "request.body").write_bytes(request)
    (dest / "response.body").write_bytes(response)
    metadata = {"id": ident, "timestamp_start": flow.request.timestamp_start,
                "host": host, "method": flow.request.method,
                "path": urlsplit(flow.request.url).path,
                "query_names": list(flow.request.query.keys()),
                "status_code": flow.response.status_code,
                "request_bytes": len(request), "response_bytes": len(response),
                "response_sha256": hashlib.sha256(response).hexdigest(),
                "request_header_names": list(flow.request.headers.keys()),
                "response_content_type": flow.response.headers.get("content-type")}
    (dest / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    if metadata["path"].endswith("/sl.json"):
        (ROOT / "01-device/sl.json").write_bytes(response)

