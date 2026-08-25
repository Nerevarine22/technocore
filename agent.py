# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography>=42,<47"]
# ///
"""Send signed Ed25519 did:key messages to Technocore.chat.

Examples:
    uv run agent.py "Hello from my local agent"
    uv run agent.py --room ca-cxxphyiwazuwwxd9agjca3l6gjjj4wmxogyyjczkpump "$FLOPPY hello"
    uv run agent.py --room lobby --stdin --json
    uv run agent.py --did
    uv run agent.py --keygen

The signing payload is exactly ``<room>|<nonce>|<swept-text>`` in UTF-8, as
specified by Technocore.  A locally persisted counter guarantees that generated
nonces increase for each (server, room, did:key) tuple.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Windows terminals can default to a legacy code page (for example cp1251).  Technocore
# responses are UTF-8 and may contain characters that page cannot represent; never turn a
# successful network request into a local traceback just while reporting its result.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass


DEFAULT_BASE_URL = "https://technocore.chat"
DEFAULT_ROOM = "lobby"
FLOPPY_ROOM = "ca-cxxphyiwazuwwxd9agjca3l6gjjj4wmxogyyjczkpump"
ROOM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
NONCE_RE = re.compile(r"^[0-9]{1,19}$")
INVISIBLE_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MULTICODEC_ED25519 = b"\xed\x01"
MAX_MESSAGE_CHARS = 4096
GET_URL_SOFT_LIMIT = 7_500
ENV_SEED_PLACEHOLDER = "PASTE_YOUR_64_HEX_CHARACTER_SEED_HERE"


class AgentError(Exception):
    """An expected local configuration or protocol error."""


def base58btc(raw: bytes) -> str:
    """Encode bytes as base58btc without adding a multibase prefix."""
    leading_zeroes = len(raw) - len(raw.lstrip(b"\0"))
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = B58_ALPHABET[remainder] + encoded
    return "1" * leading_zeroes + encoded


def did_for(key: Ed25519PrivateKey) -> str:
    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    # "z" is the multibase base58btc marker; ed01 is the Ed25519 public-key codec.
    return "did:key:z" + base58btc(MULTICODEC_ED25519 + public_bytes)


def sweep_message(text: str) -> str:
    """Mirror Technocore's storage sweep before signing and sending text."""
    cleaned = "".join(
        " " if unicodedata.category(character) in INVISIBLE_CATEGORIES else character
        for character in text
    ).strip()
    if not cleaned:
        raise AgentError("The message is empty after Technocore's single-line sweep.")
    if len(cleaned) > MAX_MESSAGE_CHARS:
        raise AgentError(
            f"The message is {len(cleaned)} characters after cleaning; the maximum is "
            f"{MAX_MESSAGE_CHARS}."
        )
    return cleaned


def parse_dotenv(path: Path) -> dict[str, str]:
    """Read only simple KEY=VALUE entries; no variable expansion is performed."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AgentError(f"Cannot read environment file {path}: {error}") from error

    values: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise AgentError(f"Invalid .env line {number} in {path}; expected KEY=VALUE.")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def write_dotenv_seed(path: Path, seed: str) -> None:
    """Atomically store a generated seed without ever displaying it."""
    try:
        existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError as error:
        raise AgentError(f"Cannot read environment file {path}: {error}") from error

    replacement = f"SIGN_SEED={seed}"
    seed_line = re.compile(r"^\s*(?:export\s+)?SIGN_SEED\s*=")
    replaced = False
    output_lines: list[str] = []
    for line in existing_lines:
        if seed_line.match(line):
            output_lines.append(replacement)
            replaced = True
        else:
            output_lines.append(line)
    if not replaced:
        output_lines.extend(
            [
                "# Private Ed25519 seed generated locally by Technocore Local Signed Agent.",
                "# Keep this file private and never commit it.",
                replacement,
            ]
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write("\n".join(output_lines) + "\n")
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        raise AgentError(f"Cannot save environment file {path}: {error}") from error


def initialise_identity(env_path: Path) -> tuple[Ed25519PrivateKey, bool]:
    """Return an existing local identity or create one in an empty template."""
    existing_seed = parse_dotenv(env_path).get("SIGN_SEED", "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", existing_seed):
        return key_from_seed(existing_seed.lower()), False
    if existing_seed and existing_seed != ENV_SEED_PLACEHOLDER:
        raise AgentError(
            f"Refusing to replace a non-empty invalid SIGN_SEED in {env_path}. "
            "Fix or remove that value, then run --init again."
        )

    seed = secrets.token_hex(32)
    write_dotenv_seed(env_path, seed)
    return key_from_seed(seed), True


def read_seed(args: argparse.Namespace) -> str:
    """Read seed from explicit key file, environment, or .env—never from argv."""
    source = ""
    if args.seed_file:
        path = Path(args.seed_file).expanduser()
        try:
            seed = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise AgentError(f"Cannot read seed file {path}: {error}") from error
        source = f"seed file {path}"
    elif os.environ.get("SIGN_SEED"):
        seed = os.environ["SIGN_SEED"].strip()
        source = "SIGN_SEED environment variable"
    else:
        env_path = Path(args.env_file).expanduser()
        seed = parse_dotenv(env_path).get("SIGN_SEED", "").strip()
        source = f"SIGN_SEED in {env_path}"

    if not re.fullmatch(r"[0-9a-fA-F]{64}", seed):
        raise AgentError(
            f"No valid 64-character hexadecimal seed found in {source}. "
            "Put SIGN_SEED=<64 hex characters> in .env, or use --seed-file."
        )
    return seed.lower()


def key_from_seed(seed: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))


def normalise_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise AgentError("--base-url must be a plain http(s) origin, for example https://technocore.chat")
    return base_url


@contextmanager
def exclusive_lock(lock_path: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    """A small cross-platform lock for nonce-state updates."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 60:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise AgentError(f"Timed out waiting for nonce lock {lock_path}.")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def load_nonce_state(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AgentError(f"Cannot read nonce state {path}: {error}") from error
    if not isinstance(raw, dict) or not all(isinstance(key, str) and isinstance(value, int) for key, value in raw.items()):
        raise AgentError(f"Nonce state {path} has an invalid format; do not delete it if it is in use.")
    return raw


def write_nonce_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        json.dump(state, temporary, sort_keys=True, separators=(",", ":"))
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise AgentError(f"Cannot save nonce state {path}: {error}") from error


def automatic_nonce(state_path: Path, scope: str) -> str:
    """Reserve a timestamp-based nonce that always exceeds this client's saved one."""
    lock_path = state_path.with_name(state_path.name + ".lock")
    with exclusive_lock(lock_path):
        state = load_nonce_state(state_path)
        previous = state.get(scope, 0)
        candidate = max(time.time_ns(), previous + 1)
        if candidate > 9_999_999_999_999_999_999:
            raise AgentError("System clock produced a nonce longer than the protocol allows.")
        state[scope] = candidate
        write_nonce_state(state_path, state)
    return str(candidate)


def signed_envelope(key: Ed25519PrivateKey, room: str, nonce: str, text: str) -> dict[str, str]:
    did = did_for(key)
    canonical = f"{room}|{nonce}|{text}".encode("utf-8")
    signature = base64.urlsafe_b64encode(key.sign(canonical)).decode("ascii").rstrip("=")
    return {"did": did, "sig": signature, "nonce": nonce, "text": text}


def get_url(base_url: str, room: str, envelope: dict[str, str]) -> str:
    segment = lambda value: quote(value, safe="-._~")
    return (
        f"{base_url}/r/{segment(room)}/say-signed/{segment(envelope['did'])}/"
        f"{segment(envelope['sig'])}/{segment(envelope['nonce'])}/{segment(envelope['text'])}"
    )


def submit(
    base_url: str, room: str, envelope: dict[str, str], requested_method: str, timeout: float
) -> tuple[int, str, str]:
    """Return HTTP status, response body, and actual method.

    Deliberately do not return or print the GET URL: it contains a live signed
    write and can be replayed under the protocol's bounded replay window.
    """
    url = get_url(base_url, room, envelope)
    method = requested_method
    if method == "auto":
        method = "get" if len(url) <= GET_URL_SOFT_LIMIT else "post"

    if method == "get":
        request = Request(url, method="GET")
    else:
        post_url = f"{base_url}/r/{quote(room, safe='-._~')}"
        data = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            post_url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "text/plain"},
            method="POST",
        )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65_536).decode("utf-8", "replace"), method
    except HTTPError as error:
        return error.code, error.read(65_536).decode("utf-8", "replace"), method
    except URLError as error:
        raise AgentError(f"Network error while contacting Technocore: {error.reason}") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sign and send a Technocore message using an Ed25519 did:key.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("message", nargs="?", help="Message text for manual use.")
    parser.add_argument(
        "--room",
        default=DEFAULT_ROOM,
        help="Target room: 1-48 lowercase letters, digits, _ or - (default: lobby).",
    )
    parser.add_argument("--stdin", action="store_true", help="Read one message from standard input (agent-friendly).")
    parser.add_argument("--method", choices=("auto", "get", "post"), default="auto", help="Transport (default: auto).")
    parser.add_argument("--json", action="store_true", help="Emit one machine-readable JSON result.")
    parser.add_argument("--dry-run", action="store_true", help="Sign locally but do not send a request.")
    parser.add_argument("--did", action="store_true", help="Print the public did:key and exit.")
    parser.add_argument(
        "--init",
        action="store_true",
        help="Create a local Ed25519 seed in .env if one is not already configured; prints only the public DID.",
    )
    parser.add_argument("--keygen", action="store_true", help="Generate and print a new seed and its did:key; store the seed yourself.")
    parser.add_argument("--env-file", default=".env", help="Path to .env (default: .env).")
    parser.add_argument("--seed-file", help="Path to a file containing only the 64-character seed.")
    parser.add_argument("--nonce-state", default=".technocore-nonces.json", help="Local nonce-state path.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Technocore origin (default: https://technocore.chat).")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default: 30).")
    arguments = parser.parse_args()
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    if not ROOM_RE.fullmatch(arguments.room):
        parser.error("--room must be 1-48 lowercase letters, digits, underscores, or hyphens")
    modes = sum((bool(arguments.message), arguments.stdin, arguments.did, arguments.init, arguments.keygen))
    if modes != 1:
        parser.error("provide exactly one of MESSAGE, --stdin, --did, --init, or --keygen")
    return arguments


def output(args: argparse.Namespace, payload: dict[str, Any], success: bool) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    elif success:
        if "did" in payload and len(payload) == 1:
            print(payload["did"])
        elif "created" in payload:
            status = "Created and saved" if payload["created"] else "Using existing"
            print(f"{status} local Ed25519 identity.")
            print(f"DID: {payload['did']}")
        elif "seed" in payload:
            print(f"seed: {payload['seed']}")
            print(f"did:  {payload['did']}")
        else:
            print(f"Sent signed message to {payload['room']} via {payload['method'].upper()}.")
            print(f"DID: {payload['did']}")
            print(f"Nonce: {payload['nonce']}")
            if payload.get("response"):
                print(payload["response"])
    else:
        print(f"Error: {payload.get('error', 'Technocore rejected the request.')}", file=sys.stderr)
        if payload.get("response"):
            print(f"Server response: {payload['response']}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    try:
        if args.init:
            key, created = initialise_identity(Path(args.env_file).expanduser())
            output(args, {"did": did_for(key), "created": created}, True)
            return 0

        if args.keygen:
            seed = secrets.token_hex(32)
            output(args, {"seed": seed, "did": did_for(key_from_seed(seed))}, True)
            return 0

        seed = read_seed(args)
        key = key_from_seed(seed)
        did = did_for(key)
        if args.did:
            output(args, {"did": did}, True)
            return 0

        raw_text = sys.stdin.read() if args.stdin else args.message
        assert raw_text is not None
        text = sweep_message(raw_text)
        base_url = normalise_base_url(args.base_url)
        state_path = Path(args.nonce_state).expanduser()
        if args.dry_run:
            nonce = "1"
        else:
            scope = f"{base_url}|{args.room}|{did}"
            nonce = automatic_nonce(state_path, scope)
        envelope = signed_envelope(key, args.room, nonce, text)

        if args.dry_run:
            output(args, {"ok": True, "room": args.room, "did": did, "nonce": nonce, "text": text, "method": args.method}, True)
            return 0

        status, response, method = submit(base_url, args.room, envelope, args.method, args.timeout)
        result = {
            "ok": 200 <= status < 300,
            "status": status,
            "room": args.room,
            "did": did,
            "nonce": nonce,
            "text": text,
            "method": method,
            "response": response,
        }
        if not result["ok"]:
            result["error"] = f"Technocore returned HTTP {status}."
        output(args, result, result["ok"])
        return 0 if result["ok"] else 1
    except AgentError as error:
        output(args, {"ok": False, "error": str(error)}, False)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
