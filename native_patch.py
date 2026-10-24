"""Build-locked, reversible native instruction patches for the local game.

Uses pump_live's exact executable identity and checked Windows APIs. No file
edits, allocation, injected code, remote threads or startup hooks are used.
Writes occur only after all game threads are paused and their instruction
pointers are outside the replaced instruction range. Failed unsafe recovery
retains handles and pauses; the UI must keep running until recovery succeeds.
"""
import ctypes
from ctypes import wintypes
import struct
import pump_live as pump

NativePatchError = pump.PumpTrainerError
THREAD_GET_CONTEXT = 0x0008
CONTEXT_AMD64_CONTROL = 0x00100001
# Microsoft x64 CONTEXT: ContextFlags offset 48, Rip offset 248, size 1232,
# 16-byte alignment. See winnt.h CONTEXT (x86 64-bit), GetThreadContext docs.
CONTEXT_SIZE = 1232
_MANAGERS = []


def _context_api():
    api = pump._api()
    api.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    api.GetThreadContext.restype = wintypes.BOOL
    return api


def _instruction_pointer(thread_handle):
    api = _context_api()
    allocation = ctypes.create_string_buffer(CONTEXT_SIZE + 15)
    aligned = (ctypes.addressof(allocation) + 15) & ~15
    ctypes.c_uint32.from_address(aligned + 48).value = CONTEXT_AMD64_CONTROL
    if not api.GetThreadContext(thread_handle, aligned):
        raise pump._failure('Could not verify a paused game thread; no code was changed')
    return ctypes.c_uint64.from_address(aligned + 248).value


def _suspend_all(pid, suspended):
    api = pump._api()
    for _ in range(4):
        tids = pump._thread_ids(pid)
        if not tids:
            raise NativePatchError('The game closed before preparation. No code was changed.')
        for tid in sorted(tids - suspended.keys()):
            rights = pump.THREAD_SUSPEND_RESUME | pump.THREAD_QUERY_LIMITED_INFORMATION | THREAD_GET_CONTEXT
            handle = api.OpenThread(rights, False, tid)
            if not handle:
                raise pump._failure('Could not pause every game thread; no code was changed')
            if api.GetProcessIdOfThread(handle) != pid:
                api.CloseHandle(handle)
                raise NativePatchError('A game thread changed during preparation. No code was changed.')
            if api.SuspendThread(handle) == pump.MAXDWORD:
                error = ctypes.get_last_error()
                api.CloseHandle(handle)
                raise pump._failure('Could not pause every game thread; no code was changed', error)
            suspended[tid] = handle
        verified = pump._thread_ids(pid)
        if verified and verified == set(suspended):
            return
        if not set(suspended).issubset(verified):
            raise NativePatchError('The game threads changed during preparation. No code was changed.')
    raise NativePatchError('The game is starting new threads. Wait for loading to finish and retry.')


def _exclude_instruction_pointers(suspended, address, length):
    for handle in suspended.values():
        rip = _instruction_pointer(handle)
        if address <= rip < address + length:
            raise NativePatchError('A game thread is currently using this calculation. No code was changed; retry.')


def _write(handle, address, code):
    api = pump._api()
    buffer = ctypes.create_string_buffer(code)
    count = ctypes.c_size_t()
    if not api.WriteProcessMemory(handle, address, buffer, len(code), ctypes.byref(count)):
        raise pump._failure('Could not update the live calculation')
    if count.value != len(code):
        raise NativePatchError('Windows reported an incomplete instruction write.')
    if not api.FlushInstructionCache(handle, address, len(code)):
        raise pump._failure('Could not refresh the live calculation')
    if pump._read(handle, address, len(code)) != code:
        raise NativePatchError('The live instruction did not match after writing.')


class PatchManager:
    def __init__(self, label, rva, original, decode, verify_tail):
        if not original or rva < 0 or (rva & 4095) + len(original) > 4096:
            raise ValueError('The patch must fit on one code page.')
        self.label, self.rva, self.original = label, rva, bytes(original)
        self.decode, self.verify_tail = decode, verify_tail
        self.recovery = None
        _MANAGERS.append(self)

    def _message(self):
        return ('The game is paused for safe ' + self.label + ' recovery. Use this feature\'s Restore button '
                'to repair the calculation and resume it. Keep the trainer open. If recovery keeps failing, '
                'close the game in Task Manager, then retry; unsaved progress may be lost.')

    def _discard_exited(self):
        if self.recovery is None:
            return False
        api, code = pump._api(), wintypes.DWORD()
        if not api.GetExitCodeProcess(self.recovery['handle'], ctypes.byref(code)) or code.value == 259:
            return False
        for handle in self.recovery['threads'].values():
            api.CloseHandle(handle)
        api.CloseHandle(self.recovery['handle'])
        self.recovery = None
        return True

    def has_pending_recovery(self):
        with pump._LOCK:
            self._discard_exited()
            return self.recovery is not None

    def _verify(self, handle, base, allow_unknown=False):
        self.verify_tail(handle, base)
        code = pump._read(handle, base + self.rva, len(self.original))
        if not allow_unknown:
            self.decode(code)
        return code

    def read(self):
        with pump._LOCK:
            if self.has_pending_recovery():
                return {'running': True, 'code': None, 'status': self._message()}
            running, handle = False, None
            try:
                pid = pump._find_target()
                if pid is None:
                    return {'running': False, 'code': None, 'status': 'Start Groundwork Simulator to use ' + self.label + '.'}
                running = True
                pump._verify_file()
                handle, base = pump._open_target(pid)
                code = self._verify(handle, base)
                return {'running': True, 'code': code, 'status': self.label + ' is ready.'}
            except (NativePatchError, OSError) as error:
                return {'running': running, 'code': None, 'status': str(error)}
            finally:
                if handle:
                    pump._api().CloseHandle(handle)

    def _change(self, recovery, previous, desired):
        api, old = pump._api(), wintypes.DWORD()
        handle, address = recovery['handle'], recovery['base'] + self.rva
        length = len(self.original)
        if not api.VirtualProtectEx(handle, address, length, pump.PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise pump._failure('Could not temporarily access the code page; no code was changed')
        if recovery['protection'] is None:
            recovery['protection'] = old.value
        recovery['protection_restored'] = False
        problems = []
        rolled_back = False
        try:
            try:
                _write(handle, address, desired)
            except BaseException as error:
                problems.append(str(error))
                try:
                    _write(handle, address, previous)
                    rolled_back = True
                except BaseException as rollback:
                    problems.append('Rollback could not be verified: ' + str(rollback))
        finally:
            ignored = wintypes.DWORD()
            okay = api.VirtualProtectEx(handle, address, length, recovery['protection'], ctypes.byref(ignored))
            if not okay:
                problems.append('The original code-page protection could not be restored.')
                if not rolled_back:
                    try:
                        _write(handle, address, previous)
                        rolled_back = True
                    except BaseException as rollback:
                        problems.append('Rollback could not be verified: ' + str(rollback))
                okay = api.VirtualProtectEx(handle, address, length, recovery['protection'], ctypes.byref(ignored))
            recovery['protection_restored'] = bool(okay)
        if problems:
            raise NativePatchError(' '.join(problems))

    def _can_resume(self, recovery):
        if not recovery['protection_restored']:
            return False
        try:
            self._verify(recovery['handle'], recovery['base'])
            # Even after a failed write, only a complete recognized instruction
            # and a successful instruction-cache flush permit resuming.
            return bool(pump._api().FlushInstructionCache(recovery['handle'], recovery['base'] + self.rva, len(self.original)))
        except (NativePatchError, OSError):
            return False

    def apply(self, desired):
        if not isinstance(desired, bytes) or len(desired) != len(self.original):
            raise NativePatchError('Invalid replacement instruction length.')
        self.decode(desired)
        with pump._LOCK:
            if self.has_pending_recovery():
                raise NativePatchError(self._message())
            if pump.has_pending_recovery() or any(m is not self and m.has_pending_recovery() for m in _MANAGERS):
                raise NativePatchError('Resolve the pending live-feature recovery before making another change.')
            pid = pump._find_target()
            if pid is None:
                raise NativePatchError('Start Groundwork Simulator before changing ' + self.label + '.')
            pump._verify_file(force=True)
            handle, base = pump._open_target(pid, write=True)
            recovery = {'handle': handle, 'base': base, 'pid': pid, 'threads': {},
                        'protection': None, 'protection_restored': True, 'previous': None}
            error, attempted = None, False
            try:
                current = self._verify(handle, base)
                if current != desired:
                    _suspend_all(pid, recovery['threads'])
                    current = self._verify(handle, base)
                    _exclude_instruction_pointers(recovery['threads'], base + self.rva, len(self.original))
                    recovery['previous'] = current
                    attempted = True
                    self._change(recovery, current, desired)
            except BaseException as caught:
                error = caught
            finally:
                if recovery['threads'] and attempted and not self._can_resume(recovery):
                    self.recovery = recovery
                elif pump._resume_all(recovery['threads']):
                    self.recovery = recovery
                else:
                    pump._api().CloseHandle(handle)
            if self.recovery is recovery:
                raise NativePatchError(self._message()) from error
            if error is not None:
                raise error
            return {'summary': self.label + ' updated for this running session.', 'code': desired}

    def restore(self):
        with pump._LOCK:
            if self._discard_exited():
                return {'summary': 'The previous game exited. Its retained handles were released.', 'code': self.original}
            if self.recovery is None:
                return self.apply(self.original)
            recovery, error = self.recovery, None
            try:
                _suspend_all(recovery['pid'], recovery['threads'])
                if pump._norm(pump._process_path(recovery['handle'])) != pump._norm(pump.GAME_PATH):
                    raise NativePatchError('The retained process identity does not match the game.')
                code = self._verify(recovery['handle'], recovery['base'], allow_unknown=True)
                _exclude_instruction_pointers(recovery['threads'], recovery['base'] + self.rva, len(self.original))
                if code != self.original or not recovery['protection_restored']:
                    self._change(recovery, recovery['previous'] or self.original, self.original)
            except BaseException as caught:
                error = caught
            if not self._can_resume(recovery):
                raise NativePatchError(self._message()) from error
            actual = self._verify(recovery['handle'], recovery['base'])
            if pump._resume_all(recovery['threads']):
                raise NativePatchError(self._message()) from error
            pump._api().CloseHandle(recovery['handle'])
            self.recovery = None
            if error is not None:
                raise NativePatchError('The game resumed with verified code, but restoration reported: ' + str(error)) from error
            if actual != self.original:
                raise NativePatchError('The game resumed safely, but the override remains enabled. Try Restore again.')
            return {'summary': 'Normal ' + self.label + ' restored and all paused game threads resumed.', 'code': actual}
