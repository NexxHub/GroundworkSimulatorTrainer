"""Local save/settings trainer for Groundwork Simulator 0.1.1-player.

No game executable patching, network operations, or process writes.
World edits preserve all untouched MessagePack bytes, including float widths.
"""
from pathlib import Path
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import math
import os
import re
import struct
import sys
import uuid
from datetime import datetime

VENDOR = Path(__file__).resolve().parent / "vendor"
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))
import msgpack
import zstandard

BASELINE = "cb194b4773c20aece152fdb526be7e441d7bf3a2"
CAMPAIGN = "groundwork-simulator-campaign-v1"
MAX_SAVE = 512 * 1024 * 1024
MAX_WORLD = 1024 * 1024 * 1024


class TrainerError(ValueError):
    pass


def default_paths():
    root = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    root = root / "groundwork-simulator" / "groundwork-simulator"
    return root / "data/saves/slots", root / "config/settings.ron"


def list_saves(save_dir):
    return sorted(p for p in Path(save_dir).glob("slot-*.groundwork-simulator")
                  if re.fullmatch(r"slot-\d+\.groundwork-simulator", p.name))


def is_game_running():
    if os.name != "nt":
        return False
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise TrainerError("Could not check whether the game is running. No files were changed.")
    try:
        item = ProcessEntry()
        item.dwSize = ctypes.sizeof(item)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(item))
        if not ok:
            raise TrainerError("Could not read the process list. No files were changed.")
        while ok:
            if item.szExeFile.lower() == "game.exe":
                return True
            ok = kernel.Process32NextW(snapshot, ctypes.byref(item))
        if ctypes.get_last_error() != 18:
            raise TrainerError("Process check did not finish. No files were changed.")
        return False
    finally:
        kernel.CloseHandle(snapshot)


def _require_closed():
    if is_game_running():
        raise TrainerError("Save and fully close Groundwork Simulator before applying or restoring. "
                           "A game.exe process is still running.")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, limit=MAX_SAVE):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise TrainerError("Missing file or unsupported file size: " + str(path))
    return path.read_bytes()


def _digest(envelope):
    from save_checksum import compute_checksum
    return compute_checksum(envelope)


def load_envelope(raw):
    try:
        e = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        if not isinstance(e, dict) or bytes(e.get("magic", [])) != b"GRNDW001":
            raise TrainerError("This is not a Groundwork Simulator save.")
        if type(e.get("format_version")) is not int or e["format_version"] != 1 or e.get("baseline") != BASELINE:
            raise TrainerError("Unsupported save version. This trainer supports build 26171350 (0.1.1-player).")
        if _digest(e) != bytes(e["checksum_sha256"]):
            raise TrainerError("Save integrity check failed. No edits are allowed.")
        e["compressed_world_payload"] = bytes(e["compressed_world_payload"])
        return e
    except TrainerError:
        raise
    except Exception as exc:
        raise TrainerError("Could not validate this save: " + str(exc)) from exc


def _campaign(envelope):
    try:
        c = json.loads(bytes(envelope["extension_payloads"][CAMPAIGN]))
        if type(c.get("version")) is not int or c["version"] != 3 or not isinstance(c.get("funds"), dict):
            raise ValueError("unsupported campaign version")
        if type(c["funds"].get("opening_balance_eur")) is not int:
            raise ValueError("missing opening balance")
        return c
    except (ValueError, KeyError, TypeError) as exc:
        raise TrainerError("This save does not contain a supported campaign account.") from exc


def _world(envelope):
    with zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(envelope["compressed_world_payload"])) as reader:
        world = reader.read(MAX_WORLD + 1)
    if len(world) > MAX_WORLD:
        raise TrainerError("World exceeds the supported size limit.")
    return world


def _unpacker(raw):
    return msgpack.Unpacker(io.BytesIO(raw), raw=False, strict_map_key=False,
                            max_buffer_size=MAX_WORLD + 1)


def _map_spans(raw):
    u = _unpacker(raw)
    count = u.read_map_header()
    found = {}
    for _ in range(count):
        key = u.unpack()
        start = u.tell()
        u.skip()
        if key in found:
            raise TrainerError("Duplicate field in save; editing refused.")
        found[key] = (start, u.tell())
    if u.tell() != len(raw):
        raise TrainerError("Unexpected trailing data in save structure.")
    return found


def _extract(raw, spans, key, default=None):
    if key not in spans:
        return default
    a, b = spans[key]
    return msgpack.unpackb(raw[a:b], raw=False, strict_map_key=False)


def inspect_save(path):
    e = load_envelope(_read(path))
    c = _campaign(e)
    raw = _world(e)
    spans = _map_spans(raw)
    machines = _extract(raw, spans, "machines", [])
    world_tools = _extract(raw, spans, "world_tools", {})
    fuels = [float(m["fuel_fraction"]) * 100 for m in machines if "fuel_fraction" in m]
    pumps = world_tools.get("water_pumps", [])
    return {"display_name": e.get("metadata", {}).get("display_name", Path(path).stem),
            "machine_count": len(machines), "pump_count": len(pumps),
            "opening_balance_eur": c["funds"]["opening_balance_eur"],
            "fuel_min_percent": min(fuels) if fuels else 0,
            "fuel_max_percent": max(fuels) if fuels else 0,
            "pump_flows_lps": [p.get("flow_m3s", 0) * 1000 for p in pumps]}


def _replace_spans(raw, replacements):
    chunks = []
    last = 0
    for start, end, value in sorted(replacements):
        if start < last or end < start:
            raise TrainerError("Overlapping edit refused.")
        chunks.extend((raw[last:start], value))
        last = end
    chunks.append(raw[last:])
    return b"".join(chunks)


def _refill_tokens(raw):
    """Walk only machine/fleet subtrees; preserve each original numeric width."""
    u = _unpacker(raw)
    changes = []

    def walk(depth=0):
        if depth > 64:
            raise TrainerError("Unsupported nesting depth in machine data.")
        token = raw[u.tell()]
        if 0x80 <= token <= 0x8f or token in (0xde, 0xdf):
            for _ in range(u.read_map_header()):
                key = u.unpack()
                if key == "fuel_fraction":
                    start = u.tell()
                    value = u.unpack()
                    end = u.tell()
                    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1:
                        raise TrainerError("Invalid machine fuel value; editing refused.")
                    marker = raw[start]
                    if marker == 0xca:
                        encoded = b"\xca" + struct.pack(">f", 1.0)
                    elif marker == 0xcb:
                        encoded = b"\xcb" + struct.pack(">d", 1.0)
                    else:
                        raise TrainerError("Unsupported machine fuel encoding.")
                    changes.append((start, end, encoded))
                else:
                    walk(depth + 1)
        elif 0x90 <= token <= 0x9f or token in (0xdc, 0xdd):
            for _ in range(u.read_array_header()):
                walk(depth + 1)
        else:
            u.skip()

    walk()
    if u.tell() != len(raw):
        raise TrainerError("Unexpected machine data.")
    return _replace_spans(raw, changes), len(changes)


def validate_options(options):
    if set(options) - {"add_money", "refill_fuel", "engine_kw", "hydraulic_flow"}:
        raise TrainerError("Unsupported trainer option.")
    money = options.get("add_money", 0)
    if type(money) is not int or not 0 <= money <= 1_000_000_000:
        raise TrainerError("Money to add must be a whole number from 0 to 1,000,000,000.")
    if type(options.get("refill_fuel", False)) is not bool:
        raise TrainerError("Invalid fuel option.")
    for name, low, high in [("engine_kw", 50, 300), ("hydraulic_flow", 1, 3)]:
        value = options.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not low <= value <= high):
            raise TrainerError(f"{name} must be between {low} and {high}.")


def edit_save_bytes(raw, options):
    validate_options(options)
    e = load_envelope(raw)
    summary = []
    amount = options.get("add_money", 0)
    if amount:
        c = _campaign(e)
        new_opening = c["funds"]["opening_balance_eur"] + amount
        if not 0 <= new_opening <= 2_000_000_000:
            raise TrainerError("Resulting opening balance is outside the supported range.")
        c["funds"]["opening_balance_eur"] = new_opening
        data = json.dumps(c, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        e["extension_payloads"][CAMPAIGN] = list(data)
        summary.append(f"Added EUR {amount:,} to the campaign opening balance.")
    if options.get("refill_fuel"):
        world = _world(e)
        spans = _map_spans(world)
        replacements = []
        count = 0
        for key in ("machines", "authored_fleet"):
            if key not in spans:
                continue
            start, end = spans[key]
            replacement, n = _refill_tokens(world[start:end])
            count += n
            replacements.append((start, end, replacement))
        if not count:
            raise TrainerError("No supported machine fuel tanks were found.")
        world = _replace_spans(world, replacements)
        e["compressed_world_payload"] = zstandard.ZstdCompressor(level=3).compress(world)
        summary.append(f"Filled machine fuel tanks (including {count} live/fleet records).")
    if not summary:
        return raw, []
    e["checksum_sha256"] = list(_digest(e))
    e["compressed_world_payload"] = list(e["compressed_world_payload"])
    result = msgpack.packb(e, use_bin_type=True)
    load_envelope(result)
    return result, summary


def _ron_block(text, name, within=None):
    start, end = within or (0, len(text))
    matches = list(re.finditer(r"\b" + re.escape(name) + r"\s*:\s*(?:[A-Za-z_][A-Za-z_0-9]*)?\s*\(", text[start:end]))
    if len(matches) != 1:
        raise TrainerError("Unsupported settings structure: " + name)
    begin = start + matches[0].end()
    depth, quoted, escaped = 1, False, False
    for i in range(begin, end):
        char = text[i]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return begin, i
    raise TrainerError("Unclosed settings block: " + name)


def _ron_number(text, name, bounds):
    a, b = bounds
    matches = list(re.finditer(r"\b" + re.escape(name) + r"\s*:\s*([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)(?=\s*,)", text[a:b]))
    if len(matches) != 1:
        raise TrainerError("Unsupported settings value: " + name)
    match = matches[0]
    return a + match.start(1), a + match.end(1), float(match.group(1))


def _settings_fields(raw):
    text = raw.decode("utf-8")
    hydraulic = _ron_block(text, "hydraulic_tuning")
    if _ron_number(text, "version", hydraulic)[2] != 9:
        raise TrainerError("Unsupported hydraulic settings version.")
    engine = _ron_block(text, "engine", hydraulic)
    return text, {"engine_kw": _ron_number(text, "powerLimitKw", engine),
                  "hydraulic_flow": _ron_number(text, "flowBudget", hydraulic)}


def inspect_settings(path):
    _, fields = _settings_fields(_read(path, 4 * 1024 * 1024))
    return {key: value[2] for key, value in fields.items()}


def edit_settings_bytes(raw, options):
    validate_options(options)
    text, fields = _settings_fields(raw)
    changes, summary = [], []
    for name, (a, b, old) in fields.items():
        value = options.get(name)
        if value is not None and float(value) != old:
            changes.append((a, b, repr(float(value))))
            label = "Hydraulic engine limit (kW)" if name == "engine_kw" else "Hydraulic flow budget"
            summary.append(f"{label}: {old:g} -> {value:g}.")
    for a, b, value in sorted(changes, reverse=True):
        text = text[:a] + value + text[b:]
    return text.encode("utf-8"), summary


def _atomic(path, data):
    path = Path(path)
    temp = path.with_name(path.name + ".trainer-" + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def _transaction(changes, backup_parent):
    _require_closed()
    for _, path, old, _ in changes:
        if path.is_symlink() or _read(path) != old:
            raise TrainerError("A file changed during editing. Reload it and try again.")
    backup_dir = Path(backup_parent) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"format": 1, "status": "prepared", "files": []}
    for label, path, old, new in changes:
        backup_name = label + ".original"
        _atomic(backup_dir / backup_name, old)
        manifest["files"].append({"kind": label, "path": str(path.resolve()),
                                  "backup": backup_name, "before_sha256": _sha(old),
                                  "after_sha256": _sha(new)})
    _atomic(backup_dir / "manifest.json", json.dumps(manifest, indent=2).encode())
    written = []
    try:
        for label, path, old, new in changes:
            _require_closed()
            if _read(path) != old:
                raise TrainerError("The game or another program changed a file during editing.")
            _atomic(path, new)
            written.append((path, old, new))
            if _read(path) != new:
                raise TrainerError("Write verification failed.")
        manifest["status"] = "complete"
        _atomic(backup_dir / "manifest.json", json.dumps(manifest, indent=2).encode())
    except Exception as exc:
        for path, old, new in reversed(written):
            if _read(path) == new:
                _atomic(path, old)
        raise TrainerError(f"Apply failed: {exc}. Originals are backed up in {backup_dir}") from exc
    return backup_dir


def apply_changes(save_path, settings_path, options):
    validate_options(options)
    _require_closed()
    changes, summary = [], []
    save_path, settings_path = Path(save_path), Path(settings_path)
    if options.get("add_money") or options.get("refill_fuel"):
        if save_path.suffix != ".groundwork-simulator":
            raise TrainerError("Choose a Groundwork Simulator save file.")
        old = _read(save_path)
        new, notes = edit_save_bytes(old, options)
        if new != old:
            changes.append(("save", save_path, old, new))
            summary.extend(notes)
    if options.get("engine_kw") is not None or options.get("hydraulic_flow") is not None:
        if settings_path.name != "settings.ron":
            raise TrainerError("Choose the game's settings.ron file.")
        old = _read(settings_path, 4 * 1024 * 1024)
        new, notes = edit_settings_bytes(old, options)
        if new != old:
            changes.append(("settings", settings_path, old, new))
            summary.extend(notes)
    if not changes:
        raise TrainerError("No changes selected, or the selected settings already match.")
    backup = _transaction(changes, save_path.parent / "trainer-backups")
    return {"backup_dir": str(backup), "summary": "\n".join(summary)}


def restore_backup(backup_dir):
    _require_closed()
    backup_dir = Path(backup_dir)
    manifest = json.loads(_read(backup_dir / "manifest.json", 65536))
    if manifest.get("format") != 1 or not 1 <= len(manifest.get("files", [])) <= 2:
        raise TrainerError("Not a supported trainer backup.")
    changes = []
    seen = set()
    for entry in manifest["files"]:
        kind = entry["kind"]
        path = Path(entry["path"])
        if kind not in ("save", "settings") or kind in seen or entry["backup"] != kind + ".original":
            raise TrainerError("Invalid backup manifest.")
        if not path.is_absolute() or (kind == "save" and path.suffix != ".groundwork-simulator") or (kind == "settings" and path.name != "settings.ron"):
            raise TrainerError("Invalid backup destination.")
        seen.add(kind)
        original = _read(backup_dir / entry["backup"])
        current = _read(path)
        if _sha(original) != entry["before_sha256"]:
            raise TrainerError("The backup is damaged. Restore refused.")
        if _sha(current) != entry["after_sha256"]:
            raise TrainerError("This file changed after the trainer edit. Automatic restore would overwrite newer progress. "
                               "Your backup is preserved for manual recovery: " + str(backup_dir))
        if kind == "save":
            load_envelope(original)
        else:
            _settings_fields(original)
        changes.append((kind, path, current, original))
    undo_dir = _transaction(changes, backup_dir.parent)
    return {"summary": "Original files restored. The replaced files were also backed up in " + str(undo_dir)}
