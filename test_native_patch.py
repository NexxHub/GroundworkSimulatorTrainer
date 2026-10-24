"""All native patch tests are mocked; no real process is opened or written."""
import ctypes
import struct
import unittest
from unittest.mock import patch
import native_patch as native
import money_live as money
import pump_live as pump

BASE = 0x140000000
RVA = 0x100
ORIGINAL = b'ORIGINA'
DESIRED = b'DESIRED'


class FakeApi:
    def __init__(self):
        self.memory = ORIGINAL
        self.closed, self.resumed, self.writes, self.protections = [], [], [], []
        self.write_plan, self.protect_plan, self.resume_plan, self.flush_plan = [], [], [], []
        self.exit_code = 259

    def _next(self, name, default):
        values = getattr(self, name)
        return values.pop(0) if values else default

    def CloseHandle(self, handle):
        self.closed.append(handle); return True

    def ResumeThread(self, handle):
        self.resumed.append(handle); return self._next('resume_plan', 1)

    def VirtualProtectEx(self, handle, address, size, protection, old):
        self.protections.append(protection); old._obj.value = 0x20
        return self._next('protect_plan', True)

    def WriteProcessMemory(self, handle, address, buffer, length, count):
        data = buffer.raw[:length]; self.writes.append(data)
        behavior = self._next('write_plan', 'ok')
        if behavior == 'partial':
            self.memory = data[:2] + self.memory[2:]; count._obj.value = 2; return False
        if behavior == 'fail':
            count._obj.value = 0; return False
        self.memory = data; count._obj.value = length; return True

    def FlushInstructionCache(self, handle, address, length):
        return self._next('flush_plan', True)

    def GetExitCodeProcess(self, handle, out):
        out._obj.value = self.exit_code; return True


class NativePatchTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.patchers = []
        def start(obj, attr, **kwargs):
            p = patch.object(obj, attr, **kwargs);self.patchers.append(p);return p.start()
        start(native, '_MANAGERS', new=[])
        start(pump, '_api', return_value=self.api)
        start(pump, '_find_target', return_value=55)
        start(pump, '_verify_file')
        start(pump, '_open_target', return_value=(99, BASE))
        start(pump, '_process_path', return_value=str(pump.GAME_PATH))
        start(pump, 'has_pending_recovery', return_value=False)
        start(pump, '_read', side_effect=lambda h,a,n: self.api.memory)
        start(native, '_suspend_all', side_effect=lambda pid,threads: threads.update({7:107}))
        start(native, '_instruction_pointer', return_value=BASE+RVA+100)
        def decode(code):
            if code not in (ORIGINAL, DESIRED): raise native.NativePatchError('unknown instruction')
            return code
        self.manager = native.PatchManager('test feature', RVA, ORIGINAL, decode, lambda h,b: None)

    def tearDown(self):
        for p in reversed(self.patchers):p.stop()

    def test_success_writes_verifies_restores_protection_and_resumes(self):
        result = self.manager.apply(DESIRED)
        self.assertEqual(result['code'], DESIRED)
        self.assertEqual(self.api.memory, DESIRED)
        self.assertEqual(self.api.protections, [0x40,0x20])
        self.assertEqual(self.api.resumed, [107])
        self.assertEqual(self.api.closed, [107,99])
        self.assertIsNone(self.manager.recovery)

    def test_read_never_writes_or_suspends(self):
        result=self.manager.read()
        self.assertTrue(result['running']);self.assertEqual(result['code'],ORIGINAL)
        self.assertEqual(self.api.writes,[]);self.assertEqual(self.api.resumed,[])
        self.assertEqual(self.api.closed,[99])

    def test_existing_code_is_noop(self):
        self.manager.apply(ORIGINAL)
        self.assertEqual(self.api.writes,[]);self.assertEqual(self.api.resumed,[])

    def test_invalid_requested_code_never_opens_target(self):
        for value in (b'short',b'INVALID','DESIRED'):
            with self.subTest(value=value),self.assertRaises(native.NativePatchError):
                self.manager.apply(value)
        self.assertEqual(self.api.writes,[]);self.assertEqual(self.api.closed,[])

    def test_unknown_existing_code_refuses_write(self):
        self.api.memory=b'UNKNOWN'
        with self.assertRaises(native.NativePatchError):self.manager.apply(DESIRED)
        self.assertEqual(self.api.writes,[]);self.assertEqual(self.api.closed,[99])

    def test_rip_inside_patch_refuses_and_resumes(self):
        for off in (0,1,6):
            self.api.resumed.clear()
            with patch.object(native,'_instruction_pointer',return_value=BASE+RVA+off),self.assertRaisesRegex(native.NativePatchError,'currently using'):
                self.manager.apply(DESIRED)
            self.assertEqual(self.api.resumed,[107]);self.assertEqual(self.api.writes,[])

    def test_context_failure_refuses_and_resumes(self):
        with patch.object(native,'_instruction_pointer',side_effect=native.NativePatchError('context fail')),self.assertRaisesRegex(native.NativePatchError,'context fail'):
            self.manager.apply(DESIRED)
        self.assertEqual(self.api.resumed,[107]);self.assertEqual(self.api.writes,[])

    def test_partial_write_rolls_back_before_resuming(self):
        self.api.write_plan=['partial','ok']
        with self.assertRaises(native.NativePatchError):self.manager.apply(DESIRED)
        self.assertEqual(self.api.memory,ORIGINAL)
        self.assertEqual(self.api.writes,[DESIRED,ORIGINAL])
        self.assertEqual(self.api.resumed,[107]);self.assertIsNone(self.manager.recovery)

    def test_partial_write_failed_rollback_retains_paused_threads_then_recovers(self):
        self.api.write_plan=['partial','fail']
        with self.assertRaisesRegex(native.NativePatchError,'paused'):self.manager.apply(DESIRED)
        self.assertNotIn(self.api.memory,(ORIGINAL,DESIRED))
        self.assertEqual(self.api.resumed,[]);self.assertEqual(self.api.closed,[])
        self.assertTrue(self.manager.has_pending_recovery())
        self.manager.restore()
        self.assertEqual(self.api.memory,ORIGINAL);self.assertEqual(self.api.resumed,[107])
        self.assertIsNone(self.manager.recovery)

    def test_failed_page_restore_retains_pauses(self):
        self.api.protect_plan=[True,False,False]
        with self.assertRaisesRegex(native.NativePatchError,'paused'):self.manager.apply(DESIRED)
        self.assertEqual(self.api.memory,ORIGINAL);self.assertEqual(self.api.resumed,[])
        self.assertTrue(self.manager.recovery)
        self.manager.restore()
        self.assertEqual(self.api.resumed,[107]);self.assertIsNone(self.manager.recovery)

    def test_failed_final_cache_flush_retains_pauses(self):
        self.api.flush_plan=[True,False]
        with self.assertRaisesRegex(native.NativePatchError,'paused'):self.manager.apply(DESIRED)
        self.assertEqual(self.api.resumed,[]);self.assertTrue(self.manager.recovery)
        self.manager.restore();self.assertEqual(self.api.memory,ORIGINAL)

    def test_failed_resume_retains_handle_and_restore_retries(self):
        self.api.resume_plan=[pump.MAXDWORD,pump.MAXDWORD]
        with self.assertRaisesRegex(native.NativePatchError,'paused'):self.manager.apply(DESIRED)
        self.assertEqual(self.api.closed,[]);self.assertIn(7,self.manager.recovery['threads'])
        self.manager.restore()
        self.assertIsNone(self.manager.recovery);self.assertEqual(self.api.closed,[107,99])

    def test_confirmed_process_exit_releases_retained_handles(self):
        self.api.write_plan=['partial','fail']
        with self.assertRaises(native.NativePatchError):self.manager.apply(DESIRED)
        self.api.exit_code=0
        self.assertFalse(self.manager.has_pending_recovery())
        self.assertEqual(self.api.closed,[107,99]);self.assertEqual(self.api.resumed,[])

    def test_preparation_failure_resumes_already_paused(self):
        def fail(pid,threads):threads[7]=107;raise native.NativePatchError('pause failed')
        with patch.object(native,'_suspend_all',side_effect=fail),self.assertRaisesRegex(native.NativePatchError,'pause failed'):
            self.manager.apply(DESIRED)
        self.assertEqual(self.api.resumed,[107]);self.assertEqual(self.api.writes,[])

    def test_pending_other_patch_recovery_blocks_without_writes(self):
        other=native.PatchManager('other',RVA+100,ORIGINAL,self.manager.decode,lambda h,b:None)
        other.recovery={'handle':98,'threads':{}}
        with self.assertRaisesRegex(native.NativePatchError,'pending'):
            self.manager.apply(DESIRED)
        self.assertEqual(self.api.writes,[])


class ContextAndMoneyTests(unittest.TestCase):
    def test_aligned_context_layout_and_control_flags(self):
        class Api:
            def GetThreadContext(self,handle,address):
                self.address=address
                assert address%16==0
                assert ctypes.c_uint32.from_address(address+48).value==native.CONTEXT_AMD64_CONTROL
                ctypes.c_uint64.from_address(address+248).value=0x123456789ABC
                return True
        api=Api()
        with patch.object(native,'_context_api',return_value=api):
            self.assertEqual(native._instruction_pointer(7),0x123456789ABC)

    def test_money_encode_decode_boundaries(self):
        for amount in (0,1,105212,500000,2000000000):
            self.assertEqual(money._decode(money._encode(amount)),amount)
        self.assertIsNone(money._decode(money.ORIGINAL))
        for amount in (True,1.0,-1,2000000001,None,float('nan')):
            with self.assertRaises(native.NativePatchError):money._encode(amount)
        for code in (b'unknown',b'\xb8'+struct.pack('<I',2000000001)+b'\xc3\x90'):
            with self.assertRaises(native.NativePatchError):money._decode(code)

    def test_money_entry_ends_after_whole_original_instructions(self):
        self.assertEqual(money.ORIGINAL.hex(),'41574156565753')
        # New body is mov eax,imm32; ret; nop, exactly seven bytes.
        self.assertEqual(money._encode(105212),b'\xb8'+struct.pack('<I',105212)+b'\xc3\x90')


if __name__=='__main__':unittest.main(verbosity=2)
