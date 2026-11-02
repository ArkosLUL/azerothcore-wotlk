import contextlib
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import wow_fmod_patch as fmod  # noqa: E402

# wow.exe+0x4E1AC0 to +0x4E1B95 in build 12340, the same bytes in the loot-patched copies
FUNCTION = bytes.fromhex(
    '55 8b ec 8b 45 08 85 c0 56 8b f1 89 86 c4 00 00 00 75 12 8b 86 ac 00 00 00 8b 88 9c 7d 00 00 89'
    '8e c4 00 00 00 8b 15 ac e9 d4 00 52 e8 8f 04 ff ff 8b 8e cc 00 00 00 8b 96 c8 00 00 00 8d 86 c8'
    '00 00 00 89 11 8b 08 8b 50 04 89 51 04 89 40 04 89 00 c7 40 08 00 00 00 00 8b 8e c4 00 00 00 8b'
    '51 18 83 c1 14 89 50 04 89 08 89 41 04 8b 48 04 89 01 89 b6 d0 00 00 00 8b 15 ac e9 d4 00 52 e8'
    '5c 04 ff ff 8b 86 c4 00 00 00 8b 48 04 8b 10 89 11 8b 08 8b 50 04 89 51 04 89 40 04 89 00 c7 40'
    '08 00 00 00 00 8b 86 ac 00 00 00 8b 88 a0 7d 00 00 8b b6 c4 00 00 00 05 a0 7d 00 00 89 46 04 89'
    '0e 8b d1 89 72 04 8b 46 04 83 c4 08 89 30 33 c0 5e 5d c2 04 00')
PAD = b'\xcc' * 32
EPILOGUE = bytes.fromhex('83 c4 08 33 c0 5e 5d c2 04 00')


def text(*funcs):
    return bytearray(PAD + PAD.join(funcs) + PAD)


def fake_pe(code):
    """A PE header with one .text section at file offset 0x200, then the build marker."""
    pe = bytearray(0x200)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 0x3c, 0x40)
    pe[0x40:0x44] = b'PE\0\0'
    struct.pack_into('<HH', pe, 0x44, 0x14c, 1)
    struct.pack_into('<H', pe, 0x44 + 16, 0xe0)
    struct.pack_into('<H', pe, 0x58, 0x10b)
    struct.pack_into('<I', pe, 0x58 + 28, 0x400000)
    struct.pack_into('<8sIIII', pe, 0x58 + 0xe0, b'.text', len(code), 0x1000, len(code), 0x200)
    return bytes(pe) + bytes(code) + fmod.BUILD_MARKER + b')'


def run(*argv):
    out = io.StringIO()
    old = sys.argv
    sys.argv = ['wow_fmod_patch.py'] + list(argv)
    try:
        with contextlib.redirect_stdout(out):
            code = fmod.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


class LocateTest(unittest.TestCase):
    def test_finds_the_unpatched_function(self):
        data = text(FUNCTION)
        self.assertEqual(fmod.locate(data, 0, len(data)), (len(PAD), False))

    def test_refuses_two_copies(self):
        data = text(FUNCTION, FUNCTION)
        with self.assertRaises(fmod.PatchError):
            fmod.locate(data, 0, len(data))

    def test_refuses_a_client_without_it(self):
        data = text(FUNCTION[:-1])
        with self.assertRaises(fmod.PatchError):
            fmod.locate(data, 0, len(data))

    def test_both_lock_reads_must_name_the_same_lock(self):
        changed = bytearray(FUNCTION)
        changed[0x7a] ^= 1
        data = text(bytes(changed))
        with self.assertRaises(fmod.PatchError):
            fmod.locate(data, 0, len(data))


class PatchTest(unittest.TestCase):
    def patched(self):
        data = text(FUNCTION)
        func, _ = fmod.locate(data, 0, len(data))
        for off, old, new, _ in fmod.collect_patches(data, func):
            self.assertEqual(bytes(data[off:off + len(old)]), old)
            data[off:off + len(new)] = new
        return data, func

    def test_reads_back_as_patched(self):
        data, func = self.patched()
        self.assertEqual(len(data), len(text(FUNCTION)))
        self.assertEqual(fmod.locate(data, 0, len(data)), (func, True))

    def test_changes_only_the_entry_store_and_the_group_move(self):
        data, func = self.patched()
        before = text(FUNCTION)
        changed = {i - func for i in range(len(data)) if data[i] != before[i]}
        self.assertTrue(changed)
        self.assertTrue(all(0x52 <= i < 0x59 or 0x84 <= i < len(FUNCTION) for i in changed))

    def test_entry_gets_its_sound_before_it_is_linked(self):
        data, func = self.patched()
        # mov [eax+8],esi with eax = the entry, before the first store that links it
        self.assertEqual(bytes(data[func + 0x52:func + 0x55]), bytes.fromhex('89 70 08'))

    def test_master_group_jumps_to_the_epilogue(self):
        move = bytes.fromhex(fmod.MOVE_NEW)
        self.assertEqual(len(move), len(bytes.fromhex(fmod.MOVE_OLD)))
        self.assertEqual(move[12:20], bytes.fromhex('3b 81 9c 7d 00 00 74 23'))
        target = 20 + move[19]
        self.assertEqual(move[target:target + len(EPILOGUE)], EPILOGUE)
        self.assertEqual(move[target + len(EPILOGUE):], b'\xcc' * 16)


class MainTest(unittest.TestCase):
    def test_patches_in_place_with_a_backup_then_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, 'wow.exe')
            original = fake_pe(text(FUNCTION))
            with open(exe, 'wb') as f:
                f.write(original)

            code, out = run(exe)
            self.assertEqual(code, 0)
            self.assertIn('done; 2 sites patched', out)
            with open(exe + '.bak', 'rb') as f:
                self.assertEqual(f.read(), original)
            with open(exe, 'rb') as f:
                patched = f.read()
            self.assertEqual(len(patched), len(original))
            self.assertNotEqual(patched, original)

            code, out = run(exe)
            self.assertEqual(code, 0)
            self.assertIn('already patched', out)
            with open(exe, 'rb') as f:
                self.assertEqual(f.read(), patched)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, 'wow.exe')
            original = fake_pe(text(FUNCTION))
            with open(exe, 'wb') as f:
                f.write(original)

            code, out = run(exe, '--dry-run')
            self.assertEqual(code, 0)
            self.assertIn('0x00401020', out)
            with open(exe, 'rb') as f:
                self.assertEqual(f.read(), original)
            self.assertFalse(os.path.exists(exe + '.bak'))

    def test_refuses_another_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, 'wow.exe')
            with open(exe, 'wb') as f:
                f.write(fake_pe(text(FUNCTION))[:-len(fmod.BUILD_MARKER) - 1])
            with self.assertRaises(fmod.PatchError):
                run(exe)


if __name__ == '__main__':
    unittest.main()
