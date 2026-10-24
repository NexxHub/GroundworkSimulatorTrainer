"""Pure mocked tests: never open or write a real game process."""
import struct
import unittest
from unittest.mock import patch
import pump_live as pump


class FakeApi:
    def __init__(self, protection_results=None):
        self.protection_results = iter(protection_results or [True, True])
        self.protections = []
        self.closed = []
        self.resumed = []
        self.suspended = []

    def VirtualProtectEx(self, handle, address, size, protection, old):
        self.protections.append((address, size, protection))
        old._obj.value = 0x20
        return next(self.protection_results)

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True

    def OpenThread(self, flags, inherit, tid):
        return tid + 100

    def SuspendThread(self, handle):
        self.suspended.append(handle)
        return 0

    def GetProcessIdOfThread(self, handle):
        return 55

    def GetExitCodeProcess(self, handle, code):
        code._obj.value = 259
        return True

    def ResumeThread(self, handle):
        self.resumed.append(handle)
        return 1


class PumpLiveTests(unittest.TestCase):
    def setUp(self):
        pump._RECOVERY = None

    def tearDown(self):
        # All retained handles in these tests are fake integers.
        pump._RECOVERY = None

    def test_all_presets_redirect_only_existing_constant(self):
        for level, (rva, code) in pump.PRESETS.items():
            self.assertEqual(code[:4], bytes.fromhex("f20f5905"))
            self.assertEqual(len(code), 8)
            self.assertEqual(pump.INSTRUCTION_RVA + 8 + struct.unpack("<i", code[4:])[0], rva)
        self.assertEqual(pump.PRESETS[100][1].hex(), "f20f59050e045102")

    def test_invalid_value_never_touches_windows(self):
        with patch.object(pump, "_api", side_effect=AssertionError("must not use Windows")):
            for value in [True, 0, 101, 200.0, "200", None]:
                with self.assertRaises(pump.PumpTrainerError):
                    pump.set_capacity(value)

    def test_success_restores_protection(self):
        api = FakeApi()
        with patch.object(pump, "_api", return_value=api), patch.object(pump, "_write_verified") as write:
            pump._change_while_suspended(99, 4096, b"original", b"modified")
        write.assert_called_once_with(99, 4096, b"modified")
        self.assertEqual(api.protections, [(4096, 8, 0x40), (4096, 8, 0x20)])

    def test_partial_write_or_flush_failure_rolls_back(self):
        api = FakeApi()
        error = pump.PumpTrainerError("write or flush failed")
        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_write_verified", side_effect=[error, None]) as write:
            with self.assertRaisesRegex(pump.PumpTrainerError, "previous capacity was restored"):
                pump._change_while_suspended(99, 4096, b"original", b"modified")
        self.assertEqual([call.args[2] for call in write.call_args_list], [b"modified", b"original"])
        self.assertEqual(api.protections[-1], (4096, 8, 0x20))

    def test_protection_restore_failure_rolls_back_then_retries(self):
        api = FakeApi([True, False, True])
        with patch.object(pump, "_api", return_value=api), patch.object(pump, "_write_verified") as write:
            with self.assertRaisesRegex(pump.PumpTrainerError, "previous capacity was restored"):
                pump._change_while_suspended(99, 4096, b"original", b"modified")
        self.assertEqual([call.args[2] for call in write.call_args_list], [b"modified", b"original"])
        self.assertEqual(api.protections[-2:], [(4096, 8, 0x20), (4096, 8, 0x20)])

    def test_new_threads_are_suspended_before_stable_snapshot(self):
        api, suspended = FakeApi(), {}
        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_thread_ids", side_effect=[{1, 2}, {1, 2, 3}, {1, 2, 3}, {1, 2, 3}]):
            pump._suspend_all(55, suspended)
            self.assertEqual(set(suspended), {1, 2, 3})
            self.assertEqual(api.suspended, [101, 102, 103])
            self.assertEqual(pump._resume_all(suspended), [])
        self.assertFalse(suspended)
        self.assertEqual(api.resumed, [103, 102, 101])

    def test_failed_suspend_always_resumes_already_paused_threads(self):
        api = FakeApi()

        def fail(pid, suspended):
            suspended[7] = 107
            raise pump.PumpTrainerError("Could not suspend another thread")

        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_find_target", return_value=55), \
             patch.object(pump, "_verify_file"), \
             patch.object(pump, "_open_target", return_value=(99, 0x140000000)), \
             patch.object(pump, "_verify_memory", return_value=(100, pump.PRESETS[100][1])), \
             patch.object(pump, "_suspend_all", side_effect=fail), \
             patch.object(pump, "_change_while_suspended") as change:
            with self.assertRaisesRegex(pump.PumpTrainerError, "Could not suspend"):
                pump.set_capacity(200)
        change.assert_not_called()
        self.assertEqual(api.resumed, [107])
        self.assertEqual(api.closed, [107, 99])

    def test_reused_thread_id_never_suspends_another_process(self):
        api, suspended = FakeApi(), {}
        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_thread_ids", return_value={7}), \
             patch.object(api, "GetProcessIdOfThread", return_value=999):
            with self.assertRaisesRegex(pump.PumpTrainerError, "changed owners"):
                pump._suspend_all(55, suspended)
        self.assertFalse(suspended)
        self.assertEqual(api.suspended, [])
        self.assertEqual(api.closed, [107])

    def test_mutation_failure_always_resumes_and_closes(self):
        api = FakeApi()

        def suspend(pid, suspended):
            suspended[7] = 107

        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_find_target", return_value=55), \
             patch.object(pump, "_verify_file"), \
             patch.object(pump, "_open_target", return_value=(99, 0x140000000)), \
             patch.object(pump, "_verify_memory", return_value=(100, pump.PRESETS[100][1])), \
             patch.object(pump, "_suspend_all", side_effect=suspend), \
             patch.object(pump, "_can_resume", return_value=True), \
             patch.object(pump, "_change_while_suspended", side_effect=pump.PumpTrainerError("rejected")):
            with self.assertRaisesRegex(pump.PumpTrainerError, "rejected"):
                pump.set_capacity(200)
        self.assertEqual(api.resumed, [107])
        self.assertEqual(api.closed, [107, 99])

    def test_existing_preset_does_not_suspend_or_write(self):
        api = FakeApi()
        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_find_target", return_value=55), \
             patch.object(pump, "_verify_file"), \
             patch.object(pump, "_open_target", return_value=(99, 0x140000000)), \
             patch.object(pump, "_verify_memory", return_value=(200, pump.PRESETS[200][1])), \
             patch.object(pump, "_suspend_all") as suspend, \
             patch.object(pump, "_change_while_suspended") as change:
            self.assertEqual(pump.set_capacity(200)["capacity_lps"], 200)
        suspend.assert_not_called()
        change.assert_not_called()
        self.assertEqual(api.closed, [99])

    def test_failed_write_and_failed_rollback_keep_unknown_code_paused(self):
        api = FakeApi()

        def suspend(pid, suspended):
            suspended[7] = 107

        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_find_target", return_value=55), \
             patch.object(pump, "_verify_file"), \
             patch.object(pump, "_open_target", return_value=(99, 0x140000000)), \
             patch.object(pump, "_verify_memory", return_value=(100, pump.PRESETS[100][1])), \
             patch.object(pump, "_suspend_all", side_effect=suspend), \
             patch.object(pump, "_live_capacity", return_value=None), \
             patch.object(pump, "_write_verified", side_effect=pump.PumpTrainerError("write failed")) as write:
            with self.assertRaisesRegex(pump.PumpTrainerError, "paused for safe"):
                pump.set_capacity(200)
            self.assertTrue(pump.has_pending_recovery())
            self.assertIn("Restore 100", pump.read_state()["status"])
        self.assertEqual(write.call_count, 2)
        self.assertEqual(api.resumed, [])
        self.assertEqual(api.closed, [])
        self.assertEqual(pump._RECOVERY["threads"], {7: 107})

    def test_unknown_or_unreadable_final_instruction_is_not_resumable(self):
        recovery = {"handle": 99, "base": 0x140000000, "protection_restored": True}
        with patch.object(pump, "_read", return_value=b"corrupt!"):
            self.assertFalse(pump._can_resume(recovery))
        with patch.object(pump, "_read", side_effect=pump.PumpTrainerError("read failed")):
            self.assertFalse(pump._can_resume(recovery))

    def test_repeated_resume_failure_retains_handle_for_recovery(self):
        api, suspended = FakeApi(), {7: 107}
        with patch.object(pump, "_api", return_value=api), \
             patch.object(api, "ResumeThread", return_value=pump.MAXDWORD) as resume:
            self.assertEqual(pump._resume_all(suspended), [7])
        self.assertEqual(resume.call_count, 2)
        self.assertEqual(suspended, {7: 107})
        self.assertEqual(api.closed, [])

    def _pending(self):
        pump._RECOVERY = {"handle": 99, "base": 0x140000000, "pid": 55,
                          "threads": {7: 107}, "protection": 0x20,
                          "protection_restored": True, "previous": pump.PRESETS[100][1]}

    def test_explicit_recovery_restores_default_then_resumes(self):
        self._pending()
        api = FakeApi()
        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_suspend_all") as suspend, \
             patch.object(pump, "_process_path", return_value=str(pump.GAME_PATH)), \
             patch.object(pump, "_verify_memory", return_value=(None, b"corrupt!")), \
             patch.object(pump, "_live_capacity", return_value=100), \
             patch.object(pump, "_change_while_suspended") as change:
            result = pump.restore_capacity()
        suspend.assert_called_once()
        self.assertEqual(change.call_args.args[3], pump.PRESETS[100][1])
        self.assertEqual(result["capacity_lps"], 100)
        self.assertEqual(api.resumed, [107])
        self.assertEqual(api.closed, [107, 99])
        self.assertIsNone(pump._RECOVERY)

    def test_recovery_does_not_report_success_until_resume_works(self):
        self._pending()
        api = FakeApi()
        with patch.object(pump, "_api", return_value=api), \
             patch.object(pump, "_suspend_all"), \
             patch.object(pump, "_process_path", return_value=str(pump.GAME_PATH)), \
             patch.object(pump, "_verify_memory", return_value=(100, pump.PRESETS[100][1])), \
             patch.object(pump, "_live_capacity", return_value=100), \
             patch.object(api, "ResumeThread", return_value=pump.MAXDWORD):
            with self.assertRaisesRegex(pump.PumpTrainerError, "paused for safe"):
                pump.restore_capacity()
            self.assertTrue(pump.has_pending_recovery())
        self.assertEqual(api.closed, [])
        self.assertEqual(pump._RECOVERY["threads"], {7: 107})

    def test_exited_game_releases_stale_handles_without_resume(self):
        self._pending()
        api = FakeApi()

        def exited(handle, code):
            code._obj.value = 0
            return True

        with patch.object(pump, "_api", return_value=api), \
             patch.object(api, "GetExitCodeProcess", side_effect=exited):
            self.assertFalse(pump.has_pending_recovery())
        self.assertEqual(api.closed, [107, 99])
        self.assertEqual(api.resumed, [])
        self.assertIsNone(pump._RECOVERY)


if __name__ == "__main__":
    unittest.main()
