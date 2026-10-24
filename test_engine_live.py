import hashlib
import struct
import unittest
from unittest.mock import patch

import engine_live as engine


class EngineTests(unittest.TestCase):
    def test_round_trip_and_original(self):
        self.assertIsNone(engine._decode(engine.ORIGINAL))
        for value in [50, 100, 155, 200, 250, 300, 155.25]:
            code = engine._encode(value)
            self.assertEqual(len(code), 9)
            self.assertEqual(code, b'\xb9' + struct.pack('<f', value) + bytes.fromhex('660f6ed1'))
            self.assertEqual(engine._decode(code), value)

    def test_input_rejects_before_backend(self):
        with patch.object(engine._MANAGER, 'apply') as apply:
            for value in [49, 301, float('nan'), float('inf'), '155', True, None]:
                with self.assertRaises(engine.EngineTrainerError):
                    engine.set_power_kw(value)
            apply.assert_not_called()

    def test_unknown_native_code_is_rejected(self):
        for code in [b'', engine.ORIGINAL[:-1], b'\x90'*9,
                     b'\xb9'+struct.pack('<f',float('nan'))+bytes.fromhex('660f6ed1'),
                     b'\xb9'+struct.pack('<f',1000)+bytes.fromhex('660f6ed1')]:
            with self.assertRaises(engine.EngineTrainerError):
                engine._decode(code)

    def test_read_state_is_read_only(self):
        with patch.object(engine._MANAGER,'read',return_value={'running':True,'code':engine.ORIGINAL,'status':'ok'}), patch.object(engine._MANAGER,'apply') as apply:
            state=engine.read_state()
            self.assertFalse(state['enabled'])
            self.assertIsNone(state['power_kw'])
            apply.assert_not_called()

    def test_read_override(self):
        with patch.object(engine._MANAGER,'read',return_value={'running':True,'code':engine._encode(250),'status':'ok'}):
            state=engine.read_state()
            self.assertTrue(state['enabled'])
            self.assertEqual(state['power_kw'],250)

    def test_recovery_guidance_preserved(self):
        with patch.object(engine._MANAGER,'read',return_value={'running':True,'code':None,'status':'Game paused for recovery.'}):
            self.assertEqual(engine.read_state()['status'],'Game paused for recovery.')

    def test_write_calls_backend_once(self):
        with patch.object(engine._MANAGER,'apply') as apply:
            result=engine.set_power_kw(230)
            apply.assert_called_once_with(engine._encode(230))
            self.assertEqual(result['power_kw'],230)

    def test_backend_failure_not_reported_success(self):
        with patch.object(engine._MANAGER,'apply',side_effect=engine.EngineTrainerError('failure')):
            with self.assertRaises(engine.EngineTrainerError):
                engine.set_power_kw(200)

    def test_restore_delegates_recovery(self):
        with patch.object(engine._MANAGER,'restore') as restore:
            result=engine.restore_engine()
            restore.assert_called_once_with()
            self.assertIsNone(result['power_kw'])

    def test_context_rejects_changes(self):
        with patch.object(engine.pump,'_read',return_value=b'wrong'):
            with self.assertRaises(engine.EngineTrainerError):
                engine._verify_tail(1,0x10000)


if __name__=='__main__':
    unittest.main()
