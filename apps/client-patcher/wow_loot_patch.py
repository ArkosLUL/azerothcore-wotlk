#!/usr/bin/env python3
"""Raise the loot-slot limit of a WoW 3.3.5a (build 12340) client from 18 to --slots.

The client holds loot in two parallel 18-slot global arrays and uses the slot number the
server sends as a direct index into both:

  records  18 x 0x18  the slots the loot packet is parsed into, reached through six
                      bounds-checked field accessors that call a fatal assert out of range
  display  18 x 0x20  a cache the loot frame reads, filled by copying from the records

Both end exactly where the next global begins, so neither can grow in place. This patcher
appends an uninitialised section holding a larger copy of each, repoints every reference,
raises every bound, and lifts the 18-item clamp in the loot packet handler.

Everything is located by byte pattern and derived from the file, not by hardcoded addresses,
so the patch also applies to a client carrying unrelated modifications.

Usage:
    python wow_loot_patch.py <wow.exe> --slots N [--dry-run] [--output PATH]
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import sys

BUILD_MARKER = b'World of WarCraft (build 12340'

OLD_COUNT = 18
# bounds are `cmp r/m32, imm8`, sign-extended, so anything past 127 would read as negative
MAX_SLOTS = 127

DISPLAY_STRIDE = 0x20
RECORDS_STRIDE = 0x18

# The display item counter is a loop unrolled 6 wide. Rounding the allocation up to a
# multiple of 6 lets it run whole iterations; the spare trailing slots stay zero because
# the bound checks still cap real writes at --slots.
UNROLL = 6

SECTION_NAME = b'.lootx'
SECTION_CHARS = 0xC0000080  # uninitialised data | read | write

IMAGE_SIZEOF_SECTION_HEADER = 40

# push ebp / mov ebp,esp / mov eax,[ebp+8] / cmp eax,bound / jae fatal
# / lea eax,[eax+eax*2] / mov eax,[eax*8 + base + field]
RECORD_ACCESSOR = rb'\x55\x8b\xec\x8b\x45\x08\x83\xf8(.)\x73\x0c\x8d\x04\x40\x8b\x04\xc5(....)'

# mov al,12h / cmp [ebp+x],al / jbe +3 / mov [ebp+x],al
COUNT_CLAMP = rb'\xb0\x12\x38\x45(.)\x76\x03\x88\x45\1'

# cmp r32,12h or cmp dword [ebp+x],12h
SLOT_BOUND = rb'\x83(?:[\xf8-\xff]|\x7d.)\x12'


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
        # relocation/linenumber pointers and counts are 16 bytes, so Characteristics is hdr+36
        struct.pack_into('<IIHHI', self.d, hdr + 24, 0, 0, 0, 0, chars)

        struct.pack_into('<H', self.d, self.coff + 2, n + 1)
        size_of_image = rva + (vsize + align - 1) // align * align
        struct.pack_into('<I', self.d, self.opt + 56, size_of_image)

        return self.image_base + rva


def alloc_count(slots):
    return -(-slots // UNROLL) * UNROLL


def find_one(pattern, buf, what):
    hits = list(re.finditer(pattern, buf, re.S))
    if len(hits) != 1:
        raise PatchError('expected exactly 1 match for %s, found %d' % (what, len(hits)))
    return hits[0]


def text_bytes(pe):
    t = pe.section(b'.text')
    if t is None:
        raise PatchError('no .text section')
    return t['rptr'], t['rptr'] + t['rsize']


def locate_display(pe, lo, hi):
    """Derive the display array base from two independent anchors that must agree.

    Anchor 1 is Script_GetLootSlotInfo:
        lea eax,[esi-1] / cmp eax,12h / jb ... / shl eax,5 / mov eax,[eax+base+0Ch]
    Anchor 2 is the item counter's unrolled loop prologue:
        push esi / lea ecx,[eax+2] / mov edx,base+24h / lea esi,[eax+3]
    """
    seg = bytes(pe.d[lo:hi])

    m1 = find_one(rb'\x8dF\xff\x83\xf8\x12\x72.', seg, 'Script_GetLootSlotInfo bound check')
    tail = seg[m1.end():m1.end() + 24]
    m1b = re.search(rb'\xc1\xe0\x05\x8b\x80(....)', tail, re.S)
    if not m1b:
        raise PatchError('could not find the indexed load after the bound check')
    base1 = struct.unpack('<I', m1b.group(1))[0] - 0x0C

    m2 = find_one(rb'\x56\x8dH\x02\xba(....)\x8dp\x03', seg, 'loot item counter prologue')
    base2 = struct.unpack('<I', m2.group(1))[0] - 0x24

    if base1 != base2:
        raise PatchError('display anchors disagree on the base: %#x vs %#x' % (base1, base2))

    # offset of the 8d 70 03 (lea esi,[eax+3]) that sets the unrolled iteration count
    return base1, lo + m2.end() - 3


def record_accessors(pe, lo, hi):
    """The records base, the slot bound its six field accessors share, and their bound offsets."""
    seg = bytes(pe.d[lo:hi])
    hits = [(lo + m.start() + 8, m.group(1)[0], struct.unpack('<I', m.group(2))[0])
            for m in re.finditer(RECORD_ACCESSOR, seg, re.S)]
    if len(hits) != 6:
        raise PatchError('expected 6 record field accessors, found %d' % len(hits))

    base = min(t for _, _, t in hits)
    fields = sorted(t - base for _, _, t in hits)
    if fields != [0x00, 0x04, 0x08, 0x0C, 0x10, 0x14]:
        raise PatchError('record accessors do not describe a 6-field struct: %s'
                         % [hex(f) for f in fields])

    bounds = {b for _, b, _ in hits}
    if len(bounds) != 1:
        raise PatchError('record accessors disagree on the slot bound: %s' % sorted(bounds))
    return base, bounds.pop(), [off for off, _, _ in hits]


def records_refs(pe, lo, hi, base, size):
    """References into the records array.

    Every genuine one is either `push imm32` or a [reg*8 + disp32] SIB operand, since the
    array is always indexed as index*3*8. Requiring that shape rejects dwords that only match
    because they straddle a ModRM byte and an immediate.
    """
    out = []
    for i in range(lo, hi - 4):
        v = struct.unpack_from('<I', pe.d, i)[0]
        if base <= v < base + size:
            prev = pe.d[i - 1]
            if prev == 0x68 or (prev & 0xC7) == 0xC5:
                out.append((i, v))
    return out


def locate_count_clamp(pe, lo, hi, rec_refs):
    """The loot packet handler caps the item count it reads at 18 before parsing any slot."""
    m = find_one(COUNT_CLAMP, bytes(pe.d[lo:hi]), 'loot packet item count clamp')
    off = lo + m.start()
    # the records memset follows it in the same handler
    if not any(0 < r - off <= 0x200 for r, _ in rec_refs):
        raise PatchError('item count clamp at %#x has no records reference after it'
                         % pe.off_to_va(off))
    return off + 1


def collect_patches(pe, lo, hi, slots):
    data = bytes(pe.d)
    alloc = alloc_count(slots)
    patches = []
    layout = {}

    # ---- display array -------------------------------------------------
    disp_base, unroll_off = locate_display(pe, lo, hi)
    disp_size = OLD_COUNT * DISPLAY_STRIDE

    disp_refs = []
    for i in range(lo, hi - 4):
        v = struct.unpack_from('<I', data, i)[0]
        if disp_base <= v < disp_base + disp_size:
            disp_refs.append((i, v))
    if not disp_refs:
        raise PatchError('found no references to the display array')

    hull_lo = min(o for o, _ in disp_refs)
    hull_hi = max(o for o, _ in disp_refs) + 4
    if hull_hi - hull_lo > 0x4000:
        raise PatchError('display references span %#x bytes; refusing to guess'
                         % (hull_hi - hull_lo))

    for off, v in disp_refs:
        patches.append((off, struct.pack('<I', v), ('display', v - disp_base),
                        'display ref'))

    margin = 0x80
    rlo, rhi = hull_lo - margin, hull_hi + margin
    seg = data[rlo:rhi]

    for m in re.finditer(SLOT_BOUND, seg, re.S):
        after = seg[m.end():m.end() + 10]
        if not (re.search(rb'[\x72\x73]', after[:6]) or re.search(rb'\x0f[\x82\x83]', after[:6])):
            raise PatchError('slot bound at %#x is not followed by a conditional jump'
                             % pe.off_to_va(rlo + m.start()))
        patches.append((rlo + m.end() - 1, b'\x12', bytes([slots]), 'display bound'))

    for pat in (rb'\x3d\x40\x02\x00\x00', rb'\x81[\xf8-\xff]\x40\x02\x00\x00'):
        for m in re.finditer(pat, seg):
            imm = rlo + m.end() - 4
            patches.append((imm, struct.pack('<I', disp_size),
                            struct.pack('<I', slots * DISPLAY_STRIDE), 'display loop limit'))

    for m in re.finditer(rb'\x68\x40\x02\x00\x00', seg):
        imm = rlo + m.end() - 4
        patches.append((imm, struct.pack('<I', disp_size),
                        struct.pack('<I', alloc * DISPLAY_STRIDE), 'display memset'))

    patches.append((unroll_off, b'\x8d\x70\x03', bytes([0x8d, 0x70, alloc // UNROLL]),
                    'display unroll'))

    # ---- records array -------------------------------------------------
    rec_base, rec_bound, rec_bounds = record_accessors(pe, lo, hi)
    if rec_bound != OLD_COUNT:
        raise PatchError('record accessors are bounded at %d, expected %d'
                         % (rec_bound, OLD_COUNT))
    rec_size = OLD_COUNT * RECORDS_STRIDE
    rec_refs = records_refs(pe, lo, hi, rec_base, rec_size)
    if len(rec_refs) < 6:
        raise PatchError('found only %d references to the records array' % len(rec_refs))

    for off, v in rec_refs:
        patches.append((off, struct.pack('<I', v), ('records', v - rec_base), 'records ref'))

    # the six accessors, plus any other cmp-18 next to a reference (the per-slot clear)
    bound_offs = set(rec_bounds)
    for m in re.finditer(rb'\x83[\xf8-\xff]\x12', bytes(pe.d[lo:hi])):
        off = lo + m.start()
        if any(abs(off - r) <= 0x60 for r, _ in rec_refs):
            bound_offs.add(off + 2)
    for off in sorted(bound_offs):
        patches.append((off, b'\x12', bytes([slots]), 'records bound'))

    for m in re.finditer(rb'\x68' + struct.pack('<I', rec_size), bytes(pe.d[lo:hi])):
        imm = lo + m.end() - 4
        if any(abs(imm - r) <= 0x80 for r, _ in rec_refs):
            patches.append((imm, struct.pack('<I', rec_size),
                            struct.pack('<I', alloc * RECORDS_STRIDE), 'records memset'))

    # ---- loot packet handler -------------------------------------------
    patches.append((locate_count_clamp(pe, lo, hi, rec_refs), b'\x12', bytes([slots]),
                    'packet count clamp'))

    layout['display'] = (disp_base, DISPLAY_STRIDE, len(disp_refs))
    layout['records'] = (rec_base, RECORDS_STRIDE, len(rec_refs))
    return patches, layout


def check_already_patched(pe, lo, hi, slots):
    """Return True when there is nothing to do; raise when the client can't be patched."""
    _, current, _ = record_accessors(pe, lo, hi)
    hint = 'patch the unpatched wow.exe instead (wow.exe.bak if it was patched in place)'
    if re.search(COUNT_CLAMP, bytes(pe.d[lo:hi]), re.S):
        raise PatchError('patched for %d slots by an older version of this script, which left '
                         'the loot packet handler capped at 18 items; %s' % (current, hint))
    if current != slots:
        raise PatchError('already patched for %d slots; %s' % (current, hint))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('exe', help='path to wow.exe')
    ap.add_argument('--slots', type=int, required=True,
                    help='loot slots the patched client takes (%d-%d); the server\'s '
                         'MaxLootItems must not exceed it' % (OLD_COUNT + 1, MAX_SLOTS))
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change without writing')
    ap.add_argument('--output', help='write here instead of patching in place')
    ap.add_argument('--no-backup', action='store_true')
    args = ap.parse_args()

    slots = args.slots
    if not OLD_COUNT < slots <= MAX_SLOTS:
        raise PatchError('--slots must be %d-%d' % (OLD_COUNT + 1, MAX_SLOTS))
    alloc = alloc_count(slots)

    raw = open(args.exe, 'rb').read()
    print('input      %s (%d bytes, md5 %s)'
          % (args.exe, len(raw), hashlib.md5(raw).hexdigest()))

    if BUILD_MARKER not in raw:
        raise PatchError('this does not look like a 3.3.5a build 12340 client')

    pe = PE(raw)
    lo, hi = text_bytes(pe)
    if pe.section(SECTION_NAME) is not None:
        check_already_patched(pe, lo, hi, slots)
        print('already patched for %d slots; nothing to do' % slots)
        return 0

    patches, layout = collect_patches(pe, lo, hi, slots)

    for name, (base, stride, nrefs) in layout.items():
        print('%-8s array %#x .. %#x  (%d x %#x)  %d refs'
              % (name, base, base + OLD_COUNT * stride, OLD_COUNT, stride, nrefs))

    kinds = {}
    for _, _, _, what in patches:
        kinds[what] = kinds.get(what, 0) + 1
    print('patches    %s' % ', '.join('%s=%d' % kv for kv in sorted(kinds.items())))

    seen = {}
    for off, old, _, what in patches:
        for i in range(off, off + len(old)):
            if i in seen:
                raise PatchError('patches overlap at %#x (%s vs %s)' % (i, what, seen[i]))
            seen[i] = what

    disp_alloc = alloc * DISPLAY_STRIDE
    rec_alloc = alloc * RECORDS_STRIDE
    sec_base = pe.append_bss_section(SECTION_NAME, disp_alloc + rec_alloc, SECTION_CHARS)
    new_base = {'display': sec_base, 'records': sec_base + disp_alloc}
    for name in ('display', 'records'):
        size = disp_alloc if name == 'display' else rec_alloc
        print('new %-6s %#x .. %#x  (%d entries allocated, %d usable)'
              % (name, new_base[name], new_base[name] + size, alloc, slots))

    for off, old, new, what in patches:
        if bytes(pe.d[off:off + len(old)]) != old:
            raise PatchError('expected %s at %#x, found %s'
                             % (old.hex(), off, bytes(pe.d[off:off + len(old)]).hex()))
        if isinstance(new, tuple):
            which, field = new
            new = struct.pack('<I', new_base[which] + field)
        if args.dry_run:
            print('  %#010x  %-18s %s -> %s'
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
    print('done; %d sites patched for %d slots' % (len(patches), slots))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except PatchError as ex:
        print('ERROR: %s' % ex, file=sys.stderr)
        sys.exit(1)
