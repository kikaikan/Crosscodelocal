"""Offline CrossCore config codec; Python standard library only.

Character permutation reference (function DdooEennccyyppttSsttrr, lines 5-25):
https://github.com/AXiX-official/CrossCore-Lua-Tool/blob/master/Assembly-CSharp/ABCustom.cs
The current local config samples confirm permutation -> base64 -> UTF-8 Lua data.
No eval, Lua interpreter, network requests, or arbitrary deserialization is used.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sys
import math
from pathlib import Path
import re
from typing import Any

SWAP_STEPS = (2, 3, 1, 1, 3, 1, 2, 1, 1, 3, 1, 2, 4, 1, 1, 2, 2, 4, 4)
UTF8_BOM = b"\xef\xbb\xbf"
MAX_INPUT_BYTES = 1024 * 1024
SOURCE_URL = (
    "https://github.com/AXiX-official/CrossCore-Lua-Tool/"
    "blob/master/Assembly-CSharp/ABCustom.cs#L5-L25"
)


def app_root() -> Path:
    """Directory that holds the executable (frozen) or the source checkout.

    A --onefile build unpacks its bundled modules into a temporary directory, so
    Path(__file__) stops describing where the user keeps data tables. Every
    runtime data path goes through here (or app_path) so the frozen server reads
    `data/`, `05-protocol/` and `03-unpack/` next to the executable, while a
    source run keeps the existing repository-relative behaviour.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


# Alias used by the server modules: the application directory in both modes.
APP_ROOT = app_root()


def app_path(*parts) -> Path:
    """Resolve a runtime data path below the application directory.

    The source checkout has two candidate roots (the repository root and
    07-server itself), so the first existing candidate wins; if none exists the
    repository root is returned so the error message names a stable location.
    """
    candidates = [APP_ROOT.joinpath(*parts)]
    if not getattr(sys, "frozen", False):
        candidates.append(Path(__file__).resolve().parents[2] / "07-server" / Path(*parts))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def swap_characters(text: str) -> str:
    """Apply the disjoint swaps. Applying the operation twice restores text."""
    chars = list(text)
    position = 0
    step_index = 0
    while position < len(chars):
        other = position + SWAP_STEPS[step_index % len(SWAP_STEPS)]
        step_index += 1
        if other >= len(chars):
            break
        chars[position], chars[other] = chars[other], chars[position]
        position = other + 1
    return "".join(chars)


def decode_payload(encoded: str) -> bytes:
    """Decode canonical, whitespace-free base64 after restoring the permutation."""
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("Encoded input exceeds the size limit")
    if not encoded.isascii():
        raise ValueError("Encoded payload must be ASCII without a BOM")
    restored = swap_characters(encoded)
    try:
        decoded = base64.b64decode(restored, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Restored payload is not valid base64") from exc
    if base64.b64encode(decoded).decode("ascii") != restored:
        raise ValueError("Restored payload is not canonical base64")
    return decoded


def encode_payload(decoded: bytes) -> str:
    """Encode the original plaintext bytes, preserving their exact formatting."""
    if len(decoded) > MAX_INPUT_BYTES * 3 // 4:
        raise ValueError("Decoded input exceeds the size limit")
    return swap_characters(base64.b64encode(decoded).decode("ascii"))


def decode_file_bytes(raw: bytes) -> tuple[bytes, bool]:
    """Return plaintext and the original BOM flag, without stripping whitespace."""
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Raw input exceeds the size limit")
    has_bom = raw.startswith(UTF8_BOM)
    payload = raw[len(UTF8_BOM):] if has_bom else raw
    try:
        encoded = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("Encoded file is not ASCII with an optional UTF-8 BOM") from exc
    return decode_payload(encoded), has_bom


def encode_file_bytes(plain: bytes, has_bom: bool = False) -> bytes:
    return (UTF8_BOM if has_bom else b"") + encode_payload(plain).encode("ascii")


class LuaDataParser:
    """Parse a bounded Lua data subset into tagged JSON, retaining table key types.

    Accepts tables, finite decimal numbers, quoted strings, true/false/nil,
    bracketed scalar keys, identifier keys, and positional fields. Rejects code,
    comments, operators, calls, long strings, duplicate keys, and table keys.
    The tagged result is a syntax representation; it does not execute Lua's nil
    assignment behavior or coerce tables into JSON objects with string keys.
    """

    NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
    IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    def __init__(self, text: str, max_depth: int = 32, max_nodes: int = 20000):
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("Lua data exceeds the size limit")
        self.text = text
        self.position = 0
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.nodes = 0

    def fail(self, message: str) -> None:
        # Do not include source text: callers may decode account-specific data.
        raise ValueError(f"{message} at character {self.position}")

    def whitespace(self) -> None:
        while self.position < len(self.text) and self.text[self.position].isspace():
            self.position += 1

    def consume(self, token: str) -> bool:
        self.whitespace()
        if self.text.startswith(token, self.position):
            self.position += len(token)
            return True
        return False

    def expect(self, token: str) -> None:
        if not self.consume(token):
            self.fail(f"Expected {token!r}")

    def value(self, depth: int = 0) -> Any:
        self.whitespace()
        self.nodes += 1
        if self.nodes > self.max_nodes:
            self.fail("Node limit exceeded")
        if depth > self.max_depth:
            self.fail("Depth limit exceeded")
        if self.position >= len(self.text):
            self.fail("Missing value")
        char = self.text[self.position]
        if char == "{":
            return self.table(depth)
        if char in ("'", '"'):
            return self.string()
        match = self.NUMBER.match(self.text, self.position)
        if match:
            literal = match.group(0)
            if len(literal) > 64:
                self.fail("Number exceeds the length limit")
            self.position = match.end()
            if "." in literal or "e" in literal.lower():
                number = float(literal)
                if not math.isfinite(number):
                    self.fail("Non-finite number")
                return number
            return int(literal)
        match = self.IDENTIFIER.match(self.text, self.position)
        if match and match.group(0) in ("true", "false", "nil"):
            self.position = match.end()
            return {"true": True, "false": False, "nil": None}[match.group(0)]
        self.fail("Unsupported value")

    def string(self) -> str:
        quote = self.text[self.position]
        self.position += 1
        chars: list[str] = []
        escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
                   "t": "\t", "v": "\v", "\\": "\\", '"': '"', "'": "'"}
        while self.position < len(self.text):
            char = self.text[self.position]
            self.position += 1
            if char == quote:
                return "".join(chars)
            if char in "\r\n":
                self.fail("Unescaped newline in string")
            if char != "\\":
                chars.append(char)
                continue
            if self.position >= len(self.text):
                self.fail("Incomplete escape")
            escaped = self.text[self.position]
            self.position += 1
            if escaped in escapes:
                chars.append(escapes[escaped])
            elif escaped.isascii() and escaped.isdigit():
                digits = escaped
                for _ in range(2):
                    if self.position < len(self.text) and self.text[self.position] in "0123456789":
                        digits += self.text[self.position]
                        self.position += 1
                    else:
                        break
                codepoint = int(digits)
                if codepoint > 255:
                    self.fail("Decimal escape outside byte range")
                chars.append(chr(codepoint))
            else:
                self.fail("Unsupported string escape")
        self.fail("Unterminated string")

    def table(self, depth: int) -> dict[str, Any]:
        self.expect("{")
        entries = []
        keys = set()
        implicit_index = 1
        if self.consume("}"):
            return {"$lua_table": entries}
        while True:
            self.whitespace()
            if self.consume("["):
                key = self.value(depth + 1)
                self.expect("]")
                self.expect("=")
            else:
                match = self.IDENTIFIER.match(self.text, self.position)
                saved = self.position
                if match:
                    self.position = match.end()
                if match and self.consume("="):
                    key = match.group(0)
                else:
                    self.position = saved
                    key = implicit_index
                    implicit_index += 1
            if key is None or isinstance(key, dict):
                self.fail("Table key must be a non-nil scalar")
            # Lua treats integral floats as the corresponding integer key, while
            # boolean true is a distinct key from number 1.
            key_type = "bool" if isinstance(key, bool) else "number" if isinstance(key, (int, float)) else "string"
            signature = (key_type, key)
            if signature in keys:
                self.fail("Duplicate table key")
            keys.add(signature)
            item = self.value(depth + 1)
            entries.append({"key": key, "value": item})
            if self.consume("}"):
                break
            if not (self.consume(",") or self.consume(";")):
                self.fail("Expected field separator")
            if self.consume("}"):
                break
        return {"$lua_table": entries}

    def parse(self) -> dict[str, Any]:
        value = self.value()
        self.whitespace()
        if self.position != len(self.text):
            self.fail("Trailing input")
        if not isinstance(value, dict) or "$lua_table" not in value:
            self.fail("Top-level value must be a table")
        return value


def parse_lua_table(text: str) -> dict[str, Any]:
    return LuaDataParser(text).parse()


def parse_server_list(plain: bytes) -> dict[str, Any]:
    """Strict JSON and observed server-list envelope validation, without I/O."""
    def pairs_to_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def invalid_constant(_: str) -> None:
        raise ValueError("Non-standard JSON numeric constant")

    result = json.loads(
        plain.decode("utf-8"), object_pairs_hook=pairs_to_object,
        parse_constant=invalid_constant,
    )
    if not isinstance(result, dict) or result.get("code") != 0:
        raise ValueError("Expected a successful server-list envelope")
    servers = result.get("data")
    if not isinstance(servers, list) or not servers:
        raise ValueError("Expected a nonempty server list")
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError("Expected server objects")
        if type(server.get("id")) is not int:
            raise ValueError("Expected numeric server id")
        if not isinstance(server.get("serverName"), str) or not server["serverName"]:
            raise ValueError("Expected a server name")
        addresses = server.get("serverIp")
        if not isinstance(addresses, list) or not addresses or not all(
            isinstance(address, str) and address for address in addresses
        ):
            raise ValueError("Expected nonempty serverIp strings")
    return result


def create_server_list_samples(source: Path, output_dir: Path) -> dict[str, Any]:
    raw = source.read_bytes()
    plain, has_bom = decode_file_bytes(raw)
    parsed = parse_server_list(plain)
    exact = encode_file_bytes(plain, has_bom) == raw
    if not exact:
        raise ValueError("Server-list byte-exact round trip failed")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sl.raw.txt").write_bytes(raw)
    (output_dir / "sl.decoded.json").write_bytes(plain)
    (output_dir / "sl.parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "source": str(source.resolve()), "algorithm_source": SOURCE_URL,
        "raw_file": "sl.raw.txt", "decoded_file": "sl.decoded.json", "parsed_file": "sl.parsed.json",
        "raw_bytes": len(raw), "decoded_bytes": len(plain), "utf8_bom": has_bom,
        "sha256_raw": hashlib.sha256(raw).hexdigest(),
        "sha256_decoded": hashlib.sha256(plain).hexdigest(),
        "strict_json_valid": True, "server_list_valid": True,
        "server_count": len(parsed["data"]), "round_trip_exact": exact,
        "retrieval": "Existing local sample captured by the main task; codec performs no network request",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def create_samples(input_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Copy only config*.txt inputs; emit exact raw/decoded pairs and regressions."""
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    inputs = sorted(input_dir.glob("*config*.txt"))
    if not inputs:
        raise ValueError("No config samples found")
    for source in inputs:
        raw = source.read_bytes()
        plain, has_bom = decode_file_bytes(raw)
        parsed = parse_lua_table(plain.decode("utf-8"))
        round_trip = encode_file_bytes(plain, has_bom) == raw
        if not round_trip:
            raise ValueError(f"Byte-exact round trip failed: {source.name}")
        stem = source.stem
        raw_name = stem + ".raw.txt"
        decoded_name = stem + ".decoded.lua.txt"
        parsed_name = stem + ".parsed.json"
        (output_dir / raw_name).write_bytes(raw)
        (output_dir / decoded_name).write_bytes(plain)
        (output_dir / parsed_name).write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        records.append({
            "source": str(source.resolve()), "name": source.name,
            "raw_file": raw_name, "decoded_file": decoded_name, "parsed_file": parsed_name,
            "raw_bytes": len(raw), "decoded_bytes": len(plain), "utf8_bom": has_bom,
            "sha256_raw": hashlib.sha256(raw).hexdigest(),
            "sha256_decoded": hashlib.sha256(plain).hexdigest(),
            "top_level_entries": len(parsed["$lua_table"]), "round_trip_exact": round_trip,
        })
    manifest = {
        "algorithm_source": SOURCE_URL,
        "algorithm": "disjoint character swaps -> strict canonical base64 -> UTF-8 Lua data subset",
        "sample_count": len(records), "passed_count": len(records),
        "files": records,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("decode", "encode"):
        action = sub.add_parser(command)
        action.add_argument("source", type=Path)
        action.add_argument("output", type=Path)
        if command == "encode":
            action.add_argument("--bom", action="store_true", help="Preserve an original UTF-8 BOM")
    batch = sub.add_parser("batch")
    batch.add_argument("--input-dir", type=Path, required=True)
    batch.add_argument("--output-dir", type=Path, required=True)
    server_list = sub.add_parser("server-list")
    server_list.add_argument("source", type=Path)
    server_list.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "decode":
        plain, has_bom = decode_file_bytes(args.source.read_bytes())
        args.output.write_bytes(plain)
        print(json.dumps({"decoded_bytes": len(plain), "source_utf8_bom": has_bom}))
    elif args.command == "encode":
        raw = encode_file_bytes(args.source.read_bytes(), args.bom)
        args.output.write_bytes(raw)
        print(json.dumps({"encoded_bytes": len(raw), "utf8_bom": args.bom}))
    elif args.command == "batch":
        result = create_samples(args.input_dir, args.output_dir)
        print(json.dumps({"sample_count": result["sample_count"], "passed_count": result["passed_count"]}))
    else:
        result = create_server_list_samples(args.source, args.output_dir)
        print(json.dumps({key: result[key] for key in (
            "raw_bytes", "decoded_bytes", "strict_json_valid", "server_count", "round_trip_exact"
        )}))


if __name__ == "__main__":
    main()
