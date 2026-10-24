"""Experimental, build-locked water-pump capacity control for local solo play.

Only an existing RIP-relative constant reference in the running game's pump
simulation is changed. There is no executable-file edit, allocation, injected
code, startup hook, or persistence. Restarting the game restores 100 L/s.
Static tracing proves the nominal-flow calculation; gameplay effects have not
been validated. Intake, hose, priming and downstream restrictions still apply.
Importing this module and read_state() never write to the target process.
"""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import struct
import threading


GAME_PATH = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Groundwork Simulator\game.exe")
BUILD_ID = "26171350"
EXE_SHA256 = "6b3703c22c8b0f67a2d35089a22e9b202921b1f466821d3db16097d8a8ec5898"
INSTRUCTION_RVA = 0x668FAFA
CONTEXT_BEFORE = bytes.fromhex("0f57c0f30f5ac7f20f5905d6a75903f20f58056e46f001")
CONTEXT_AFTER = bytes.fromhex("660f28f8f20fc2f803660f28cf66410f54c8f2440f5dc066410f55f8660f56f9")
PRESETS = {
    100: (146407184, bytes.fromhex("f20f59050e045102")),
    200: (146412200, bytes.fromhex("f20f5905a6175102")),
    300: (163772048, bytes.fromhex("f20f59058efb5903")),
    500: (145832008, bytes.fromhex("f20f5905463d4802")),
    1000: (146401712, bytes.fromhex("f20f5905aeee5002")),
}
_LOCK = threading.RLock()
_HASH_CACHE = None
_API = None
_RECOVERY = None
INVALID_HANDLE = ctypes.c_void_p(-1).value
MAXDWORD = 0xFFFFFFFF
ERROR_NO_MORE_FILES = 18
ERROR_BAD_LENGTH = 24
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
THREAD_SUSPEND_RESUME = 0x0002
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
PAGE_EXECUTE_READWRITE = 0x40


class PumpTrainerError(ValueError):
    """A rejected operation or recoverable Windows API failure."""


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


def _api():
    global _API
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise PumpTrainerError("Live pump controls require 64-bit Windows and the 64-bit trainer.")
    if _API is not None:
        return _API
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    declarations = {
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
        "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)], wintypes.BOOL),
        "Module32FirstW": ([wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wintypes.BOOL),
        "Module32NextW": ([wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)], wintypes.BOOL),
        "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)], wintypes.BOOL),
        "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "ReadProcessMemory": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        "WriteProcessMemory": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        "VirtualProtectEx": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "FlushInstructionCache": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t], wintypes.BOOL),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "SuspendThread": ([wintypes.HANDLE], wintypes.DWORD),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "GetProcessIdOfThread": ([wintypes.HANDLE], wintypes.DWORD),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
    }
    for name, (args, result) in declarations.items():
        func = getattr(api, name)
        func.argtypes, func.restype = args, result
    _API = api
    return api


def _failure(action, code=None):
    code = ctypes.get_last_error() if code is None else code
    suffix = " Run trainer as administrator." if code == 5 else ""
    return PumpTrainerError("{} (Windows error {}).{}".format(action, code, suffix))


def _norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _snapshot(flags, pid=0):
    api = _api()
    for _ in range(4):
        handle = api.CreateToolhelp32Snapshot(flags, pid)
        if handle != INVALID_HANDLE:
            return handle
        if ctypes.get_last_error() != ERROR_BAD_LENGTH:
            break
    raise _failure("Could not inspect the running game")


def _entries(flags, structure, first, following, pid=0):
    api, handle = _api(), _snapshot(flags, pid)
    try:
        entry = structure()
        entry.dwSize = ctypes.sizeof(entry)
        ok = getattr(api, first)(handle, ctypes.byref(entry))
        while ok:
            yield structure.from_buffer_copy(entry)
            entry.dwSize = ctypes.sizeof(entry)
            ok = getattr(api, following)(handle, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != ERROR_NO_MORE_FILES:
            raise _failure("Could not finish inspecting the game", error)
    finally:
        api.CloseHandle(handle)


def _process_path(handle):
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not _api().QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
        raise _failure("Could not verify the game's full path")
    return buffer.value


def _find_target():
    api, matches = _api(), []
    for entry in _entries(2, PROCESSENTRY32W, "Process32FirstW", "Process32NextW"):
        if entry.szExeFile.lower() != "game.exe":
            continue
        handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, entry.th32ProcessID)
        if not handle:
            raise _failure("Could not identify a game.exe process; no change was made")
        try:
            if _norm(_process_path(handle)) == _norm(GAME_PATH):
                matches.append(entry.th32ProcessID)
        finally:
            api.CloseHandle(handle)
    if len(matches) > 1:
        raise PumpTrainerError("More than one Groundwork Simulator is running. Close the extra instance.")
    return matches[0] if matches else None


def _verify_file(force=False):
    global _HASH_CACHE
    try:
        before = GAME_PATH.stat()
        stamp = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if not force and _HASH_CACHE == stamp:
            return
        digest = hashlib.sha256()
        with GAME_PATH.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = GAME_PATH.stat()
        if stamp != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PumpTrainerError("The game updated during inspection. Restart the game and trainer.")
        if digest.hexdigest() != EXE_SHA256:
            raise PumpTrainerError("Live pump controls support only the verified Steam build 26171350. "
                                   "This executable is different; no change was made.")
        _HASH_CACHE = stamp
    except OSError as error:
        raise PumpTrainerError("Could not read the verified game executable: " + str(error)) from error


def _open_target(pid, write=False):
    flags = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    if write:
        flags |= PROCESS_VM_OPERATION | PROCESS_VM_WRITE
    handle = _api().OpenProcess(flags, False, pid)
    if not handle:
        raise _failure("Could not access the game")
    try:
        if _norm(_process_path(handle)) != _norm(GAME_PATH):
            raise PumpTrainerError("The selected process changed. No change was made.")
        modules = [entry for entry in _entries(0x18, MODULEENTRY32W, "Module32FirstW",
                                               "Module32NextW", pid)
                   if _norm(entry.szExePath) == _norm(GAME_PATH)]
        if len(modules) != 1 or not modules[0].modBaseAddr:
            raise PumpTrainerError("Could not uniquely verify the game's loaded executable.")
        module = modules[0]
        if module.modBaseSize <= max(value[0] for value in PRESETS.values()) + 8:
            raise PumpTrainerError("The game's loaded executable has an unsupported layout.")
        return handle, int(module.modBaseAddr)
    except BaseException:
        _api().CloseHandle(handle)
        raise


def _read(handle, address, size):
    buffer, count = ctypes.create_string_buffer(size), ctypes.c_size_t()
    if not _api().ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count)):
        raise _failure("Could not inspect the pump calculation")
    if count.value != size:
        raise PumpTrainerError("The game returned an incomplete memory read. No change was made.")
    return buffer.raw


def _verify_memory(handle, base, allow_unknown=False):
    address = base + INSTRUCTION_RVA
    raw = _read(handle, address - len(CONTEXT_BEFORE), len(CONTEXT_BEFORE) + 8 + len(CONTEXT_AFTER))
    if raw[:len(CONTEXT_BEFORE)] != CONTEXT_BEFORE or raw[-len(CONTEXT_AFTER):] != CONTEXT_AFTER:
        raise PumpTrainerError("The live pump code differs from the verified build. No change was made.")
    instruction = raw[len(CONTEXT_BEFORE):len(CONTEXT_BEFORE) + 8]
    current = next((level for level, (_, code) in PRESETS.items() if instruction == code), None)
    if current is None and not allow_unknown:
        raise PumpTrainerError("Another tool changed the pump calculation. No change was made.")
    for level, (rva, code) in PRESETS.items():
        if _read(handle, base + rva, 8) != struct.pack("<d", level / 1000.0):
            raise PumpTrainerError("A live pump constant differs from the verified build. No change was made.")
        if INSTRUCTION_RVA + 8 + struct.unpack("<i", code[4:])[0] != rva:
            raise PumpTrainerError("The trainer's capacity definitions are invalid.")
    return current, instruction


def _thread_ids(pid):
    return {entry.th32ThreadID for entry in _entries(4, THREADENTRY32, "Thread32First", "Thread32Next")
            if entry.th32OwnerProcessID == pid}


def _resume_all(suspended):
    failures = []
    api = _api()
    for tid, handle in reversed(list(suspended.items())):
        # A failed ResumeThread does not decrement the count; one retry is safe.
        if api.ResumeThread(handle) == MAXDWORD and api.ResumeThread(handle) == MAXDWORD:
            failures.append(tid)
            continue
        api.CloseHandle(handle)
        del suspended[tid]
    return failures


def _suspend_all(pid, suspended):
    api = _api()
    for _ in range(4):
        tids = _thread_ids(pid)
        if not tids:
            raise PumpTrainerError("The game closed before the change. No change was made.")
        for tid in sorted(tids - suspended.keys()):
            handle = api.OpenThread(THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION, False, tid)
            if not handle:
                raise _failure("Could not pause every game thread; no change was made")
            owner = api.GetProcessIdOfThread(handle)
            if owner != pid:
                error = ctypes.get_last_error()
                api.CloseHandle(handle)
                if not owner:
                    raise _failure("Could not verify a game thread; no change was made", error)
                raise PumpTrainerError("A game thread changed owners during preparation. No change was made.")
            if api.SuspendThread(handle) == MAXDWORD:
                error = ctypes.get_last_error()
                api.CloseHandle(handle)
                raise _failure("Could not pause every game thread; no change was made", error)
            suspended[tid] = handle
        verified = _thread_ids(pid)
        if verified and verified == set(suspended):
            return
        if not set(suspended).issubset(verified):
            raise PumpTrainerError("The game's threads changed during preparation. No change was made.")
    raise PumpTrainerError("The game kept starting new threads. No change was made; try again after loading.")


def _write_verified(handle, address, code):
    buffer, count = ctypes.create_string_buffer(code), ctypes.c_size_t()
    if not _api().WriteProcessMemory(handle, address, buffer, len(code), ctypes.byref(count)):
        raise _failure("Could not update the pump calculation")
    if count.value != len(code):
        raise PumpTrainerError("Windows reported an incomplete pump-code write.")
    if not _api().FlushInstructionCache(handle, address, len(code)):
        raise _failure("Could not refresh the pump calculation")
    if _read(handle, address, len(code)) != code:
        raise PumpTrainerError("The pump-code verification did not match.")


def _change_while_suspended(handle, address, previous, desired, recovery=None):
    api, old_protection = _api(), wintypes.DWORD()
    # Eight bytes are on one code page; VirtualProtectEx rounds only that page.
    if not api.VirtualProtectEx(handle, address, 8, PAGE_EXECUTE_READWRITE, ctypes.byref(old_protection)):
        raise _failure("Could not temporarily access the pump-code page; no change was made")
    restore_protection = old_protection.value
    if recovery is not None:
        if recovery.get("protection") is None:
            recovery["protection"] = restore_protection
        restore_protection = recovery["protection"]
        recovery["protection_restored"] = False
    problems = []
    restored = False
    try:
        try:
            _write_verified(handle, address, desired)
        except BaseException as error:
            problems.append(str(error))
            try:
                _write_verified(handle, address, previous)
                restored = True
            except BaseException as rollback_error:
                problems.append("Could not verify rollback: " + str(rollback_error))
    finally:
        ignored = wintypes.DWORD()
        protection_ok = api.VirtualProtectEx(handle, address, 8, restore_protection, ctypes.byref(ignored))
        if not protection_ok:
            problems.append("Windows could not restore the code-page protection.")
            if not restored:
                try:
                    _write_verified(handle, address, previous)
                    restored = True
                except BaseException as rollback_error:
                    problems.append("Could not verify rollback: " + str(rollback_error))
            protection_ok = api.VirtualProtectEx(handle, address, 8, restore_protection, ctypes.byref(ignored))
            if not protection_ok:
                problems.append("Code-page protection is still changed; restart the game.")
        if recovery is not None:
            recovery["protection_restored"] = bool(protection_ok)
    if problems:
        ending = " The previous capacity was restored." if restored else " Restart the game before continuing."
        raise PumpTrainerError(" ".join(problems) + ending)


def _live_capacity(handle, base):
    instruction = _read(handle, base + INSTRUCTION_RVA, 8)
    return next((level for level, (_, code) in PRESETS.items() if instruction == code), None)


def _discard_exited_recovery():
    """Release stale retained handles only after Windows confirms process exit."""
    global _RECOVERY
    if _RECOVERY is None:
        return False
    exit_code = wintypes.DWORD()
    api = _api()
    if not api.GetExitCodeProcess(_RECOVERY["handle"], ctypes.byref(exit_code)) or exit_code.value == 259:
        return False
    for handle in _RECOVERY["threads"].values():
        api.CloseHandle(handle)
    api.CloseHandle(_RECOVERY["handle"])
    _RECOVERY = None
    return True


def has_pending_recovery():
    """The UI must prevent closing while a live game has retained paused threads."""
    with _LOCK:
        _discard_exited_recovery()
        return _RECOVERY is not None


def _recovery_message():
    return ("The game is paused for safe pump-code recovery. Click Restore 100 L/s to repair "
            "the calculation and resume it. Keep this trainer open. If recovery keeps failing, "
            "close the game through Task Manager, then retry; unsaved progress may be lost.")


def _can_resume(recovery):
    if not recovery.get("protection_restored", True):
        return False
    try:
        return _live_capacity(recovery["handle"], recovery["base"]) is not None
    except (PumpTrainerError, OSError):
        return False


def read_state():
    """Read-only status; failures are returned as friendly status text."""
    with _LOCK:
        if has_pending_recovery():
            return {"running": True, "capacity_lps": None, "status": _recovery_message()}
        running, handle = False, None
        try:
            pid = _find_target()
            if pid is None:
                return {"running": False, "capacity_lps": None,
                        "status": "Start Groundwork Simulator to use experimental live pump controls."}
            running = True
            _verify_file()
            handle, base = _open_target(pid)
            current, _ = _verify_memory(handle, base)
            return {"running": True, "capacity_lps": current,
                    "status": "Experimental: all water pumps have {} L/s nominal capacity.".format(current)}
        except (PumpTrainerError, OSError) as error:
            return {"running": running, "capacity_lps": None, "status": str(error)}
        finally:
            if handle:
                _api().CloseHandle(handle)


def set_capacity(capacity_lps):
    """Apply an explicit preset to the running game. Never called automatically."""
    global _RECOVERY
    if isinstance(capacity_lps, bool) or not isinstance(capacity_lps, int) or capacity_lps not in PRESETS:
        raise PumpTrainerError("Choose 100, 200, 300, 500 or 1000 L/s.")
    with _LOCK:
        if has_pending_recovery():
            raise PumpTrainerError(_recovery_message())
        pid = _find_target()
        if pid is None:
            raise PumpTrainerError("Start Groundwork Simulator before changing live pump capacity.")
        _verify_file(force=True)
        handle, base = _open_target(pid, write=True)
        suspended, error, mutation_attempted = {}, None, False
        recovery = {"handle": handle, "base": base, "pid": pid, "threads": suspended,
                    "protection": None, "protection_restored": True, "previous": None}
        try:
            current, _ = _verify_memory(handle, base)
            if current != capacity_lps:
                _suspend_all(pid, suspended)
                _, previous = _verify_memory(handle, base)
                recovery["previous"] = previous
                mutation_attempted = True
                _change_while_suspended(handle, base + INSTRUCTION_RVA,
                                        previous, PRESETS[capacity_lps][1], recovery)
        except BaseException as caught:
            error = caught
        finally:
            if suspended and mutation_attempted and not _can_resume(recovery):
                _RECOVERY = recovery
            else:
                failed_threads = _resume_all(suspended)
                if failed_threads:
                    _RECOVERY = recovery
                else:
                    _api().CloseHandle(handle)
        if _RECOVERY is recovery:
            raise PumpTrainerError(_recovery_message()) from error
        if error is not None:
            raise error
        return {"summary": "Experimental water-pump capacity set to {} L/s for all pumps in this "
                           "running session. Restarting the game restores 100 L/s.".format(capacity_lps),
                "capacity_lps": capacity_lps, "experimental": True}


def restore_capacity():
    """Restore the game's normal 100 L/s nominal capacity for this session."""
    global _RECOVERY
    with _LOCK:
        if _discard_exited_recovery():
            return {"summary": "The previous game process has exited. Its retained handles were released. "
                               "The next launch uses the normal 100 L/s capacity.",
                    "capacity_lps": 100, "experimental": True}
        if _RECOVERY is None:
            return set_capacity(100)
        recovery = _RECOVERY
        handle, base = recovery["handle"], recovery["base"]
        error = None
        try:
            # Some threads may already have resumed after a partial ResumeThread
            # failure. Pause the entire verified process again before any edit.
            _suspend_all(recovery["pid"], recovery["threads"])
            if _norm(_process_path(handle)) != _norm(GAME_PATH):
                raise PumpTrainerError("The retained process identity no longer matches the game.")
            current, instruction = _verify_memory(handle, base, allow_unknown=True)
            previous = recovery["previous"] or PRESETS[100][1]
            if current != 100 or not recovery.get("protection_restored", True):
                _change_while_suspended(handle, base + INSTRUCTION_RVA,
                                        previous, PRESETS[100][1], recovery)
        except BaseException as caught:
            error = caught
        if not _can_resume(recovery):
            raise PumpTrainerError(_recovery_message()) from error
        actual = _live_capacity(handle, base)
        if actual not in PRESETS:
            raise PumpTrainerError(_recovery_message()) from error
        failed = _resume_all(recovery["threads"])
        if failed:
            raise PumpTrainerError(_recovery_message()) from error
        _api().CloseHandle(handle)
        _RECOVERY = None
        if error is not None:
            raise PumpTrainerError("The game resumed with a verified {} L/s setting, but restoration "
                                   "reported: {}".format(actual, error)) from error
        if actual != 100:
            raise PumpTrainerError("The game resumed safely at {} L/s. Try Restore 100 L/s again.".format(actual))
        return {"summary": "Normal 100 L/s water-pump capacity restored and all paused game threads resumed.",
                "capacity_lps": 100, "experimental": True}


if __name__ == "__main__":
    import json
    print(json.dumps(read_state(), indent=2))
