"""Offline TCP sequence reassembly and CrossCore outer-frame extraction.

Classic PCAP + Linux cooked v1/ Ethernet / raw IPv4; Python standard library.
No network access or Lua execution. Console output contains counts, not fields.
Outer framing observed in session1: big-endian uint16 length excluding the
length prefix, followed by one flag byte. Flag 1 contains a little-endian IVProto
frame for c2s. Native Packet.Decode confirms s2c additionally carries an
8-byte big-endian signed timestamp after the flag, followed by the same IVProto.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import socket
import struct
from typing import Iterator

MAX_STREAM_SPAN = 64 * 1024 * 1024


@dataclass
class Segment:
    sequence: int
    data: bytes
    packet_number: int
    timestamp_ns: int


def pcap_records(path: Path) -> tuple[int, Iterator[tuple[int, int, bytes, int]]]:
    raw = path.read_bytes()
    if len(raw) > 256 * 1024 * 1024:
        raise ValueError("PCAP exceeds the local processing limit")
    formats = {
        b"\xd4\xc3\xb2\xa1": ("<", 1000), b"\xa1\xb2\xc3\xd4": (">", 1000),
        b"\x4d\x3c\xb2\xa1": ("<", 1), b"\xa1\xb2\x3c\x4d": (">", 1),
    }
    if len(raw) < 24 or raw[:4] not in formats:
        raise ValueError("Expected a classic PCAP file")
    endian, fractional_multiplier = formats[raw[:4]]
    major, minor = struct.unpack_from(endian + "HH", raw, 4)
    if (major, minor) != (2, 4):
        raise ValueError("Unsupported PCAP version")
    linktype = struct.unpack_from(endian + "I", raw, 20)[0]

    def records() -> Iterator[tuple[int, int, bytes, int]]:
        position = 24
        number = 0
        while position < len(raw):
            if len(raw) - position < 16:
                raise ValueError("Truncated PCAP record header")
            seconds, fraction, captured, original = struct.unpack_from(endian + "IIII", raw, position)
            position += 16
            if captured > len(raw) - position:
                raise ValueError("Truncated PCAP record data")
            packet = raw[position:position + captured]
            position += captured
            number += 1
            yield number, seconds * 1_000_000_000 + fraction * fractional_multiplier, packet, original
    return linktype, records()


def ipv4_tcp(packet: bytes, linktype: int) -> dict | None:
    if linktype == 113:  # Linux cooked capture v1, 16-byte SLL header
        if len(packet) < 16 or struct.unpack_from(">H", packet, 14)[0] != 0x0800:
            return None
        offset = 16
    elif linktype == 1:
        if len(packet) < 14:
            return None
        ethertype = struct.unpack_from(">H", packet, 12)[0]
        offset = 14
        while ethertype in (0x8100, 0x88a8):
            if len(packet) < offset + 4:
                return None
            ethertype = struct.unpack_from(">H", packet, offset + 2)[0]
            offset += 4
        if ethertype != 0x0800:
            return None
    elif linktype in (101, 228):
        offset = 0
    else:
        raise ValueError(f"Unsupported link type: {linktype}")
    ip = packet[offset:]
    if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 6:
        return None
    header_length = (ip[0] & 15) * 4
    total_length = struct.unpack_from(">H", ip, 2)[0]
    if header_length < 20 or total_length < header_length + 20 or len(ip) < header_length + 20:
        return None
    fragment = struct.unpack_from(">H", ip, 6)[0]
    if fragment & 0x3fff:
        return {"fragmented": True}
    tcp = ip[header_length:min(total_length, len(ip))]
    source_port, destination_port, sequence, acknowledge = struct.unpack_from(">HHII", tcp)
    tcp_header_length = (tcp[12] >> 4) * 4
    if tcp_header_length < 20 or tcp_header_length > len(tcp):
        return None
    flags = tcp[13]
    return {
        "source": (socket.inet_ntoa(ip[12:16]), source_port),
        "destination": (socket.inet_ntoa(ip[16:20]), destination_port),
        "sequence": sequence, "acknowledge": acknowledge, "flags": flags,
        "payload": tcp[tcp_header_length:], "ip_truncated": len(ip) < total_length,
        "fragmented": False,
    }


def signed_sequence_distance(sequence: int, anchor: int) -> int:
    return ((sequence - anchor + (1 << 31)) & 0xffffffff) - (1 << 31)


def reassemble(segments: list[Segment]) -> dict:
    """Deduplicate overlapping TCP data, identify conflicting bytes and gaps."""
    if not segments:
        return {"chunks": [], "gaps": [], "unique_bytes": 0, "overlap_bytes": 0,
                "conflicting_bytes": 0, "segment_count": 0, "first_sequence": None}
    anchor = segments[0].sequence
    positioned = [(signed_sequence_distance(segment.sequence, anchor), segment) for segment in segments]
    first = min(position for position, _ in positioned)
    last = max(position + len(segment.data) for position, segment in positioned)
    span = last - first
    if span > MAX_STREAM_SPAN:
        raise ValueError("Sequence span exceeds the reassembly limit")
    data = bytearray(span)
    occupied = bytearray(span)
    overlap = 0
    conflicts = set()
    # Capture order decides conflict precedence, rather than silently preferring
    # longer retransmissions. Conflicting streams are excluded from frame parsing.
    for position, segment in positioned:
        start = position - first
        end = start + len(segment.data)
        if not any(occupied[start:end]):
            data[start:end] = segment.data
            occupied[start:end] = b"\x01" * len(segment.data)
        else:
            for index, byte in enumerate(segment.data, start):
                if occupied[index]:
                    overlap += 1
                    if data[index] != byte:
                        conflicts.add(index)
                else:
                    data[index] = byte
                    occupied[index] = 1
    chunks = []
    gaps = []
    position = 0
    while position < span:
        if not occupied[position]:
            end = occupied.find(b"\x01", position)
            if end < 0:
                end = span
            gaps.append({"offset": position, "length": end - position})
        else:
            end = occupied.find(b"\x00", position)
            if end < 0:
                end = span
            chunks.append({"offset": position, "data": bytes(data[position:end])})
        position = end
    return {
        "chunks": chunks, "gaps": gaps, "unique_bytes": sum(occupied),
        "overlap_bytes": overlap, "conflicting_bytes": len(conflicts),
        "segment_count": len(segments), "first_sequence": (anchor + first) & 0xffffffff,
    }


def outer_frames(stream: bytes, byte_order: str = "big") -> tuple[list[dict], bytes]:
    frames = []
    offset = 0
    while len(stream) - offset >= 2:
        length = int.from_bytes(stream[offset:offset + 2], byte_order)
        if length < 1:
            raise ValueError(f"Invalid outer length at offset {offset}")
        size = length + 2
        if len(stream) - offset < size:
            break
        raw = stream[offset:offset + size]
        frames.append({"offset": offset, "outer_length": length, "flag": raw[2],
                       "raw": raw, "payload": raw[3:]})
        offset += size
    return frames, stream[offset:]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def process_pcap(source: Path, output_dir: Path, samples_dir: Path, server_ip: str, server_port: int) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(parents=True, exist_ok=True)
    linktype, records = pcap_records(source)
    groups = defaultdict(list)
    epochs = Counter()
    seen_syn = {}
    connection_meta = {}
    counts = Counter()
    server = (server_ip, server_port)
    for packet_number, timestamp_ns, packet, original_length in records:
        counts["pcap_records"] += 1
        decoded = ipv4_tcp(packet, linktype)
        if decoded is None:
            continue
        if decoded["fragmented"]:
            counts["unsupported_ipv4_fragment_packets"] += 1
            continue
        if decoded["source"] != server and decoded["destination"] != server:
            continue
        counts["selected_tcp_packets"] += 1
        if decoded["ip_truncated"]:
            counts["truncated_selected_ip_packets"] += 1
        client = decoded["destination"] if decoded["source"] == server else decoded["source"]
        base = (client, server)
        # A repeated SYN with the same sequence is a retransmission; a different
        # SYN for the same endpoint tuple starts another connection epoch.
        if decoded["flags"] & 2 and not decoded["flags"] & 16:
            if base in seen_syn and seen_syn[base] != decoded["sequence"]:
                epochs[base] += 1
            seen_syn[base] = decoded["sequence"]
        connection = (base, epochs[base])
        direction = "s2c" if decoded["source"] == server else "c2s"
        meta = connection_meta.setdefault(connection, {
            "client": list(client), "server": list(server), "epoch": epochs[base],
            "first_packet": packet_number, "first_timestamp_ns": timestamp_ns,
            "capture_contains_client_syn": False,
        })
        if decoded["flags"] & 2 and not decoded["flags"] & 16:
            meta["capture_contains_client_syn"] = True
        if decoded["payload"]:
            counts["selected_payload_packets"] += 1
            sequence = (decoded["sequence"] + bool(decoded["flags"] & 2)) & 0xffffffff
            groups[(connection, direction)].append(Segment(sequence, decoded["payload"], packet_number, timestamp_ns))
    manifest = {"source": str(source.resolve()), "pcap_sha256": sha256(source.read_bytes()),
                "linktype": linktype, "packet_counts": dict(counts),
                "outer_frame_rule": "big-endian u16 length excluding prefix, u8 flag, c2s inner / s2c big-endian int64 timestamp + inner",
                "native_evidence": "Packet.Decode RVA 0x17C2994 copies/reverses 8 timestamp bytes at flag+1, then body at flag+9",
                "streams": []}
    for index, ((connection, direction), segments) in enumerate(groups.items(), 1):
        assembled = reassemble(segments)
        name = f"{source.stem}-stream{index:02d}-{direction}"
        stream_record = {"name": name, "direction": direction, **connection_meta[connection],
                         **{key: value for key, value in assembled.items() if key != "chunks"}, "chunks": []}
        for chunk_index, chunk in enumerate(assembled["chunks"], 1):
            stream = chunk["data"]
            chunk_name = name if len(assembled["chunks"]) == 1 else f"{name}-chunk{chunk_index:02d}"
            stream_file = chunk_name + ".stream.bin"
            (output_dir / stream_file).write_bytes(stream)
            chunk_record = {"file": stream_file, "stream_offset": chunk["offset"],
                            "bytes": len(stream), "sha256": sha256(stream), "frames": []}
            if assembled["conflicting_bytes"]:
                chunk_record["frame_error"] = "Conflicting TCP overlaps: frame parsing deliberately refused"
                stream_record["chunks"].append(chunk_record)
                continue
            try:
                frames, tail = outer_frames(stream)
                chunk_record["incomplete_tail_bytes"] = len(tail)
                if tail:
                    tail_file = chunk_name + ".incomplete-tail.bin"
                    (output_dir / tail_file).write_bytes(tail)
                    chunk_record["incomplete_tail_file"] = tail_file
                chunk_record["flag_counts"] = dict(Counter(str(frame["flag"]) for frame in frames))
                plain_inner = []
                for frame_index, frame in enumerate(frames, 1):
                    stem = f"{chunk_name}-frame{frame_index:04d}"
                    outer_file = stem + ".outer.bin"
                    (samples_dir / outer_file).write_bytes(frame["raw"])
                    frame_record = {"index": frame_index, "stream_offset": chunk["offset"] + frame["offset"],
                                    "outer_bytes": len(frame["raw"]), "flag": frame["flag"],
                                    "outer_file": outer_file, "sha256_outer": sha256(frame["raw"])}
                    # Packet.Encode sends flag + body; Packet.Decode on the
                    # receiving client strips flag + 8-byte server timestamp.
                    # Flag 3 is observed on s2c and is not a compression marker.
                    if frame["flag"] in (1, 3):
                        inner = frame["payload"]
                        if direction == "s2c":
                            if len(inner) < 8:
                                frame_record["decode_status"] = "missing server timestamp"
                                chunk_record["frames"].append(frame_record)
                                continue
                            frame_record["server_timestamp"] = int.from_bytes(inner[:8], "big", signed=True)
                            inner = inner[8:]
                        inner_file = stem + ".inner.bin"
                        (samples_dir / inner_file).write_bytes(inner)
                        frame_record["inner_file"] = inner_file
                        frame_record["inner_bytes"] = len(inner)
                        if len(inner) >= 4:
                            declared, opcode = struct.unpack_from("<HH", inner)
                            frame_record["inner_length"] = declared
                            frame_record["opcode"] = opcode
                            frame_record["inner_length_matches"] = declared == len(inner)
                            if declared == len(inner):
                                plain_inner.append(inner)
                    else:
                        frame_record["decode_status"] = "unknown flag payload preserved; no speculative decode"
                    chunk_record["frames"].append(frame_record)
                if plain_inner:
                    inner_stream_file = chunk_name + ".plain-inner.stream.bin"
                    (output_dir / inner_stream_file).write_bytes(b"".join(plain_inner))
                    chunk_record["plain_inner_stream_file"] = inner_stream_file
            except ValueError as exc:
                chunk_record["frame_error"] = str(exc)
            stream_record["chunks"].append(chunk_record)
        manifest["streams"].append(stream_record)
    manifest_path = output_dir / (source.stem + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sample_manifest = samples_dir / (source.stem + ".manifest.json")
    sample_manifest.write_bytes(manifest_path.read_bytes())
    return manifest


def validate_samples(manifest: dict, schema_path: Path, samples_dir: Path, output_dir: Path) -> dict:
    """Validate real captured frames with the separate static IVProto codec."""
    from protocol_codec import IVProtoCodec, WireConfig, encode_packet
    schema = json.loads(schema_path.read_text("utf-8"))
    codec = IVProtoCodec(schema, WireConfig(byte_order="little", max_frame_size=65535))
    stats = {
        "session": Path(manifest["source"]).stem, "pcap_sha256": manifest["pcap_sha256"],
        "schema_sha256": sha256(schema_path.read_bytes()),
        "validation": "real TCP capture -> sequence reassembly -> outer unwrap -> IVProto decode -> byte-exact encode",
        "wire_byte_order": "little", "string_encoding": "utf-8", "string_length_bytes": 2,
        "frames_total": 0, "frames_decoded": 0, "round_trip_exact": 0,
        "outer_round_trip_exact": 0,
        "max_observed_inner_bytes": 0, "failures": [], "opcodes": {},
    }
    for stream in manifest["streams"]:
        for chunk in stream["chunks"]:
            for item in chunk["frames"]:
                stats["frames_total"] += 1
                if "inner_file" not in item or not item.get("inner_length_matches"):
                    stats["failures"].append({"outer_file": item["outer_file"], "reason": "No valid unwrapped inner frame"})
                    continue
                raw = (samples_dir / item["inner_file"]).read_bytes()
                stats["max_observed_inner_bytes"] = max(stats["max_observed_inner_bytes"], len(raw))
                try:
                    frame = codec.decode_frame(raw)
                    stats["frames_decoded"] += 1
                    reencoded = codec.encode_frame(frame.name, frame.fields)
                    exact = reencoded == raw
                    original_outer = (samples_dir / item["outer_file"]).read_bytes()
                    reencoded_outer = encode_packet(
                        reencoded, flag=item["flag"], direction=stream["direction"],
                        timestamp=item.get("server_timestamp"),
                    )
                    outer_exact = reencoded_outer == original_outer
                    decoded_file = item["inner_file"].removesuffix(".inner.bin") + ".decoded.json"
                    result = frame.readable()
                    result["_capture"] = {
                        "pcap": manifest["source"], "pcap_sha256": manifest["pcap_sha256"],
                        "stream": stream["name"], "direction": stream["direction"],
                        "stream_offset": item["stream_offset"], "outer_file": item["outer_file"],
                        "inner_file": item["inner_file"], "flag": item["flag"],
                        "server_timestamp": item.get("server_timestamp"),
                    }
                    result["_validation"] = {"round_trip_exact": exact, "outer_round_trip_exact": outer_exact,
                                             "sha256_inner": sha256(raw), "sha256_outer": sha256(original_outer)}
                    (samples_dir / decoded_file).write_text(
                        json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n", encoding="utf-8"
                    )
                    opcode = stats["opcodes"].setdefault(str(frame.opcode), {
                        "opcode": frame.opcode, "name": frame.name,
                        "directions": [], "decoded_count": 0, "round_trip_exact_count": 0,
                        "samples": [],
                    })
                    opcode["decoded_count"] += 1
                    if stream["direction"] not in opcode["directions"]:
                        opcode["directions"].append(stream["direction"])
                    opcode["samples"].append({"raw": item["inner_file"], "outer": item["outer_file"],
                                              "decoded": decoded_file, "round_trip_exact": exact})
                    if exact:
                        stats["round_trip_exact"] += 1
                        opcode["round_trip_exact_count"] += 1
                    else:
                        stats["failures"].append({"inner_file": item["inner_file"], "opcode": frame.opcode,
                                                   "reason": "Reencoded frame differs from captured bytes"})
                    if outer_exact:
                        stats["outer_round_trip_exact"] += 1
                    else:
                        stats["failures"].append({"inner_file": item["inner_file"], "opcode": frame.opcode,
                                                   "reason": "Reencoded outer transport differs from captured bytes"})
                except Exception as exc:
                    stats["failures"].append({"inner_file": item["inner_file"], "opcode": item.get("opcode"),
                                               "reason": f"{type(exc).__name__}: {exc}"})
    stats["unique_decoded_opcodes"] = len(stats["opcodes"])
    stats["unique_round_trip_opcodes"] = sum(
        value["round_trip_exact_count"] > 0 for value in stats["opcodes"].values()
    )
    stats_path = output_dir / (stats["session"] + ".validation.json")
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (samples_dir / (stats["session"] + ".validation.json")).write_bytes(stats_path.read_bytes())
    print(json.dumps({key: stats[key] for key in (
        "session", "frames_total", "frames_decoded", "round_trip_exact", "outer_round_trip_exact",
        "unique_round_trip_opcodes", "max_observed_inner_bytes"
    )} | {"failure_count": len(stats["failures"])}))
    return stats


def self_test() -> None:
    # Segmentation, capture reordering, repeated bytes, and partial overlaps.
    segments = [Segment(106, b"ghij", 1, 1), Segment(100, b"abcdef", 2, 2),
                Segment(103, b"defghi", 3, 3), Segment(100, b"abcdef", 4, 4)]
    result = reassemble(segments)
    assert result["chunks"] == [{"offset": 0, "data": b"abcdefghij"}]
    assert result["overlap_bytes"] == 12 and result["conflicting_bytes"] == 0
    gap = reassemble([Segment(100, b"ab", 1, 1), Segment(104, b"ef", 2, 2)])
    assert gap["gaps"] == [{"offset": 2, "length": 2}] and len(gap["chunks"]) == 2
    conflict = reassemble([Segment(100, b"abcd", 1, 1), Segment(102, b"XX", 2, 2)])
    assert conflict["conflicting_bytes"] == 2
    wrapped = reassemble([Segment(0xfffffffe, b"ab", 1, 1), Segment(0, b"cd", 2, 2)])
    assert wrapped["chunks"][0]["data"] == b"abcd"
    inner = struct.pack("<HHB", 5, 1045, 0)
    raw = len(inner + b"\x01").to_bytes(2, "big") + b"\x01" + inner
    frames, tail = outer_frames(raw + raw + raw[:3])
    assert len(frames) == 2 and tail == raw[:3] and frames[0]["payload"] == inner
    print(json.dumps({"self_test": "passed", "cases": 5}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", nargs="*", type=Path)
    parser.add_argument("--server-ip", default="106.14.8.36")
    parser.add_argument("--server-port", type=int, default=6441)
    root = Path(__file__).resolve().parents[2]
    parser.add_argument("--output-dir", type=Path, default=root / "04-capture" / "decoded" / "tcp")
    parser.add_argument("--samples-dir", type=Path, default=root / "05-protocol" / "samples" / "tcp")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--validate-schema", type=Path, help="Decode real samples with protocol_codec and verify byte-exact encoding")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    validations = []
    for path in args.pcap:
        manifest = process_pcap(path, args.output_dir, args.samples_dir, args.server_ip, args.server_port)
        chunks = [chunk for stream in manifest["streams"] for chunk in stream["chunks"]]
        frames = [frame for chunk in chunks for frame in chunk["frames"]]
        print(json.dumps({"session": path.stem, "streams": len(manifest["streams"]),
                          "frames": len(frames), "flag_counts": dict(Counter(str(frame["flag"]) for frame in frames)),
                          "unique_opcodes": len({frame["opcode"] for frame in frames if "opcode" in frame}),
                          "inner_length_mismatches": sum(not frame["inner_length_matches"] for frame in frames if "inner_length_matches" in frame),
                          "gap_bytes": sum(gap["length"] for stream in manifest["streams"] for gap in stream["gaps"]),
                          "conflicting_bytes": sum(stream["conflicting_bytes"] for stream in manifest["streams"])}))
        if args.validate_schema:
            validations.append(validate_samples(manifest, args.validate_schema, args.samples_dir, args.output_dir))
    if validations:
        by_opcode = {}
        for session in validations:
            for key, opcode in session["opcodes"].items():
                merged = by_opcode.setdefault(key, {
                    "opcode": opcode["opcode"], "name": opcode["name"], "directions": [],
                    "decoded_count": 0, "round_trip_exact_count": 0, "sessions": [], "samples": [],
                })
                merged["decoded_count"] += opcode["decoded_count"]
                merged["round_trip_exact_count"] += opcode["round_trip_exact_count"]
                merged["sessions"].append(session["session"])
                merged["samples"].extend(opcode["samples"])
                for direction in opcode["directions"]:
                    if direction not in merged["directions"]:
                        merged["directions"].append(direction)
        summary = {
            "sessions": [session["session"] for session in validations],
            "frames_total": sum(session["frames_total"] for session in validations),
            "frames_decoded": sum(session["frames_decoded"] for session in validations),
            "round_trip_exact": sum(session["round_trip_exact"] for session in validations),
            "outer_round_trip_exact": sum(session["outer_round_trip_exact"] for session in validations),
            "unique_decoded_opcodes": len(by_opcode),
            "unique_round_trip_opcodes": sum(value["round_trip_exact_count"] > 0 for value in by_opcode.values()),
            "failure_count": sum(len(session["failures"]) for session in validations),
            "max_observed_inner_bytes": max(session["max_observed_inner_bytes"] for session in validations),
            "opcodes": sorted(by_opcode.values(), key=lambda value: value["opcode"]),
        }
        text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        (args.output_dir / "validation-summary.json").write_text(text, encoding="utf-8")
        (args.samples_dir / "validation-summary.json").write_text(text, encoding="utf-8")
        print(json.dumps({key: value for key, value in summary.items() if key != "opcodes"}))


if __name__ == "__main__":
    main()
