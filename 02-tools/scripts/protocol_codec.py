"""Offline IVProto schema extraction and binary frame codec.

Sources: device Lua GameMsg, GMsgNo, GameMsgMgr, ProtParse, MsgBuffer, BufferUtil.
Schema-derived self-tests are synthetic and are never treated as captured traffic.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import re
import struct
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PRIMITIVES = {"byte": "B", "short": "h", "ushort": "H", "int": "i",
              "uint": "I", "long": "q", "float": "f", "double": "d"}
ALL_PRIMITIVES = set(PRIMITIVES) | {"string", "json", "bool"}


def bracket_array(field_type):
    """Accept the recovered 'T[]' spelling as the 'array|T' composite.

    The static recovery records sFurniture.childID as 'int[]'; the client writes
    that list when furniture is stacked (DormFurniture.lua:668-671), so a frame
    the codec cannot read would close the connection before dispatch.
    """
    return 'array|' + field_type[:-2] if field_type.endswith('[]') else field_type


class CodecError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class WireConfig:
    byte_order: str
    string_encoding: str = "utf-8"
    string_length_bytes: int = 2
    max_frame_size: int = 32767
    max_depth: int = 64

    @property
    def prefix(self):
        if self.byte_order not in ("little", "big"):
            raise CodecError("Byte order must be explicitly little or big")
        return "<" if self.byte_order == "little" else ">"


@dataclasses.dataclass
class JSONValue:
    text: str
    parsed: object
    serialization_kind: str = "json"


def lua_data(value):
    """Convert the safe parser's tagged syntax tree into Lua-table data without execution."""
    if not isinstance(value, dict) or "$lua_table" not in value:
        return value
    entries = value["$lua_table"]
    converted = [(e["key"], lua_data(e["value"])) for e in entries if e["value"] is not None]
    if converted and [k for k, _ in converted] == list(range(1, len(converted) + 1)):
        return [v for _, v in converted]
    # Preserve tagged tables if Python would conflate boolean and numeric keys.
    keys = [k for k, _ in converted]
    if len(set(keys)) != len(keys):
        return value
    return dict(converted)


def lua_literal(value):
    """Serialize bounded Python data as a Lua expression; never execute Lua code."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("Lua table numeric value must be finite")
        return repr(value)
    if isinstance(value, str):
        escaped = []
        for char in value:
            if char in ('"', "\\"):
                escaped.append("\\" + char)
            elif ord(char) < 32 or ord(char) == 127:
                escaped.append("\\" + f"{ord(char):03d}")
            else:
                escaped.append(char)
        return '"' + "".join(escaped) + '"'
    if isinstance(value, list):
        return "{" + ",".join(lua_literal(v) for v in value) + "}"
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if item is None:
                continue
            if not isinstance(key, (str, int, float, bool)):
                raise CodecError("Lua table key must be scalar")
            encoded_key = key if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else "[" + lua_literal(key) + "]"
            result.append(encoded_key + "=" + lua_literal(item))
        return "{" + ",".join(result) + "}"
    raise CodecError("Unsupported Lua table data type")


class FloatValue(float):
    def __new__(cls, value, raw, kind):
        result = super().__new__(cls, value)
        result.raw = raw
        result.kind = kind
        return result


@dataclasses.dataclass
class BoolValue:
    raw: int

    def __bool__(self):
        return bool(self.raw)


class WireStruct(dict):
    """Readable fields plus original field order, including duplicate named fields."""
    def __init__(self):
        super().__init__()
        self.wire = []


@dataclasses.dataclass
class Frame:
    opcode: int
    name: str
    fields: WireStruct
    size: int

    def readable(self):
        return {"opcode": self.opcode, "name": self.name, "size": self.size,
                "fields": readable(self.fields)}


@dataclasses.dataclass
class OuterPacket:
    flag: int
    payload: bytes
    size: int
    direction: str = "c2s"
    timestamp: int | None = None

    def decode_inner(self, codec):
        if self.flag not in (1, 3):
            raise CodecError(f"Outer flag {self.flag} has not been observed")
        return codec.decode_frame(self.payload)


def encode_packet(payload, flag=1, direction="c2s", timestamp=None):
    """BE16 size excluding prefix, flag; s2c adds BE64 timestamp before IVProto."""
    if direction not in ("c2s", "s2c"):
        raise CodecError("Outer packet direction must be c2s or s2c")
    if direction == "s2c":
        if timestamp is None:
            raise CodecError("Server packet requires its observed timestamp")
        payload = struct.pack(">Q", timestamp) + payload
    elif timestamp is not None:
        raise CodecError("Client packet does not carry the server timestamp")
    size = len(payload) + 1
    if not 0 < size <= 65535 or not 0 <= flag <= 255:
        raise CodecError("Outer packet length/flag out of range")
    return struct.pack(">HB", size, flag) + payload


def decode_packet_stream(raw, direction="c2s"):
    """Parse direction-aware framing. Flags alone do not determine direction."""
    if direction not in ("c2s", "s2c"):
        raise CodecError("Outer stream direction must be c2s or s2c")
    result = []
    position = 0
    while len(raw) - position >= 2:
        size = struct.unpack_from(">H", raw, position)[0]
        if size < 1:
            raise CodecError(f"Invalid outer length at offset {position}")
        if len(raw) - position < size + 2:
            break
        flag = raw[position + 2]
        payload = bytes(raw[position + 3:position + size + 2])
        timestamp = None
        if direction == "s2c":
            if len(payload) < 8:
                raise CodecError("Server packet lacks its eight-byte timestamp")
            timestamp = struct.unpack_from(">Q", payload)[0]
            payload = payload[8:]
        result.append(OuterPacket(flag, payload, size + 2, direction, timestamp))
        position += size + 2
    return result, bytes(raw[position:])


def readable(value):
    if isinstance(value, JSONValue):
        return value.parsed
    if isinstance(value, BoolValue):
        return bool(value)
    if isinstance(value, dict):
        return {str(k): readable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [readable(v) for v in value]
    return value


def source_record(path, project):
    return {"file": path.relative_to(project).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def extract_schema(project=PROJECT):
    project = Path(project)
    lua = project / "03-unpack/lua/device-luascripts"
    game_path = lua / "GameMsg.lua"
    opcode_path = lua / "GMsgNo.lua"
    original = game_path.read_text(encoding="utf-8-sig")
    # Types are quoted atoms; names also contain numeric keys used by fight commands.
    text = re.sub(r"--[^\n]*", "", original)
    pattern = re.compile(r'GameMsg\.map\["([^"\n]+)"\]\s*=\s*\{\s*\{([^}]*)\}\s*,\s*\{([^}]*)\}\s*,?\s*\}', re.S)
    matches = list(pattern.finditer(text))
    if len(matches) != len(re.findall(r"GameMsg\.map\[", text)):
        raise CodecError("Unparsed GameMsg declarations; refusing a partial schema")
    id_text = opcode_path.read_text(encoding="utf-8-sig")
    opcodes = {}
    opcode_lines = {}
    for match in re.finditer(r'^GMsgNo\["([^"\n]+)"\]\s*=\s*(\d+)', id_text, re.M):
        name, code = match[1], int(match[2])
        if name in opcodes or code in opcodes.values():
            raise CodecError("Duplicate opcode declaration")
        opcodes[name] = code
        opcode_lines[name] = id_text.count("\n", 0, match.start()) + 1
    sends = {}
    for path in sorted(lua.glob("*.lua")):
        if not path.exists():
            continue
        contents = path.read_text(encoding="utf-8-sig")
        contents = re.sub(r"--[^\n]*", "", contents)
        for match in re.finditer(r'(?:\bproto\s*=|:Send\s*\()\s*\{\s*"([^"\n]+)"', contents):
            sends.setdefault(match[1], []).append({"file": path.relative_to(project).as_posix(),
                                                   "line": contents.count("\n", 0, match.start()) + 1})
    schemas = []
    anomalies = []
    for match in matches:
        name = match[1]
        types = list(re.finditer(r'"([^"\n]*)"', match[2]))
        name_atoms = list(re.finditer(r'"([^"\n]*)"|(-?\d+)', match[3]))
        names = [atom[1] if atom[1] is not None else int(atom[2]) for atom in name_atoms]
        line = text.count("\n", 0, match.start()) + 1
        problems = []
        if len(types) != len(names):
            problems.append(f"type/field counts differ: {len(types)}/{len(names)}")
        if len(names) != len(set(names)):
            problems.append("duplicate field names; decode preserves indexed wire entries")
        fields = []
        for index, type_match in enumerate(types):
            field_type = type_match[1]
            parts = field_type.split("|")
            kind = parts[0]
            if len(parts) == 1 and kind not in ALL_PRIMITIVES:
                problems.append(f"unsupported primitive at index {index}: {field_type!r}")
            elif len(parts) > 1 and kind not in {"struts", "list", "array", "map"}:
                problems.append(f"unsupported composite at index {index}: {field_type!r}")
            field_name = names[index] if index < len(names) else None
            fields.append({"index": index, "name": field_name, "type": field_type,
                           "optional": True,
                           "source": {"file": game_path.relative_to(project).as_posix(),
                                      "line": text.count("\n", 0, match.start(2) + type_match.start()) + 1}})
        schema = {"name": name, "opcode": opcodes.get(name),
                  "kind": "message" if ":" in name else "structure",
                  "validation": "static_source_only", "observed_samples": [],
                  "client_send_literal": bool(sends.get(name)),
                  "send_sources": sends.get(name, []),
                  "source": {"file": game_path.relative_to(project).as_posix(), "line": line},
                  "opcode_source": {"file": opcode_path.relative_to(project).as_posix(),
                                    "line": opcode_lines.get(name)},
                  "fields": fields, "anomalies": problems}
        schemas.append(schema)
        if problems:
            anomalies.append({"name": name, "problems": problems})
    names_set = {s["name"] for s in schemas}
    schema_by_name = {s["name"]: s for s in schemas}
    if set(opcodes) != names_set:
        raise CodecError("Schema and opcode declarations do not form a complete bijection")
    for schema in schemas:
        for field in schema["fields"]:
            parts = field["type"].split("|")
            if parts[0] in {"list", "struts", "map"} and parts[1] not in names_set:
                problem = f"missing referenced structure at index {field['index']}: {parts[1]}"
                schema["anomalies"].append(problem)
                if not any(a["name"] == schema["name"] for a in anomalies):
                    anomalies.append({"name": schema["name"], "problems": schema["anomalies"]})
            elif parts[0] == "map" and len(parts) == 3 and parts[2] not in {
                    value["name"] for value in schema_by_name[parts[1]]["fields"]}:
                problem = f"map key missing from referenced structure at index {field['index']}: {parts[1]}.{parts[2]}"
                schema["anomalies"].append(problem)
                if not any(a["name"] == schema["name"] for a in anomalies):
                    anomalies.append({"name": schema["name"], "problems": schema["anomalies"]})
    sources = [source_record(lua / name, project) for name in
               ["GameMsg.lua", "GMsgNo.lua", "GameMsgMgr.lua", "ProtParse.lua", "MsgBuffer.lua", "BufferUtil.lua", "ComUtils.lua",
                "NetBase.lua", "NetListener.lua", "LoginProto.lua"]]
    document = {"format_version": 1, "target": "CrossCore CN 3.3.0 + device current Lua",
            "status": "static_recovered; capture_validation_pending",
            "sources": sources, "schema_count": len(schemas),
            "message_count": sum(s["kind"] == "message" for s in schemas),
            "structure_count": sum(s["kind"] == "structure" for s in schemas),
            "observed_endpoint_count": 0,
            "wire": {"transport": "business sockets Main/Fight",
                     "frame": "length:u16, opcode:u16, field_count:u8, indexed fields",
                     "length_includes_header": True, "sequence_number": "commented out in active Lua code",
                     "byte_order": "little (native Buffer BitConverter calls and observed flag-1 game TCP login frame); big is supported for comparison tests",
                     "string": "LE16 UTF-8 byte-length, followed by UTF-8 bytes (native Buffer verified)",
                     "json_type": "misnamed: ComUtils.table.Encode/Decode serialize a Lua table expression; safe parser only, never loadstring",
                     "native_buffer_source": {"file": "03-unpack/il2cpp/dump.cs", "lines": [207817, 207821, 207865, 207869]},
                     "outer_transport": "BE16 length excluding prefix, U8 flag, c2s IVProto or s2c BE64 timestamp + IVProto; observed flags 1/3 are not compression proof",
                     "field_index_base": 0, "optional_fields": "Lua nil omitted; false, zero and empty strings are included"},
            "schemas": schemas, "source_anomalies": anomalies}
    attach_observations(document, project)
    return document


def attach_observations(document, project):
    summary_path = Path(project) / "04-capture/decoded/tcp/validation-summary.json"
    if not summary_path.exists():
        return document
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    by_opcode = {entry["opcode"]: entry for entry in document["schemas"]}
    validated = 0
    for observed in summary["opcodes"]:
        entry = by_opcode.get(observed["opcode"])
        if entry is None or entry["name"] != observed["name"]:
            raise CodecError("Capture opcode/name differs from the current static schema")
        samples = []
        for sample in observed["samples"]:
            copied = dict(sample)
            for key in ("raw", "outer", "decoded"):
                copied[key] = "05-protocol/samples/tcp/" + copied[key]
                if not (Path(project) / copied[key]).is_file():
                    raise CodecError("Capture summary references a missing sample file")
            samples.append(copied)
        entry["observed_samples"] = samples
        entry["validation"] = "observed_decode_encode_byte_exact" if observed["round_trip_exact_count"] == observed["decoded_count"] else "observed_partial_validation"
        entry["observed"] = {k: v for k, v in observed.items() if k != "samples"}
        validated += 1
    document["observed_endpoint_count"] = validated
    document["capture_summary"] = {k: v for k, v in summary.items() if k != "opcodes"}
    document["capture_summary"]["source"] = summary_path.relative_to(project).as_posix()
    document["status"] = "static_recovered; observed samples attached independently"
    return document


class Reader:
    def __init__(self, data, config):
        self.data = memoryview(data)
        self.pos = 0
        self.config = config

    def take(self, length):
        if length < 0 or self.pos + length > len(self.data):
            raise CodecError(f"Truncated input at offset {self.pos}, requested {length}")
        value = bytes(self.data[self.pos:self.pos + length])
        self.pos += length
        return value

    def number(self, fmt):
        return struct.unpack(self.config.prefix + fmt, self.take(struct.calcsize(fmt)))[0]


class IVProtoCodec:
    def __init__(self, schema, config):
        self.config = config
        self.schemas = {s["name"]: s for s in schema["schemas"]}
        self.opcodes = {s["opcode"]: s for s in schema["schemas"]}
        self.config.prefix

    def pack(self, fmt, value):
        try:
            return struct.pack(self.config.prefix + fmt, value)
        except (struct.error, TypeError) as error:
            raise CodecError(f"Value {value!r} does not fit {fmt}") from error

    def string(self, value):
        if not isinstance(value, str):
            raise CodecError("String field requires a string")
        data = value.encode(self.config.string_encoding, "surrogateescape")
        fmt = {2: "H", 4: "I"}.get(self.config.string_length_bytes)
        if fmt is None:
            raise CodecError("Unsupported string length width")
        return self.pack(fmt, len(data)) + data

    def encode_value(self, field_type, value, depth):
        field_type = bracket_array(field_type)
        parts = field_type.split("|")
        kind = parts[0]
        if len(parts) > 1:
            if kind == "struts":
                return self.encode_struct(parts[1], value, depth + 1)
            if kind in ("list", "array"):
                if not isinstance(value, list):
                    raise CodecError(f"{kind} requires a list")
                items = [self.encode_struct(parts[1], item, depth + 1) if kind == "list"
                         else self.encode_value(parts[1], item, depth + 1) for item in value]
                return self.pack("H", len(items)) + b"".join(items)
            if kind == "map":
                if not isinstance(value, dict) or len(parts) != 3:
                    raise CodecError("Map requires a dictionary and a key-field declaration")
                items = [item for item in value.values() if isinstance(item, dict) and parts[2] in item]
                if len(items) >= 127:
                    raise CodecError("Lua map encoder requires fewer than 127 items")
                return self.pack("B", len(items)) + b"".join(
                    self.encode_struct(parts[1], item, depth + 1) for item in items)
            raise CodecError(f"Unsupported composite {field_type!r}")
        if kind in PRIMITIVES:
            if isinstance(value, FloatValue) and value.kind == kind:
                return value.raw
            return self.pack(PRIMITIVES[kind], value)
        if kind == "bool":
            return self.pack("B", value.raw if isinstance(value, BoolValue) else int(bool(value)))
        if kind == "string":
            return self.string(value)
        if kind == "json":
            if not isinstance(value, (JSONValue, dict, list)):
                raise CodecError("The source json type requires a Lua table")
            text = value.text if isinstance(value, JSONValue) else lua_literal(value)
            return self.string(text)
        raise CodecError(f"Unsupported source primitive {field_type!r}")

    def decode_value(self, field_type, reader, depth):
        field_type = bracket_array(field_type)
        parts = field_type.split("|")
        kind = parts[0]
        if len(parts) > 1:
            if kind == "struts":
                return self.decode_struct(parts[1], reader, depth + 1)
            if kind in ("list", "array"):
                count = reader.number("H")
                return [self.decode_struct(parts[1], reader, depth + 1) if kind == "list"
                        else self.decode_value(parts[1], reader, depth + 1) for _ in range(count)]
            if kind == "map" and len(parts) == 3:
                count = reader.number("B")
                result = {}
                for _ in range(count):
                    item = self.decode_struct(parts[1], reader, depth + 1)
                    if parts[2] not in item:
                        raise CodecError("Map item omitted its key field")
                    key = item[parts[2]]
                    if key in result:
                        raise CodecError("Duplicate map key cannot be round-tripped losslessly")
                    result[key] = item
                return result
            raise CodecError(f"Unsupported composite {field_type!r}")
        if kind in PRIMITIVES:
            fmt = PRIMITIVES[kind]
            raw = reader.take(struct.calcsize(fmt))
            value = struct.unpack(self.config.prefix + fmt, raw)[0]
            return FloatValue(value, raw, kind) if kind in ("float", "double") else value
        if kind == "bool":
            return BoolValue(reader.number("B"))
        if kind in ("string", "json"):
            fmt = {2: "H", 4: "I"}[self.config.string_length_bytes]
            size = reader.number(fmt)
            text = reader.take(size).decode(self.config.string_encoding, "surrogateescape")
            if kind == "string":
                return text
            try:
                return JSONValue(text, json.loads(text or "{}"))
            except json.JSONDecodeError:
                from config_codec import parse_lua_table
                try:
                    return JSONValue(text, lua_data(parse_lua_table(text)), "lua_table")
                except (ValueError, UnicodeError) as error:
                    raise CodecError("Invalid Lua-table string field") from error
        raise CodecError(f"Unsupported source primitive {field_type!r}")

    def encode_struct(self, name, data, depth=0):
        if depth > self.config.max_depth:
            raise CodecError("Structure recursion limit exceeded")
        if not isinstance(data, dict):
            raise CodecError(f"Structure {name} requires a dictionary")
        if name not in self.schemas:
            raise CodecError(f"Missing source structure {name}")
        schema = self.schemas[name]
        fields = schema["fields"]
        if isinstance(data, WireStruct):
            chosen = data.wire
        else:
            # Mirror Lua FormatKey: unknown keys are ignored, nil is absent.
            chosen = []
            for field in fields:
                key = field['name']
                if isinstance(key, int) and key not in data and str(key) in data:
                    key = str(key)  # Numeric table keys after a readable JSON round trip.
                if key is not None and key in data and data[key] is not None:
                    chosen.append((field['index'], data[key]))
        if len(chosen) > 255:
            raise CodecError("Too many indexed fields")
        out = [self.pack("B", len(chosen))]
        for index, value in chosen:
            field = fields[index]
            if field["name"] is None:
                raise CodecError(f"Source omitted field name at {name}:{index}")
            out.append(self.pack("B", index))
            out.append(self.encode_value(field["type"], value, depth))
        return b"".join(out)

    def decode_struct(self, name, reader, depth=0):
        if depth > self.config.max_depth:
            raise CodecError("Structure recursion limit exceeded")
        if name not in self.schemas:
            raise CodecError(f"Missing source structure {name}")
        schema = self.schemas[name]
        result = WireStruct()
        count = reader.number("B")
        seen = set()
        for _ in range(count):
            index = reader.number("B")
            if index in seen:
                raise CodecError(f"Repeated field index {index} in {name}")
            seen.add(index)
            if index >= len(schema["fields"]):
                raise CodecError(f"Unknown field index {index} in {name}; untyped fields cannot be skipped")
            field = schema["fields"][index]
            if field["name"] is None:
                raise CodecError(f"Unnamed source field {name}:{index}")
            value = self.decode_value(field["type"], reader, depth)
            result[field["name"]] = value
            result.wire.append((index, value))
        return result

    def encode_frame(self, name, data):
        if name not in self.schemas:
            raise CodecError(f"Unknown command {name}")
        body = self.encode_struct(name, data)
        length = len(body) + 4
        if length > self.config.max_frame_size:
            raise CodecError("Frame exceeds configured size limit")
        return self.pack("H", length) + self.pack("H", self.schemas[name]["opcode"]) + body

    def decode_frame(self, raw):
        reader = Reader(raw, self.config)
        length = reader.number("H")
        opcode = reader.number("H")
        if length != len(raw) or length < 5 or length > self.config.max_frame_size:
            raise CodecError(f"Invalid frame length {length}, input length {len(raw)}")
        if opcode not in self.opcodes:
            raise CodecError(f"Unknown opcode {opcode}")
        name = self.opcodes[opcode]["name"]
        fields = self.decode_struct(name, reader)
        if reader.pos != length:
            raise CodecError(f"Unconsumed frame bytes: {length - reader.pos}")
        return Frame(opcode, name, fields, length)

    def decode_stream(self, raw):
        """Return complete frames plus incomplete tail; raw must be a reassembled TCP stream."""
        frames = []
        pos = 0
        while len(raw) - pos >= 2:
            size = struct.unpack_from(self.config.prefix + "H", raw, pos)[0]
            if size < 5 or size > self.config.max_frame_size:
                raise CodecError(f"Invalid stream length at byte offset {pos}: {size}")
            if len(raw) - pos < size:
                break
            frame = self.decode_frame(raw[pos:pos + size])
            frames.append(frame)
            pos += size
        return frames, bytes(raw[pos:])


def synthetic_value(field_type, schemas, depth=0):
    field_type = bracket_array(field_type)
    parts = field_type.split("|")
    kind = parts[0]
    if len(parts) > 1:
        if kind == "struts":
            return synthetic_struct(parts[1], schemas, depth + 1) if depth < 2 else {}
        if kind == "list":
            return [synthetic_struct(parts[1], schemas, depth + 1)] if depth < 2 else []
        if kind == "array":
            return [synthetic_value(parts[1], schemas, depth + 1)]
        if kind == "map":
            item = synthetic_struct(parts[1], schemas, depth + 1) if depth < 2 else {}
            if parts[2] not in item:
                return {}
            return {item[parts[2]]: item}
    return {"byte": 7, "short": -123, "ushort": 1234, "int": -123456,
            "uint": 345678, "long": 2**40 + 5, "float": 1.5, "double": -2.25,
            "bool": False, "string": "synthetic-测试", "json": {"test_only": True, "n": 1}}[kind]


def synthetic_struct(name, schemas, depth=0):
    result = {}
    for field in schemas[name]["fields"]:
        if field["name"] is None:
            continue
        try:
            result[field["name"]] = synthetic_value(field["type"], schemas, depth)
        except KeyError:
            continue
    return result


def selftest(schema):
    summaries = []
    for order in ("little", "big"):
        codec = IVProtoCodec(schema, WireConfig(order))
        schemas = codec.schemas
        tested = 0
        for entry in schema["schemas"]:
            name = entry["name"]
            empty = codec.encode_frame(name, {})
            assert len(empty) == 5
            decoded = codec.decode_frame(empty)
            assert decoded.fields == {}
            if entry["anomalies"]:
                continue
            value = synthetic_struct(name, schemas)
            try:
                raw = codec.encode_frame(name, value)
                frame = codec.decode_frame(raw)
                assert codec.encode_frame(frame.name, frame.fields) == raw
                assert frame.readable()["fields"] == readable(value), f"Synthetic value mismatch in {name} ({order})"
                tested += 1
            except CodecError:
                # A referenced source anomaly can make a containing schema unsupported.
                continue
        heartbeat = codec.encode_frame("ClientProto:Heartbeat", {})
        expected = b"\x05\x00\x15\x04\x00" if order == "little" else b"\x00\x05\x04\x15\x00"
        assert heartbeat == expected
        query = codec.encode_frame("ClientProto:QueryAccount", {"account": "unit-test", "pwd": "", "SvnVersion": None})
        frame = codec.decode_frame(query)
        assert frame.readable()["fields"] == {"account": "unit-test", "pwd": ""}
        assert [i for i, _ in frame.fields.wire] == [0, 2]
        frames, tail = codec.decode_stream(heartbeat + query + heartbeat[:3])
        assert len(frames) == 2 and tail == heartbeat[:3]
        outer = encode_packet(heartbeat)
        packets, tail = decode_packet_stream(outer + encode_packet(query) + outer[:2])
        assert len(packets) == 2 and tail == outer[:2]
        assert packets[0].decode_inner(codec).name == "ClientProto:Heartbeat"
        assert encode_packet(packets[0].payload, packets[0].flag) == outer
        server = encode_packet(heartbeat, flag=3, direction="s2c", timestamp=1720000000000)
        server_packets, tail = decode_packet_stream(server, direction="s2c")
        assert not tail and server_packets[0].timestamp == 1720000000000
        assert server_packets[0].decode_inner(codec).name == "ClientProto:Heartbeat"
        assert encode_packet(server_packets[0].payload, server_packets[0].flag,
                             direction="s2c", timestamp=server_packets[0].timestamp) == server
        for malformed in (b"", heartbeat[:-1], heartbeat + b"\x00", heartbeat[:4] + b"\x01\xff"):
            try:
                codec.decode_frame(malformed)
            except CodecError:
                pass
            else:
                raise AssertionError("Malformed frame was accepted")
        # Preserve noncanonical field order and JSON whitespace instead of canonicalizing captures.
        test_schema = {"schemas": [{"name": "UnitTest", "opcode": 6550,
                                    "fields": [{"index": 0, "name": "a", "type": "json"},
                                               {"index": 1, "name": "b", "type": "bool"}]}]}
        test_codec = IVProtoCodec(test_schema, WireConfig(order))
        fields = WireStruct()
        fields.wire = [(1, BoolValue(2)), (0, JSONValue('{ "x": 1 }', {"x": 1}))]
        raw = test_codec.encode_frame("UnitTest", fields)
        decoded = test_codec.decode_frame(raw)
        assert test_codec.encode_frame(decoded.name, decoded.fields) == raw
        summaries.append({"byte_order": order, "all_empty_schema_frames": len(schema["schemas"]),
                          "representative_roundtrip_frames": tested,
                          "heartbeat_golden": True, "optional_fields": True,
                          "tcp_stream_partial_tail": True, "malformed_frames_rejected": True,
                          "outer_big_endian_wrapper": True,
                          "server_timestamp_wrapper": True,
                          "field_order_json_bool_preserved": True})
    return {"test_kind": "synthetic_schema_tests; not captured samples", "results": summaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--extract-schema", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--decode-stream", type=Path)
    parser.add_argument("--byte-order", choices=("little", "big"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = args.project / "05-protocol/endpoints.json"
    schema = extract_schema(args.project) if args.extract_schema or not destination.exists() else json.loads(destination.read_text(encoding="utf-8"))
    if args.extract_schema:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"schema_count": schema["schema_count"], "message_count": schema["message_count"],
                          "source_anomalies": len(schema["source_anomalies"])}, ensure_ascii=False))
    if args.selftest:
        result = selftest(schema)
        path = args.project / "05-protocol/schema-selftest.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    if args.decode_stream:
        if not args.byte_order or not args.output:
            parser.error("--decode-stream requires --byte-order and --output; decoded account data are not printed")
        codec = IVProtoCodec(schema, WireConfig(args.byte_order))
        frames, tail = codec.decode_stream(args.decode_stream.read_bytes())
        result = {"source": str(args.decode_stream.resolve()), "frames": [f.readable() for f in frames],
                  "incomplete_tail_bytes": len(tail), "validation": "decoded local input; verify capture provenance separately"}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"decoded_frame_count": len(frames), "incomplete_tail_bytes": len(tail)}))


if __name__ == "__main__":
    main()
