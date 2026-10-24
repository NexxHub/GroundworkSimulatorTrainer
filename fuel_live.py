"""Build-locked live fuel hold for the currently operated machine.

The verified native simulation subtracts fuel use, clamps the fraction to 0..1,
and stores it on the selected machine. Enabling this control substitutes the
already-loaded 1.0 constant for the subtraction result. The next simulation
tick refills that machine and subsequent ticks hold it at 100 percent. The
empty-tank path disables inputs but joins the same simulation path; it does
not skip the fuel store. Trailer/generator fuel is a separate system and is
not changed. Switch vehicles to replenish another machine.

Restoring normal consumption does not remove fuel already replenished. Fuel
levels can be persisted by the game's own saves. No executable-file edits,
process writes at import, allocation, code injection, or startup hooks.

Evidence: research/live_machine_fuel_patch.json and the associated native
disassembly. Static verification is complete; gameplay is not yet verified.
"""
import struct

import pump_live
from native_patch import NativePatchError, PatchManager


RVA = 0x65546DB
ORIGINAL = bytes.fromhex("f30f5cca")  # subss xmm1, xmm2
ENABLED = bytes.fromhex("410f28c8")   # movaps xmm1, xmm8 (xmm8.low = 1.0)
BEFORE_RVA = 0x65546C0
BEFORE = bytes.fromhex("488b80c02000004c69c1d0050000f3420f108400540500000f28c8")
AFTER = bytes.fromhex("f30f5fd9410f28c8f30f5dcbf3420f118c0054050000")
ONE_LOAD_RVA = 0x6553DAC
ONE_LOAD = bytes.fromhex("f3440f1005bbd40502")
ONE_RVA = 0x85B1270


def _decode(code):
    if code == ORIGINAL:
        return False
    if code == ENABLED:
        return True
    raise NativePatchError("Another tool changed the machine fuel calculation. No change was made.")


def _verify(handle, base):
    expected = ((BEFORE_RVA, BEFORE), (RVA + len(ORIGINAL), AFTER),
                (ONE_LOAD_RVA, ONE_LOAD), (ONE_RVA, struct.pack("<f", 1.0)))
    for rva, value in expected:
        if pump_live._read(handle, base + rva, len(value)) != value:
            raise NativePatchError("The live machine fuel code differs from the verified build. No change was made.")


_MANAGER = PatchManager("Machine fuel", RVA, ORIGINAL, _decode, _verify)


def read_state():
    state = _MANAGER.read()
    code = state.get("code")
    enabled = _decode(code) if code is not None else None
    if enabled is True:
        status = "Unlimited fuel ON — operated machine refills while simulation runs"
    elif enabled is False:
        status = "Normal machine fuel consumption"
    else:
        status = state.get("status", "")
    return {"running": bool(state.get("running")), "enabled": enabled,
            "status": status}


def set_enabled(enabled):
    if type(enabled) is not bool:
        raise NativePatchError("Fuel hold must be enabled or disabled.")
    if not enabled:
        return restore_fuel()
    result = _MANAGER.apply(ENABLED)
    result["summary"] = (
        "Live fuel hold enabled. The currently operated machine will refill to 100% "
        "when the simulation ticks. Switch machines to refill others. "
        "Fuel trailers and generators are not included."
    )
    return result


def restore_fuel():
    result = _MANAGER.restore()
    result["summary"] = "Normal machine fuel consumption restored. Fuel already replenished remains in the tank."
    return result


def has_pending_recovery():
    return _MANAGER.has_pending_recovery()
