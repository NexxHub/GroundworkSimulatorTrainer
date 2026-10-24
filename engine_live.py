"""Experimental live hydraulic engine power limit for the verified game build.

The native hydraulic EngineTuning power-limit load is overridden in place. RPM,
load, response time and hydraulic flow calculations continue normally. The
setting applies to machines using this hydraulic engine simulation; separate
truck/wheel-loader drivetrains are not changed. No configuration/save/executable
file is edited. Restoring or restarting returns the normal machine/tuning limit.
Importing this module and read_state() never write to the game. Gameplay write
verification has not been performed.
"""
import hashlib
import math
import struct

import pump_live as pump
from native_patch import PatchManager, NativePatchError

EngineTrainerError = NativePatchError
FUNCTION_RVA = 0x67CBC50
FUNCTION_END_RVA = 0x67CC04E
INSTRUCTION_RVA = 0x67CBCCE
ORIGINAL = bytes.fromhex('66410f6e10660f7ed1')
CONTEXT_SHA256 = '4cddf8fbcc9d2e7e89fb5cbc1d2d8202f6a905f09b37054e8de21d19b9dc3da8'
MIN_POWER_KW = 50.0
MAX_POWER_KW = 300.0


def _encode(power_kw):
    if isinstance(power_kw, bool) or not isinstance(power_kw, (int, float)):
        raise EngineTrainerError('Hydraulic engine power must be a number from 50 to 300 kW.')
    if not math.isfinite(power_kw) or not MIN_POWER_KW <= power_kw <= MAX_POWER_KW:
        raise EngineTrainerError('Hydraulic engine power must be a number from 50 to 300 kW.')
    # Replace movd xmm2,[r8]; movd ecx,xmm2 with mov ecx,imm32;
    # movd xmm2,ecx. Both leave the exact same input registers for the native
    # finite-value checks and engine simulation. No stack/register side effects.
    return b'\xb9' + struct.pack('<f', float(power_kw)) + bytes.fromhex('660f6ed1')


def _decode(code):
    if code == ORIGINAL:
        return None
    if len(code) == len(ORIGINAL) and code[:1] == b'\xb9' and code[5:] == bytes.fromhex('660f6ed1'):
        value = struct.unpack('<f', code[1:5])[0]
        if math.isfinite(value) and MIN_POWER_KW <= value <= MAX_POWER_KW:
            return value
    raise EngineTrainerError('The live hydraulic engine code is not a recognized supported setting. No change was made.')


def _verify_tail(handle, base):
    before = pump._read(handle, base + FUNCTION_RVA, INSTRUCTION_RVA - FUNCTION_RVA)
    after_start = INSTRUCTION_RVA + len(ORIGINAL)
    after = pump._read(handle, base + after_start, FUNCTION_END_RVA - after_start)
    if hashlib.sha256(before + after).hexdigest() != CONTEXT_SHA256:
        raise EngineTrainerError('The hydraulic engine calculation differs from the verified game build. No change was made.')


_MANAGER = PatchManager('hydraulic engine power', INSTRUCTION_RVA, ORIGINAL, _decode, _verify_tail)


def read_state():
    state = _MANAGER.read()
    code = state.pop('code')
    state['power_kw'] = _decode(code) if code is not None else None
    state['enabled'] = code is not None and code != ORIGINAL
    if code is not None:
        state['status'] = ('Normal hydraulic engine limits are active.' if code == ORIGINAL else
                           'Hydraulic engine power limit: {:g} kW.'.format(state['power_kw']))
    return state


def set_power_kw(power_kw):
    code = _encode(power_kw)
    value = _decode(code)
    _MANAGER.apply(code)
    return {'power_kw': value, 'experimental': True,
            'summary': 'Hydraulic engine power limit set to {:g} kW for this session. Separate truck and wheel-loader drivetrains are unchanged.'.format(value)}


def restore_engine():
    _MANAGER.restore()
    return {'power_kw': None, 'summary': 'Normal hydraulic engine limits restored. The session override is off.'}


def has_pending_recovery():
    return _MANAGER.has_pending_recovery()
