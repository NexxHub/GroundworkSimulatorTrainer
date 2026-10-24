"""Live fixed company-money balance; no save/ledger edits or injected code.

The verified native balance() function supplies HUD balance and purchase/fuel
checks. Its normal ledger computation is restored exactly when disabled.
Costs still accrue in the underlying ledger; disabling/restarting reveals that
balance, which can be negative. This is deliberately not called an add-money
or permanent-money feature. Gameplay write verification is not yet performed.
"""
import hashlib
import struct
import pump_live as pump
from native_patch import PatchManager, NativePatchError

MoneyTrainerError = NativePatchError
FUNCTION_RVA = 0x45CD1D0
FUNCTION_SIZE = 625
ORIGINAL = bytes.fromhex('41574156565753')
TAIL_SHA256 = '01c72054d986bc4d27efc31a70bcbb7cf1b4452c9b7ec0380e0a6d6927449b2f'


def _encode(amount):
    if type(amount) is not int or not 0 <= amount <= 2_000_000_000:
        raise MoneyTrainerError('Money override must be a whole number from 0 to 2,000,000,000 EUR.')
    # Original seven-byte prologue is replaced by mov eax,imm32; ret; nop.
    # EAX assignment zero-extends to RAX and leaves stack/nonvolatile registers intact.
    return b'\xb8' + struct.pack('<I', amount) + b'\xc3\x90'


def _decode(code):
    if code == ORIGINAL:
        return None
    if len(code) == 7 and code[0] == 0xb8 and code[-2:] == b'\xc3\x90':
        amount = struct.unpack('<I', code[1:5])[0]
        if 0 <= amount <= 2_000_000_000:
            return amount
    raise MoneyTrainerError('The live money code is not a recognized supported setting. No change was made.')


def _verify_tail(handle, base):
    tail = pump._read(handle, base + FUNCTION_RVA + len(ORIGINAL), FUNCTION_SIZE - len(ORIGINAL))
    if hashlib.sha256(tail).hexdigest() != TAIL_SHA256:
        raise MoneyTrainerError('The live balance calculation differs from the verified game build. No change was made.')


_MANAGER = PatchManager('money override', FUNCTION_RVA, ORIGINAL, _decode, _verify_tail)


def read_state():
    state = _MANAGER.read()
    code = state.pop('code')
    state['amount'] = _decode(code) if code is not None else None
    state['enabled'] = code is not None and code != ORIGINAL
    if code is not None:
        state['status'] = ('Normal ledger balance is active.' if code == ORIGINAL else
                           'Session money override: EUR {:,}. Spending still affects the underlying balance.'.format(state['amount']))
    return state


def set_amount(amount):
    _MANAGER.apply(_encode(amount))
    return {'amount': amount, 'summary': 'Session money override set to EUR {:,}. Spending still affects the underlying balance; switching off or restarting reveals it.'.format(amount)}


def restore_money():
    _MANAGER.restore()
    return {'amount': None, 'summary': 'Normal ledger balance restored. The session money override is off.'}


def has_pending_recovery():
    return _MANAGER.has_pending_recovery()
