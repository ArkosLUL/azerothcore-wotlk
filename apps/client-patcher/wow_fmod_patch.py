#!/usr/bin/env python3
"""Fix the FMOD sound-group crash of a WoW 3.3.5a (build 12340) client.

FMOD Ex, built into wow.exe, keeps its sound groups on two rings in the system object: groups with
a sound playing and idle ones. SoundI::setSoundGroup moves the sound's group onto the playing ring
after leaving its lock. FMOD's FMOD_NONBLOCKING loader thread calls it for every sound it loads,
always with the master group, while the main thread moves groups between the same rings whenever a
sound starts and on every frame. When the two overlap the rings cross-link, and a later frame
crashes at wow.exe+0x4E47C2 walking onto the idle ring's head.

The patch rewrites setSoundGroup only:

  - it leaves the master group where it is, so the loader thread never touches the rings. The master
    group has no audible limit, and starting one of its sounds still moves it.
  - it stores which sound a group's list entry belongs to before linking the entry in, so the main
    thread can't read an entry without a sound while the loader thread adds one.

Usage:
    python wow_fmod_patch.py <wow.exe> [--dry-run] [--output PATH] [--no-backup]
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import sys

BUILD_MARKER = b'World of WarCraft (build 12340'


def _hex(s):
    return re.escape(bytes.fromhex(s))


# SoundI::setSoundGroup(SoundGroupI *group) at wow.exe+0x4E1AC0, esi = the sound. Wildcards: the
# sound list lock's address, read twice, and the rel32 of the enter and leave calls.
HEAD = (
    _hex('55 8b ec 8b 45 08 85 c0 56 8b f1'
         '89 86 c4 00 00 00 75 12'                 # sound->group = group
         '8b 86 ac 00 00 00 8b 88 9c 7d 00 00'     # or the system's master group
         '89 8e c4 00 00 00')
    + rb'\x8b\x15(....)\x52\xe8....'               # enter the sound list lock
    + _hex('8b 8e cc 00 00 00 8b 96 c8 00 00 00'   # unlink the sound's entry (eax)
           '8d 86 c8 00 00 00 89 11 8b 08 8b 50 04'
           '89 51 04 89 40 04 89 00')
)

ENTRY_OLD = 'c7 40 08 00 00 00 00'                 # entry->sound = 0
ENTRY_NEW = ('89 70 08'                            # entry->sound = this
             '0f 1f 40 00')                        # nop

MID = (
    _hex('8b 8e c4 00 00 00 8b 51 18 83 c1 14'     # link it last in group+14h
         '89 50 04 89 08 89 41 04 8b 48 04 89 01'
         '89 b6 d0 00 00 00')                      # entry->sound = this
    + rb'\x8b\x15\1\x52\xe8....'                   # leave the lock
)

# move the group to the head of the playing ring, system+7DA0h, outside the lock
MOVE_OLD = ('8b 86 c4 00 00 00 8b 48 04 8b 10 89 11 8b 08 8b 50 04 89 51 04 89 40 04 89 00'
            'c7 40 08 00 00 00 00 8b 86 ac 00 00 00 8b 88 a0 7d 00 00 8b b6 c4 00 00 00'
            '05 a0 7d 00 00 89 46 04 89 0e 8b d1 89 72 04 8b 46 04'
            '83 c4 08 89 30 33 c0 5e 5d c2 04 00')
MOVE_NEW = ('8b 86 c4 00 00 00'                    # mov eax,[esi+0C4h]   group
            '8b 8e ac 00 00 00'                    # mov ecx,[esi+0ACh]   system
            '3b 81 9c 7d 00 00'                    # cmp eax,[ecx+7D9Ch]  master group?
            '74 23'                                # je  done
            '8b 50 04 8b 30 89 32 89 56 04'        # unlink the group
            'c7 40 08 00 00 00 00'
            '81 c1 a0 7d 00 00'                    # add ecx,7DA0h        playing ring head
            '8b 11 89 48 04 89 10 89 42 04 89 01'  # link the group after it
            '83 c4 08 33 c0 5e 5d c2 04 00'        # done: return FMOD_OK
            + ' cc' * 16)

UNPATCHED = re.compile(HEAD + b'(?P<entry>' + _hex(ENTRY_OLD) + b')' + MID
                       + b'(?P<move>' + _hex(MOVE_OLD) + b')', re.S)
PATCHED = re.compile(HEAD + b'(?P<entry>' + _hex(ENTRY_NEW) + b')' + MID
                     + b'(?P<move>' + _hex(MOVE_NEW) + b')', re.S)


class PatchError(Exception):
    pass


def text_range(data):
    """File offsets of .text and the delta that turns one into a virtual address."""
    if data[:2] != b'MZ':
        raise PatchError('not a PE file (no MZ signature)')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise PatchError('not a PE file (no PE signature)')
    coff = pe + 4
    count, = struct.unpack_from('<H', data, coff + 2)
    opt_size, = struct.unpack_from('<H', data, coff + 16)
    image_base, = struct.unpack_from('<I', data, coff + 20 + 28)
    table = coff + 20 + opt_size
    for i in range(count):
        h = table + 40 * i
        if data[h:h + 8].rstrip(b'\0') == b'.text':
            vaddr, rsize, rptr = struct.unpack_from('<III', data, h + 12)
            return rptr, rptr + rsize, image_base + vaddr - rptr
    raise PatchError('no .text section')


def locate(data, lo, hi):
    """Offset of setSoundGroup and whether it is patched already."""
    text = bytes(data[lo:hi])
    old = [lo + m.start() for m in UNPATCHED.finditer(text)]
    new = [lo + m.start() for m in PATCHED.finditer(text)]
    if len(old) + len(new) != 1:
        raise PatchError('expected exactly one SoundI::setSoundGroup, found %d unpatched and %d '
                         'patched' % (len(old), len(new)))
    return (old or new)[0], bool(new)


def collect_patches(data, func):
    m = UNPATCHED.match(bytes(data), func)
    return [(m.start('entry'), bytes.fromhex(ENTRY_OLD), bytes.fromhex(ENTRY_NEW),
             'sound entry: set its sound before linking it'),
            (m.start('move'), bytes.fromhex(MOVE_OLD), bytes.fromhex(MOVE_NEW),
             'group move: leave the master group alone')]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exe', help='path to wow.exe')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change without writing')
    ap.add_argument('--output', help='write here instead of patching in place')
    ap.add_argument('--no-backup', action='store_true')
    args = ap.parse_args()

    with open(args.exe, 'rb') as f:
        raw = f.read()
    print('input     %s (%d bytes, md5 %s)' % (args.exe, len(raw), hashlib.md5(raw).hexdigest()))
    if BUILD_MARKER not in raw:
        raise PatchError('this does not look like a 3.3.5a build 12340 client')

    data = bytearray(raw)
    lo, hi, to_va = text_range(data)
    func, patched = locate(data, lo, hi)
    print('function   SoundI::setSoundGroup at %#010x' % (func + to_va))
    if patched:
        print('already patched; nothing to do')
        return 0

    patches = collect_patches(data, func)
    for off, old, new, what in patches:
        if args.dry_run:
            print('  %#010x  %s\n    %s\n -> %s' % (off + to_va, what, old.hex(' '), new.hex(' ')))
        data[off:off + len(old)] = new

    if args.dry_run:
        print('dry run; nothing written')
        return 0

    dest = args.output or args.exe
    if not args.output and not args.no_backup:
        bak = args.exe + '.bak'
        if not os.path.exists(bak):
            shutil.copy2(args.exe, bak)
            print('backup     %s' % bak)

    with open(dest, 'wb') as f:
        f.write(data)
    with open(dest, 'rb') as f:
        out = f.read()
    if locate(out, lo, hi) != (func, True):
        raise PatchError('%s does not read back as patched' % dest)
    print('output     %s (%d bytes, md5 %s)' % (dest, len(out), hashlib.md5(out).hexdigest()))
    print('done; %d sites patched' % len(patches))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PatchError as ex:
        print('ERROR: %s' % ex, file=sys.stderr)
        sys.exit(1)
