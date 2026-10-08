"""Export a small, verified resource subset without deserializing .NET objects.

Only local APK/device files are read. All outputs belong to the project directory.
Optional dependency: UnityPy, installed in 02-tools/python/site-packages.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import struct
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "02-tools/python/site-packages"))
ALGORITHM_SOURCE = "https://github.com/AXiX-official/CrossCore-Lua-Tool/blob/master/Assembly-CSharp/ABCustom.cs"


def exact(stream, count):
    value = stream.read(count)
    if len(value) != count:
        raise ValueError(f"Truncated input: expected {count}, received {len(value)}")
    return value


def u32(stream):
    return struct.unpack("<I", exact(stream, 4))[0]


def nrbf_string(stream):
    length = 0
    for shift in range(0, 35, 7):
        value = exact(stream, 1)[0]
        length |= (value & 127) << shift
        if not value & 128:
            if length > 4096:
                raise ValueError("NRBF string exceeds the expected ABCustom structure")
            return exact(stream, length).decode("utf-8")
    raise ValueError("Invalid NRBF 7-bit string length")


def expect(stream, value, label):
    if exact(stream, len(value)) != value:
        raise ValueError(f"Unexpected {label}; refusing arbitrary NRBF deserialization")


def read_abcustom(data):
    """Parse only the observed ABCustom { byte[] bytes; string abPath; } layout."""
    stream = io.BytesIO(data)
    expect(stream, struct.pack("<Biiii", 0, 1, -1, 1, 0), "SerializationHeader")
    expect(stream, b"\x0c", "BinaryLibrary record")
    library_id = u32(stream)
    library = nrbf_string(stream)
    if not library.startswith("Assembly-CSharp, Version="):
        raise ValueError("Unexpected ABCustom assembly identity")
    expect(stream, b"\x05", "ClassWithMembersAndTypes record")
    root_id = u32(stream)
    class_name = nrbf_string(stream)
    member_count = u32(stream)
    if root_id != 1 or class_name != "ABCustom" or member_count != 2:
        raise ValueError("Unexpected class or member count")
    members = [nrbf_string(stream) for _ in range(member_count)]
    if members != ["bytes", "abPath"]:
        raise ValueError("Unexpected ABCustom members")
    expect(stream, b"\x07\x01\x02", "primitive-byte-array / string member types")
    if u32(stream) != library_id:
        raise ValueError("Mismatched ABCustom library reference")
    expect(stream, b"\x09", "MemberReference record")
    array_ref = u32(stream)
    expect(stream, b"\x06", "BinaryObjectString record")
    path_id = u32(stream)
    ab_path = nrbf_string(stream)
    expect(stream, b"\x0f", "ArraySinglePrimitive record")
    array_id = u32(stream)
    array_length = u32(stream)
    expect(stream, b"\x02", "Byte primitive type")
    if array_id != array_ref or path_id == array_id:
        raise ValueError("Mismatched NRBF object references")
    offset = stream.tell()
    if offset + array_length + 1 != len(data):
        raise ValueError("ABCustom array length does not end immediately before MessageEnd")
    payload = exact(stream, array_length)
    expect(stream, b"\x0b", "MessageEnd record")
    if stream.read(1):
        raise ValueError("Unexpected data after NRBF MessageEnd")
    return payload, {"wrapper": "ABCustom", "array_offset": offset,
                     "array_length": array_length, "ab_path": ab_path,
                     "observed_offset_152": offset == 152, "tail_message_end_verified": True}


def sparse_xor(data):
    """ABCustom's symmetric sparse XOR, keyed by the actual byte-array length."""
    result = bytearray(data)
    key = len(result) % 254 + 1
    step = max(1, len(result) // 100)
    for pos in range(0, len(result), step):
        old = result[pos]
        new = old ^ key
        result[pos] = new
        key = (old + new) & 255
    return bytes(result)


def cstring(data, offset, limit=96):
    end = data.find(b"\0", offset, min(len(data), offset + limit))
    if end < 0:
        raise ValueError("Unterminated UnityFS header string")
    return data[offset:end].decode("ascii"), end + 1


def bundle_header(data, offset):
    if data[offset:offset + 8] != b"UnityFS\0":
        raise ValueError("Missing UnityFS signature")
    version = struct.unpack_from(">I", data, offset + 8)[0]
    if version not in (6, 7, 8):
        raise ValueError("Unsupported UnityFS format version")
    player, pos = cstring(data, offset + 12)
    engine, pos = cstring(data, pos)
    if not re.fullmatch(r"\d+\.\d+\.\d+[A-Za-z0-9.]*", engine):
        raise ValueError("Invalid Unity engine version; possible decoy header")
    declared, compressed, uncompressed, flags = struct.unpack_from(">QIII", data, pos)
    available = len(data) - offset
    if declared != available:
        raise ValueError(f"UnityFS declared size {declared} differs from remaining length {available}")
    if not 0 < compressed <= declared or not 0 < uncompressed < 256 * 1024 * 1024:
        raise ValueError("Invalid UnityFS block-info lengths")
    if flags & 63 not in (0, 1, 2, 3):
        raise ValueError("Unsupported UnityFS compression flag")
    return {"offset": offset, "format_version": version, "player_version": player,
            "engine_version": engine, "declared_size": declared,
            "compressed_block_info_size": compressed,
            "uncompressed_block_info_size": uncompressed, "flags": flags,
            "header_end": pos + 20}


def select_bundle(data):
    candidates = []
    for match in re.finditer(re.escape(b"UnityFS\0"), data[:4096]):
        offset = match.start()
        try:
            header = bundle_header(data, offset)
            candidates.append({"offset": offset, "valid": True, "header": header})
        except (ValueError, struct.error, UnicodeError) as error:
            candidates.append({"offset": offset, "valid": False, "reason": str(error)})
    valid = [c for c in candidates if c["valid"]]
    if not valid:
        raise ValueError(f"No valid UnityFS header: {candidates}")
    if len(valid) > 1:
        # Use actual parser success when more than one size/version-valid header exists.
        try:
            import UnityPy
            valid = [c for c in valid if list(UnityPy.load(data[c["offset"]:]).objects)]
        except ImportError:
            raise ValueError("Multiple valid bundle headers require UnityPy verification")
    if len(valid) != 1:
        raise ValueError("Ambiguous UnityFS header candidates")
    choice = valid[0]
    return data[choice["offset"]:], {"header": choice["header"], "header_candidates": candidates}


def bs_records(apk, member):
    """Walk a stored ZIP member by direct file seeks, without reading skipped payloads."""
    with zipfile.ZipFile(apk) as archive:
        info = archive.getinfo(member)
    if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1:
        raise ValueError("Direct BS traversal requires a non-encrypted ZIP_STORED member")
    with apk.open("rb") as stream:
        stream.seek(info.header_offset)
        local = exact(stream, 30)
        if local[:4] != b"PK\x03\x04":
            raise ValueError("Invalid ZIP local header")
        filename_size, extra_size = struct.unpack_from("<HH", local, 26)
        local_name = exact(stream, filename_size).decode("utf-8")
        if local_name != member:
            raise ValueError("ZIP local filename differs from directory metadata")
        stream.seek(extra_size, 1)
        start = stream.tell()
        end = start + info.file_size
        index = 0
        while stream.tell() < end:
            record_offset = stream.tell() - start
            if end - stream.tell() < 8:
                raise ValueError("Truncated BS record header")
            name_size = u32(stream)
            if not 0 < name_size <= 4096 or stream.tell() + name_size + 4 > end:
                raise ValueError("Invalid BS record name length")
            name = exact(stream, name_size).decode("utf-8")
            length = u32(stream)
            payload_offset = stream.tell() - start
            if stream.tell() + length > end:
                raise ValueError("BS record extends beyond its ZIP member")
            yield {"index": index, "name": name, "record_offset": record_offset,
                   "payload_offset": payload_offset, "length": length,
                   "absolute_payload_offset": start + payload_offset}, stream
            stream.seek(start + payload_offset + length)
            index += 1


def safe_name(name):
    name = re.sub(r"[<>:\"|?*\x00-\x1f]", "_", name.replace("\\", "/"))
    parts = [p.rstrip(" .") for p in name.split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("Empty TextAsset name")
    reserved = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
    return Path(*[("_" + p if reserved.match(p) else p) for p in parts])


def extract_lua(bundle, source, project):
    import UnityPy
    environment = UnityPy.load(bundle)
    output = project / "03-unpack/lua" / source
    count = 0
    total = 0
    assets = []
    for obj in environment.objects:
        if obj.type.name != "TextAsset":
            continue
        asset = obj.read()
        name = asset.m_Name
        script = asset.m_Script
        raw = script.encode("utf-8", "surrogateescape") if isinstance(script, str) else bytes(script)
        relative = safe_name(name)
        if relative.suffix.lower() != ".lua":
            relative = Path(str(relative) + ".lua")
        target = output / relative
        if target.exists() and target.read_bytes() != raw:
            target = target.with_name(target.stem + "__" + str(obj.path_id) + target.suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        count += 1
        total += len(raw)
        assets.append({"name": name, "path_id": obj.path_id,
                       "file": target.relative_to(project).as_posix(),
                       "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                       "lua_bytecode": raw.startswith(b"\x1bLua")})
    if not count:
        raise ValueError("UnityPy found no TextAssets in the Lua bundle")
    return {"unitypy_version": getattr(UnityPy, "__version__", "unknown"),
            "text_asset_count": count, "total_script_bytes": total, "assets": assets}


def write_bundle(data, source, project, wrapped=True):
    details = {"source_id": source, "source_sha256": hashlib.sha256(data).hexdigest(),
               "source_size": len(data)}
    if wrapped:
        payload, wrapper = read_abcustom(data)
        details.update(wrapper)
        transformed = sparse_xor(payload)
        if sparse_xor(transformed) != payload:
            raise ValueError("ABCustom sparse-XOR round-trip failed")
        details["sparse_xor"] = {"algorithm_source": ALGORITHM_SOURCE,
                                 "initial_key": len(payload) % 254 + 1,
                                 "step": max(1, len(payload) // 100), "round_trip": True}
    else:
        transformed = data
    bundle, header = select_bundle(transformed)
    details.update(header)
    path = project / "03-unpack/packs" / (source + ".bundle")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bundle)
    details.update({"bundle": path.relative_to(project).as_posix(), "bundle_size": len(bundle),
                    "bundle_sha256": hashlib.sha256(bundle).hexdigest()})
    if wrapped:
        try:
            details["lua"] = extract_lua(bundle, source, project)
        except Exception as error:
            details["lua_error"] = f"{type(error).__name__}: {error}"
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--apk", type=Path, default=PROJECT.parent / "crosscore_3.3.0/base.apk")
    args = parser.parse_args()
    project = args.project.resolve()
    results = []
    for member, record_name, source, wrapped in [
        ("assets/packs/8.bs", "luascripts", "apk-luascripts", True),
        ("assets/packs/2.bs", None, "apk-unityfs-sample", False),
    ]:
        try:
            for record, stream in bs_records(args.apk, member):
                if record_name is not None and record["name"] != record_name:
                    continue
                result = write_bundle(exact(stream, record["length"]), source, project, wrapped)
                result.update({"apk_member": member, "bs_record": record})
                results.append(result)
                break
            else:
                raise ValueError("Selected BS record not found")
        except Exception as error:
            results.append({"source_id": source, "error": f"{type(error).__name__}: {error}"})
    for name in ["luascripts", "fixluascripts"]:
        path = project / "01-device/files/Custom" / name
        if not path.exists():
            continue
        try:
            result = write_bundle(path.read_bytes(), "device-" + name, project)
            result["source_file"] = path.relative_to(project).as_posix()
            results.append(result)
        except Exception as error:
            results.append({"source_id": "device-" + name, "error": f"{type(error).__name__}: {error}"})
    report = {"scope": "Selected local APK records and device Custom Lua bundles only",
              "nrbf_deserialization": False, "official_network_requests": False, "results": results}
    destination = project / "03-unpack/resource-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in results:
        print(json.dumps({k: v for k, v in result.items() if k in
                          ("source_id", "error", "lua_error", "bundle_size", "header")}, ensure_ascii=False))
        if "lua" in result:
            print("TextAssets:", result["lua"]["text_asset_count"], "bytes:", result["lua"]["total_script_bytes"])
    if any("error" in r or "lua_error" in r for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
