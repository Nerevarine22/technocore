# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography>=42,<47"]
# ///
"""Local-only browser interface for the Technocore signed-message client."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

import agent


HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 16_384
INDEX_PATH = Path(__file__).with_name("web") / "index.html"
APP_PATH = Path(__file__).with_name("web") / "app.js"
ROOM_LINE_RE = re.compile(r"^/r/([a-z0-9][a-z0-9_-]{0,47})(?:\s|$)")


def load_identity() -> tuple[Any, str]:
    """Load the key locally; callers receive only the public DID."""
    env_path = Path(".env")
    seed = agent.parse_dotenv(env_path).get("SIGN_SEED", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", seed):
        raise agent.AgentError("No local identity found. Run 'uv run agent.py --init' first.")
    key = agent.key_from_seed(seed.lower())
    return key, agent.did_for(key)


class App:
    def __init__(self, key: Any, did: str, csrf_token: str) -> None:
        self.key = key
        self.did = did
        self.csrf_token = csrf_token

    def send(self, room: str, raw_text: str) -> dict[str, Any]:
        if not agent.ROOM_RE.fullmatch(room):
            raise agent.AgentError("Room names use 1-48 lowercase letters, digits, underscores, or hyphens.")
        text = agent.sweep_message(raw_text)
        base_url = agent.normalise_base_url(agent.DEFAULT_BASE_URL)
        nonce = agent.automatic_nonce(Path(".technocore-nonces.json"), f"{base_url}|{room}|{self.did}")
        envelope = agent.signed_envelope(self.key, room, nonce, text)
        status, _response, method = agent.submit(base_url, room, envelope, "auto", 30.0)
        if not 200 <= status < 300:
            raise agent.AgentError(f"Technocore rejected the message (HTTP {status}).")
        return {
            "ok": True,
            "accepted": True,
            "room": room,
            "text": text,
            "did": self.did,
            "nonce": nonce,
            "method": method,
            "status": status,
            "receipt": self.find_receipt(base_url, room, text, nonce),
        }

    def list_rooms(self) -> list[str]:
        """Return only room names; topics and all other remote text stay untrusted."""
        try:
            with urlopen(f"{agent.DEFAULT_BASE_URL}/rooms", timeout=15) as response:
                listing = response.read(262_144).decode("utf-8", "replace")
        except OSError as error:
            raise agent.AgentError(f"Could not load public rooms: {error}") from error
        return [match.group(1) for line in listing.splitlines() if (match := ROOM_LINE_RE.match(line))]

    def find_receipt(self, base_url: str, room: str, text: str, nonce: str) -> dict[str, int] | None:
        """Look for the just-accepted message without exposing unrelated room content."""
        try:
            url = f"{base_url}/r/{quote(room, safe='-._~')}?limit=200&format=json"
            with urlopen(url, timeout=15) as response:
                payload = json.loads(response.read(262_144).decode("utf-8"))
            for message in payload.get("messages", []):
                if (
                    message.get("from") == self.did
                    and str(message.get("nonce")) == nonce
                    and message.get("text") == text
                    and isinstance(message.get("seq"), int)
                ):
                    return {"seq": message["seq"]}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return None


def handler_for(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TechnocoreLocal/1.0"

        def log_message(self, _format: str, *_args: object) -> None:
            # Do not log message bodies, headers, or other browser input.
            return

        def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def send_static(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/", "/index.html"}:
                self.send_static(INDEX_PATH, "text/html; charset=utf-8")
            elif self.path == "/app.js":
                self.send_static(APP_PATH, "text/javascript; charset=utf-8")
            elif self.path == "/api/status":
                self.send_json(HTTPStatus.OK, {"did": app.did, "csrfToken": app.csrf_token})
            elif self.path == "/api/rooms":
                try:
                    self.send_json(HTTPStatus.OK, {"rooms": app.list_rooms()})
                except agent.AgentError as error:
                    self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/send":
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), app.csrf_token):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid local request token."})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 1 or length > MAX_REQUEST_BYTES:
                    raise ValueError
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("room"), str) or not isinstance(data.get("text"), str):
                    raise agent.AgentError("Room and message must be text.")
                self.send_json(HTTPStatus.OK, app.send(data["room"], data["text"]))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request."})
            except agent.AgentError as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    try:
        key, did = load_identity()
    except agent.AgentError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    address = f"http://{HOST}:{args.port}/"
    server = ThreadingHTTPServer((HOST, args.port), handler_for(App(key, did, secrets.token_urlsafe(32))))
    print(f"Technocore local interface: {address}")
    print(f"DID: {did}")
    print("Listening on 127.0.0.1 only. Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLocal interface stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
