"""Safety and byte-preservation tests. Every write stays in tests-temp."""
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import trainer_core as core
from save_checksum import compute_checksum
import msgpack
import zstandard

ROOT = Path(__file__).resolve().parent
TEMP_ROOT = ROOT / 'tests-temp'
TEMP_ROOT.mkdir(exist_ok=True)


def pack(value):
    return msgpack.packb(value, use_bin_type=True)


def map_bytes(pairs):
    return msgpack.Packer().pack_map_header(len(pairs)) + b''.join(pack(k) + v for k, v in pairs)


def float32(value):
    return b'\xca' + struct.pack('>f', value)


def float64(value):
    return b'\xcb' + struct.pack('>d', value)


def make_world(fuel=.25, second=.75):
    machine = map_bytes([
        ('name', pack('Test excavator')),
        ('fuel_fraction', float32(fuel)),
        ('future_field', map_bytes([('curve', float32(.1234)), ('untouched', float64(.123456789012345))])),
        ('id', pack(17)),
    ])
    fleet = map_bytes([('nested', map_bytes([('fuel_fraction', float64(second)), ('unique', pack(b'\x00\xfffuture'))]))])
    return map_bytes([
        ('machines', b'\x91' + machine),
        ('terrain', pack(bytes(range(256)))),
        ('authored_fleet', b'\x91' + fleet),
        ('world_tools', pack({'water_pumps': [{'flow_m3s': .02, 'label': 'leave me'}]})),
        ('unknown_future_schema', map_bytes([('f32', float32(.99)), ('fuel_fraction', float32(.2))])),
    ])


def make_campaign():
    return {'version': 3, 'map_id': 'fixture-map',
            'funds': {'profile': 'fresh-campaign', 'opening_balance_eur': 500000,
                      'income': [{'id': 'abc', 'amount': 17.25}], 'expenses': [33]},
            'future': {'text': 'Grunnarbeid \u00e6\u00f8\u00e5', 'nested': [True, None, 2.7]}}


def make_envelope(world=None, campaign=None):
    if world is None:
        world = make_world()
    if campaign is None:
        campaign = make_campaign()
    e = {'magic': list(b'GRNDW001'), 'format_version': 1, 'baseline': core.BASELINE,
         'checksum_sha256': [0]*32,
         'metadata': {'display_name': 'Synthetic safety fixture', 'machine': 'test'},
         'compressed_world_payload': list(zstandard.ZstdCompressor().compress(world)),
         'extension_payloads': {core.CAMPAIGN: list(json.dumps(campaign).encode()),
                                'z-unknown-extension': [0, 1, 2, 255]},
         'unknown_top_level': {'future': 'preserve'}}
    e['checksum_sha256'] = list(compute_checksum(e))
    return e


def envelope_bytes(e=None):
    return pack(e if e is not None else make_envelope())


def settings_bytes():
    return b'''(\n    profile: "test (quoted) ",\n    powerLimitKw: 777.0,\n    unrelated: (flowBudget: 42.0,),\n    hydraulic_tuning: (\n        version: 9,\n        engine: (powerLimitKw: 100.0, other: 111,),\n        flowBudget: 1.0,\n        unknown: "leave this alone",\n    ),\n    other_tuning: (engine: (powerLimitKw: 888.0,),),\n)\n'''


class IntegrityTests(unittest.TestCase):
    def test_known_digest_algorithm_uses_byte_lengths_sorted_extensions(self):
        e = {'compressed_world_payload': [1, 2, 3], 'extension_payloads': {'z': [255], '\u00e6': [4,5], 'a': [7]}}
        expected = hashlib.sha256(b'\x01\x02\x03' + struct.pack('<Q',1)+b'a'+struct.pack('<Q',1)+b'\x07' + struct.pack('<Q',1)+b'z'+struct.pack('<Q',1)+b'\xff' + struct.pack('<Q',2)+'\u00e6'.encode()+struct.pack('<Q',2)+b'\x04\x05').digest()
        self.assertEqual(compute_checksum(e), expected)
        self.assertEqual(compute_checksum({'compressed_world_payload': b'abc'}), hashlib.sha256(b'abc').digest())

    def test_checksum_corruption_refused_before_edit(self):
        for field in ('world', 'extension', 'checksum'):
            e = make_envelope()
            if field == 'world': e['compressed_world_payload'][5] ^= 1
            if field == 'extension': e['extension_payloads'][core.CAMPAIGN][10] ^= 1
            if field == 'checksum': e['checksum_sha256'][0] ^= 1
            with self.subTest(field=field), self.assertRaisesRegex(core.TrainerError, 'integrity'):
                core.edit_save_bytes(envelope_bytes(e), {'add_money': 5})

    def test_unknown_envelope_versions_refused(self):
        for field, value in [('format_version', 2), ('format_version', 0), ('format_version', True), ('format_version', 1.0), ('baseline', 'unknown')]:
            e = make_envelope(); e[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(core.TrainerError, 'version'):
                core.load_envelope(envelope_bytes(e))

    def test_unknown_campaign_version_refused(self):
        for version in (4, 3.0, True):
            c = make_campaign(); c['version'] = version
            with self.subTest(version=version), self.assertRaises(core.TrainerError):
                core.edit_save_bytes(envelope_bytes(make_envelope(campaign=c)), {'add_money': 5})

    def test_all_invalid_options_refused(self):
        bad = [{'add_money': True}, {'add_money': -1}, {'add_money': 1.5}, {'add_money': 1_000_000_001},
               {'refill_fuel': 1}, {'engine_kw': True}, {'engine_kw': math.nan}, {'engine_kw': math.inf},
               {'engine_kw': 49.999}, {'engine_kw': 300.001}, {'hydraulic_flow': .999},
               {'hydraulic_flow': 3.001}, {'hydraulic_flow': -math.inf}, {'unknown': 1}]
        for option in bad:
            with self.subTest(option=option), self.assertRaises(core.TrainerError):
                core.validate_options(option)

    def test_money_only_changes_opening_balance_semantically(self):
        e = make_envelope(); old = envelope_bytes(e)
        changed, notes = core.edit_save_bytes(old, {'add_money': 12345})
        result = core.load_envelope(changed)
        expected = make_campaign(); expected['funds']['opening_balance_eur'] += 12345
        self.assertEqual(core._campaign(result), expected)
        self.assertEqual(result['compressed_world_payload'], bytes(e['compressed_world_payload']))
        self.assertEqual(result['extension_payloads']['z-unknown-extension'], e['extension_payloads']['z-unknown-extension'])
        self.assertEqual(result['metadata'], e['metadata'])
        self.assertEqual(result['unknown_top_level'], e['unknown_top_level'])
        self.assertEqual(len(notes), 1)

    def test_money_result_limit_refused(self):
        c = make_campaign(); c['funds']['opening_balance_eur'] = 1_999_999_999
        with self.assertRaises(core.TrainerError):
            core.edit_save_bytes(envelope_bytes(make_envelope(campaign=c)), {'add_money': 2})

    def test_fuel_edits_preserve_every_other_world_byte_and_float_width(self):
        e = make_envelope()
        changed, notes = core.edit_save_bytes(envelope_bytes(e), {'refill_fuel': True})
        result = core.load_envelope(changed)
        self.assertEqual(core._world(result), make_world(1.0, 1.0))
        self.assertEqual(result['extension_payloads'], e['extension_payloads'])
        self.assertEqual(result['metadata'], e['metadata'])
        self.assertIn('2 live/fleet', notes[0])

    def test_fuel_invalid_numeric_values_refused(self):
        for value in (math.nan, math.inf, -.1, 1.01):
            with self.subTest(value=value), self.assertRaises(core.TrainerError):
                core.edit_save_bytes(envelope_bytes(make_envelope(world=make_world(value))), {'refill_fuel': True})

    def test_integer_fuel_encoding_and_no_tanks_refused(self):
        for world in (map_bytes([('machines', pack([{'fuel_fraction': 0}]))]), pack({'machines': [{'fuel_fraction': True}]}), pack({'machines': []})):
            with self.assertRaises(core.TrainerError):
                core.edit_save_bytes(envelope_bytes(make_envelope(world=world)), {'refill_fuel': True})

    def test_duplicate_world_keys_and_trailing_data_refused(self):
        worlds = [b'\x82' + pack('machines')+pack([])+pack('machines')+pack([]), make_world()+b'\x00']
        for world in worlds:
            with self.assertRaises(core.TrainerError):
                core.edit_save_bytes(envelope_bytes(make_envelope(world=world)), {'refill_fuel': True})

    def test_no_save_changes_returns_identical_input(self):
        raw = envelope_bytes()
        self.assertEqual(core.edit_save_bytes(raw, {}), (raw, []))


class SettingsTests(unittest.TestCase):
    def test_settings_change_only_target_numbers_in_hydraulic_block(self):
        raw = settings_bytes()
        result, notes = core.edit_settings_bytes(raw, {'engine_kw': 250, 'hydraulic_flow': 2.5})
        expected = raw.replace(b'powerLimitKw: 100.0', b'powerLimitKw: 250.0').replace(b'flowBudget: 1.0', b'flowBudget: 2.5')
        self.assertEqual(result, expected)
        self.assertEqual(len(notes), 2)

    def test_settings_unknown_version_and_ambiguous_values_refused(self):
        cases = [settings_bytes().replace(b'version: 9', b'version: 10'),
                 settings_bytes().replace(b'flowBudget: 1.0,', b'flowBudget: 1.0, flowBudget: 2.0,'),
                 settings_bytes().replace(b'powerLimitKw: 100.0', b'powerLimitKw: NaN')]
        for raw in cases:
            with self.assertRaises(core.TrainerError):
                core.edit_settings_bytes(raw, {'engine_kw': 250})

    def test_unchanged_settings_are_byte_identical(self):
        raw = settings_bytes()
        self.assertEqual(core.edit_settings_bytes(raw, {'engine_kw': 100, 'hydraulic_flow': 1}), (raw, []))


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='trainer-test-', dir=str(TEMP_ROOT))
        self.base = Path(self.temp.name)
        self.save = self.base / 'slot-01.groundwork-simulator'
        self.settings = self.base / 'settings.ron'
        self.original_save = envelope_bytes()
        self.original_settings = settings_bytes()
        self.save.write_bytes(self.original_save)
        self.settings.write_bytes(self.original_settings)
        self.closed = patch.object(core, 'is_game_running', return_value=False)
        self.closed.start()

    def tearDown(self):
        self.closed.stop()
        self.temp.cleanup()

    def test_apply_backup_restore_and_restore_undo(self):
        result = core.apply_changes(self.save, self.settings, {'add_money': 12345, 'refill_fuel': True, 'engine_kw': 250, 'hydraulic_flow': 2})
        backup = Path(result['backup_dir'])
        manifest = json.loads((backup/'manifest.json').read_bytes())
        self.assertEqual(manifest['status'], 'complete')
        self.assertEqual((backup/'save.original').read_bytes(), self.original_save)
        self.assertEqual((backup/'settings.original').read_bytes(), self.original_settings)
        edited_save = self.save.read_bytes(); edited_settings = self.settings.read_bytes()
        self.assertNotEqual(edited_save, self.original_save)
        self.assertNotEqual(edited_settings, self.original_settings)
        core.restore_backup(backup)
        self.assertEqual(self.save.read_bytes(), self.original_save)
        self.assertEqual(self.settings.read_bytes(), self.original_settings)
        undo = next(p for p in backup.parent.iterdir() if p != backup)
        core.restore_backup(undo)
        self.assertEqual(self.save.read_bytes(), edited_save)
        self.assertEqual(self.settings.read_bytes(), edited_settings)

    def test_running_game_refuses_all_writes(self):
        with patch.object(core, 'is_game_running', return_value=True), self.assertRaises(core.TrainerError):
            core.apply_changes(self.save, self.settings, {'add_money': 1})
        self.assertEqual(self.save.read_bytes(), self.original_save)
        self.assertFalse((self.base/'trainer-backups').exists())

    def test_corrupt_save_refuses_all_writes_even_when_settings_selected(self):
        e = make_envelope(); e['checksum_sha256'][1] ^= 1
        corrupted = envelope_bytes(e); self.save.write_bytes(corrupted)
        with self.assertRaises(core.TrainerError):
            core.apply_changes(self.save, self.settings, {'add_money': 1, 'engine_kw': 250})
        self.assertEqual(self.save.read_bytes(), corrupted)
        self.assertEqual(self.settings.read_bytes(), self.original_settings)
        self.assertFalse((self.base/'trainer-backups').exists())

    def test_second_write_failure_rolls_back_first_and_preserves_backup(self):
        real_atomic = core._atomic
        def fail_settings(path, data):
            if Path(path) == self.settings:
                raise OSError('simulated settings write failure')
            return real_atomic(path, data)
        with patch.object(core, '_atomic', side_effect=fail_settings), self.assertRaisesRegex(core.TrainerError, 'Apply failed'):
            core.apply_changes(self.save, self.settings, {'add_money': 1, 'engine_kw': 250})
        self.assertEqual(self.save.read_bytes(), self.original_save)
        self.assertEqual(self.settings.read_bytes(), self.original_settings)
        backup = next((self.base/'trainer-backups').iterdir())
        self.assertEqual((backup/'save.original').read_bytes(), self.original_save)
        self.assertEqual((backup/'settings.original').read_bytes(), self.original_settings)

    def test_backup_creation_failure_never_touches_targets(self):
        real_atomic = core._atomic
        def fail_manifest(path, data):
            if Path(path).name == 'manifest.json': raise OSError('simulated backup failure')
            return real_atomic(path, data)
        with patch.object(core, '_atomic', side_effect=fail_manifest), self.assertRaises(OSError):
            core.apply_changes(self.save, self.settings, {'add_money': 1})
        self.assertEqual(self.save.read_bytes(), self.original_save)
        self.assertEqual(self.settings.read_bytes(), self.original_settings)

    def test_restore_refuses_newer_progress(self):
        result = core.apply_changes(self.save, self.settings, {'add_money': 1})
        newer, _ = core.edit_save_bytes(self.save.read_bytes(), {'add_money': 2})
        self.save.write_bytes(newer)
        with self.assertRaisesRegex(core.TrainerError, 'newer progress'):
            core.restore_backup(result['backup_dir'])
        self.assertEqual(self.save.read_bytes(), newer)

    def test_restore_refuses_damaged_backup(self):
        result = core.apply_changes(self.save, self.settings, {'add_money': 1})
        before = self.save.read_bytes()
        (Path(result['backup_dir'])/'save.original').write_bytes(b'damaged')
        with self.assertRaisesRegex(core.TrainerError, 'damaged'):
            core.restore_backup(result['backup_dir'])
        self.assertEqual(self.save.read_bytes(), before)

    def test_change_between_prepare_and_commit_refused(self):
        changed = b'external update'
        real_atomic = core._atomic
        def change_after_manifest(path, data):
            value = real_atomic(path, data)
            if Path(path).name == 'manifest.json': self.save.write_bytes(changed)
            return value
        with patch.object(core, '_atomic', side_effect=change_after_manifest), self.assertRaises(core.TrainerError):
            core.apply_changes(self.save, self.settings, {'add_money': 1})
        self.assertEqual(self.save.read_bytes(), changed)

    def test_settings_only_does_not_require_or_touch_save(self):
        nonexistent = self.base/'nonexistent.groundwork-simulator'
        core.apply_changes(nonexistent, self.settings, {'engine_kw': 250})
        self.assertFalse(nonexistent.exists())
        self.assertEqual(self.save.read_bytes(), self.original_save)
        self.assertIn(b'powerLimitKw: 250.0', self.settings.read_bytes())

    def test_invalid_options_never_write_or_create_backups(self):
        with self.assertRaises(core.TrainerError):
            core.apply_changes(self.save, self.settings, {'engine_kw': math.nan})
        self.assertEqual(self.save.read_bytes(), self.original_save)
        self.assertEqual(self.settings.read_bytes(), self.original_settings)
        self.assertFalse((self.base/'trainer-backups').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
