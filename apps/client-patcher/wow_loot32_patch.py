#!/usr/bin/env python3
"""Raise the loot-slot limit of a WoW 3.3.5a (build 12340) client from 18 to 32.

The client stores loot in a fixed 18-entry global array and uses the slot number the
server sends as a direct index into it, so a 19th slot corrupts whatever follows the
array. The array ends exactly where the next global begins, so there is no room to grow
it in place; this patcher appends an uninitialised section to hold a larger copy and
repoints every reference at it.

Everything is located by byte pattern and derived from the file, not by hardcoded
addresses, so the patch also applies to a client carrying unrelated modifications.

Usage:
    python wow_loot32_patch.py <wow.exe> [--dry-run] [--output PATH]
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import sys

BUILD_MARKER = b'World of WarCraft (build 12340)'

OLD_COUNT = 18
NEW_COUNT = 32
STRIDE = 0x20

# The item counter is a 6-way unrolled loop, and 32 is not a multiple of 6. Allocating 36
# entries lets it run 6 iterations unchanged; the 4 trailing slots stay zero because the
# bound checks below still cap real writes at 32.
ALLOC_COUNT = 36

OLD_SIZE = OLD_COUNT * STRIDE      # 0x240
NEW_SIZE = NEW_COUNT * STRIDE      # 0x400
ALLOC_SIZE = ALLOC_COUNT * STRIDE  # 0x480

SECTION_NAME = b'.lootx'
SECTION_CHARS = 0xC0000080  # uninitialised data | read | write

IMAGE_SIZEOF_SECTION_HEADER = 40


class PatchError(Exception):
    pass


class PE:
    """Just enough PE parsing to read the section table and append one section."""

    def __init__(self, data):
        self.d = bytearray(data)
        if self.d[:2] != b'MZ':
            raise PatchError('not a PE file (no MZ signature)')
        self.e_lfanew = struct.unpack_from('<I', self.d, 0x3c)[0]
        if self.d[self.e_lfanew:self.e_lfanew + 4] != b'PE\0\0':
            raise PatchError('not a PE file (no PE signature)')
        self.coff = self.e_lfanew + 4
        self.opt = self.coff + 20
        self.opt_size = struct.unpack_from('<H', self.d, self.coff + 16)[0]
        if struct.unpack_from('<H', self.d, self.opt)[0] != 0x10b:
            raise PatchError('not a 32-bit PE32 image')
        self.sec_table = self.opt + self.opt_size

    @property
    def num_sections(self):
        return struct.unpack_from('<H', self.d, self.coff + 2)[0]

    @property
    def image_base(self):
        return struct.unpack_from('<I', self.d, self.opt + 28)[0]

    @property
    def section_alignment(self):
        return struct.unpack_from('<I', self.d, self.opt + 32)[0]

    @property
    def size_of_headers(self):
        return struct.unpack_from('<I', self.d, self.opt + 60)[0]

    def sections(self):
        out = []
        for i in range(self.num_sections):
            b = self.sec_table + IMAGE_SIZEOF_SECTION_HEADER * i
            name = bytes(self.d[b:b + 8]).rstrip(b'\0')
            vsize, vaddr, rsize, rptr = struct.unpack_from('<IIII', self.d, b + 8)
            out.append({'name': name, 'vsize': vsize, 'vaddr': vaddr,
                        'rsize': rsize, 'rptr': rptr, 'hdr': b})
        return out

    def section(self, name):
        for s in self.sections():
            if s['name'] == name:
                return s
        return None

    def va_to_off(self, va):
        rva = va - self.image_base
        for s in self.sections():
            if s['rsize'] and s['vaddr'] <= rva < s['vaddr'] + s['rsize']:
                return s['rptr'] + (rva - s['vaddr'])
        raise PatchError('VA %#x is not backed by raw data' % va)

    def off_to_va(self, off):
        for s in self.sections():
            if s['rsize'] and s['rptr'] <= off < s['rptr'] + s['rsize']:
                return self.image_base + s['vaddr'] + (off - s['rptr'])
        raise PatchError('offset %#x is not inside a section' % off)

    def append_bss_section(self, name, vsize, chars):
        """Append a section with no raw data; the loader zero-fills it.

        Keeping SizeOfRawData at 0 means the file grows by nothing, which leaves the
        Authenticode overlay at the end of the file undisturbed.
        """
        n = self.num_sections
        hdr = self.sec_table + IMAGE_SIZEOF_SECTION_HEADER * n
        if hdr + IMAGE_SIZEOF_SECTION_HEADER > self.size_of_headers:
            raise PatchError('no room in the PE headers for another section header')

        align = self.section_alignment
        end = max(s['vaddr'] + s['vsize'] for s in self.sections())
        rva = (end + align - 1) // align * align

        struct.pack_into('<8s', self.d, hdr, name)
        struct.pack_into('<IIII', self.d, hdr + 8, vsize, rva, 0, 0)
        # PointerToRelocations, PointerToLinenumbers, NumberOf{Relocations,Linenumbers},
        # Characteristics -- 16 bytes, so Characteristics lands at hdr+36.
        struct.pack_into('<IIHHI', self.d, hdr + 24, 0, 0, 0, 0, chars)

        struct.pack_into('<H', self.d, self.coff + 2, n + 1)
        size_of_image = rva + (vsize + align - 1) // align * align
        struct.pack_into('<I', self.d, self.opt + 56, size_of_image)

        return self.image_base + rva


def find_one(pattern, buf, what):
    hits = list(re.finditer(pattern, buf))
    if len(hits) != 1:
        raise PatchError('expected exactly 1 match for %s, found %d' % (what, len(hits)))
    return hits[0]


def locate_array(pe):
    """Derive the loot array base from two independent anchors and cross-check them.

    Anchor 1 is Script_GetLootSlotInfo:
        lea eax,[esi-1] / cmp eax,12h / jb ... / shl eax,5 / mov eax,[eax+base+0Ch]
    Anchor 2 is the item counter's unrolled loop prologue:
        push esi / lea ecx,[eax+2] / mov edx,base+24h / lea esi,[eax+3]
    """
    text = pe.section(b'.text')
    if text is None:
        raise PatchError('no .text section')
    lo, hi = text['rptr'], text['rptr'] + text['rsize']
    seg = bytes(pe.d[lo:hi])

    m1 = find_one(rb'\x8dF\xff\x83\xf8\x12\x72.', seg, 'Script_GetLootSlotInfo bound check')
    tail = seg[m1.end():m1.end() + 24]
    m1b = re.search(rb'\xc1\xe0\x05\x8b\x80(....)', tail)
    if not m1b:
        raise PatchError('could not find the indexed load after the bound check')
    base1 = struct.unpack('<I', m1b.group(1))[0] - 0x0C

    m2 = find_one(rb'\x56\x8dH\x02\xba(....)\x8dp\x03', seg, 'loot item counter prologue')
    base2 = struct.unpack('<I', m2.group(1))[0] - 0x24

    if base1 != base2:
        raise PatchError('anchors disagree on the array base: %#x vs %#x' % (base1, base2))

    # Offset of the 8d 70 03 (lea esi,[eax+3]) that sets the unrolled iteration count.
    unroll_off = lo + m2.end() - 3
    return base1, unroll_off, (lo, hi)


def collect_patches(pe, base, unroll_off, text_range):
    lo, hi = text_range
    data = bytes(pe.d)
    patches = []

    refs = []
    for i in range(lo, hi - 4):
        v = struct.unpack_from('<I', data, i)[0]
        if base <= v < base + OLD_SIZE:
            refs.append((i, v))
    if not refs:
        raise PatchError('found no references to the loot array')

    hull_lo = min(o for o, _ in refs)
    hull_hi = max(o for o, _ in refs) + 4
    if hull_hi - hull_lo > 0x4000:
        raise PatchError('array references are scattered over %#x bytes; refusing to guess'
                         % (hull_hi - hull_lo))

    for off, v in refs:
        patches.append((off, struct.pack('<I', v), None, 'ref %+#06x' % (v - base)))

    margin = 0x80
    rlo, rhi = hull_lo - margin, hull_hi + margin
    seg = data[rlo:rhi]

    # Bound checks: cmp r32, 18 -> cmp r32, 32. Every one must gate a conditional jump.
    for m in re.finditer(rb'\x83[\xf8-\xff]\x12', seg):
        off = rlo + m.start()
        after = seg[m.end():m.end() + 10]
        if not (re.match(rb'[\x72\x73\x76\x77]', after) or
                re.match(rb'\x0f[\x82\x83\x86\x87]', after) or
                re.search(rb'[\x72\x73]', after[:6]) or
                re.search(rb'\x0f[\x82\x83]', after[:6])):
            raise PatchError('cmp r32,12h at %#x is not followed by a conditional jump'
                             % pe.off_to_va(off))
        patches.append((off + 2, b'\x12', bytes([NEW_COUNT]), 'bound check'))

    # Loop limits expressed as a byte offset into the array.
    for pat in (rb'\x3d\x40\x02\x00\x00', rb'\x81[\xf8-\xff]\x40\x02\x00\x00'):
        for m in re.finditer(pat, seg):
            imm = rlo + m.end() - 4
            patches.append((imm, struct.pack('<I', OLD_SIZE),
                            struct.pack('<I', NEW_SIZE), 'loop limit'))

    # memset sizes; clear the whole allocation so the spare slots stay zero.
    for m in re.finditer(rb'\x68\x40\x02\x00\x00', seg):
        imm = rlo + m.end() - 4
        patches.append((imm, struct.pack('<I', OLD_SIZE),
                        struct.pack('<I', ALLOC_SIZE), 'memset size'))

    patches.append((unroll_off, b'\x8d\x70\x03', b'\x8d\x70\x06', 'counter unroll'))

    return patches, (hull_lo, hull_hi)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exe', help='path to wow.exe')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change without writing')
    ap.add_argument('--output', help='write here instead of patching in place')
    ap.add_argument('--no-backup', action='store_true')
    args = ap.parse_args()

    raw = open(args.exe, 'rb').read()
    print('input      %s (%d bytes, md5 %s)'
          % (args.exe, len(raw), hashlib.md5(raw).hexdigest()))

    if BUILD_MARKER not in raw:
        raise PatchError('this does not look like a 3.3.5a build 12340 client')

    pe = PE(raw)
    if pe.section(SECTION_NAME) is not None:
        print('already patched (%s section present); nothing to do'
              % SECTION_NAME.decode())
        return 0

    base, unroll_off, text_range = locate_array(pe)
    print('loot array %#x .. %#x  (%d entries x %#x)'
          % (base, base + OLD_SIZE, OLD_COUNT, STRIDE))

    patches, hull = collect_patches(pe, base, unroll_off, text_range)
    kinds = {}
    for _, _, _, what in patches:
        kinds[what.split()[0]] = kinds.get(what.split()[0], 0) + 1
    print('code hull  %#x .. %#x' % (pe.off_to_va(hull[0]), pe.off_to_va(hull[1])))
    print('patches    %s' % ', '.join('%s=%d' % kv for kv in sorted(kinds.items())))

    seen = {}
    for off, old, new, what in patches:
        for i in range(off, off + len(old)):
            if i in seen:
                raise PatchError('patches overlap at %#x (%s vs %s)' % (i, what, seen[i]))
            seen[i] = what

    new_base = pe.append_bss_section(SECTION_NAME, ALLOC_SIZE, SECTION_CHARS)
    print('new array  %#x .. %#x  (%d entries allocated, %d usable)'
          % (new_base, new_base + ALLOC_SIZE, ALLOC_COUNT, NEW_COUNT))

    for off, old, new, what in patches:
        if bytes(pe.d[off:off + len(old)]) != old:
            raise PatchError('expected %s at %#x, found %s'
                             % (old.hex(), off, bytes(pe.d[off:off + len(old)]).hex()))
        if new is None:
            v = struct.unpack('<I', old)[0]
            new = struct.pack('<I', new_base + (v - base))
        if args.dry_run:
            print('  %#010x  %-14s %s -> %s'
                  % (pe.off_to_va(off), what, old.hex(' '), new.hex(' ')))
        pe.d[off:off + len(old)] = new

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
        f.write(pe.d)
    out = open(dest, 'rb').read()
    print('output     %s (%d bytes, md5 %s)'
          % (dest, len(out), hashlib.md5(out).hexdigest()))
    print('done; %d sites patched' % len(patches))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PatchError as ex:
        print('ERROR: %s' % ex, file=sys.stderr)
        sys.exit(1)
