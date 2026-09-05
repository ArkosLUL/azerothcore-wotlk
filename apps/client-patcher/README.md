# Client patcher

Binary patches for the WoW 3.3.5a (build 12340) client, for server features the stock client
cannot support.

## wow_loot32_patch.py — raise the loot window from 18 slots to 32

Pairs with the `MaxLootItems` option in `worldserver.conf`. Leave that at 18 until every player
on the realm runs a patched client.

### Why the client needs patching

The client keeps loot in a fixed global array and uses the slot number the server sends as a
direct index into it. In a stock client that array is 18 entries of 32 bytes at `0x00BFA690`,
and it ends at `0xBFA8D0` — exactly where the next global begins. A 19th slot therefore
overwrites the "loot window is open" state rather than landing in spare space. There is
nothing to enlarge in place, which is why this is a relocation rather than a changed constant.

The patcher appends a section to hold a 36-entry array, repoints every reference at it, and
raises the bounds.

### Usage

```
python apps/client-patcher/wow_loot32_patch.py <path-to-wow.exe> [--dry-run] [--output PATH]
```

- `--dry-run` lists every site it would change, with before/after bytes, and writes nothing.
- `--output PATH` writes a patched copy instead of modifying the input.
- Patching in place creates `wow.exe.bak` first, unless `--no-backup` is given.
- Re-running on an already patched file is a no-op.

The patcher refuses to run unless the file is a build 12340 client, and aborts if anything it
expects is missing or ambiguous rather than patching partially. The patched executable is a
build artifact and is not committed here.

### What it changes

| Change | Sites |
|---|---|
| References to the loot array repointed to the new section | 52 |
| Slot bound checks `cmp r32, 18` → `cmp r32, 32` | 21 |
| Loop limits over the array `0x240` → `0x400` bytes | 3 |
| Array memset sizes `0x240` → `0x480` bytes | 2 |
| Item counter unroll, 3 iterations → 6 | 1 |

The array gets room for 36 entries rather than 32 because the client's item counter is a 6-way
unrolled loop and 32 is not a multiple of 6; 36 lets it run six iterations unchanged. The four
spare slots stay zero — the bound checks cap real writes at 32.

Nothing is appended to the file: the new section carries no raw data and is zero-filled by the
loader, so the file size is unchanged and the Authenticode overlay at the end is left alone.
This also relies on the client having no ASLR and no relocation directory, so the absolute
addresses the patcher writes stay valid.

### Locating, not hardcoding

Addresses are derived from the file at run time, from two independent anchors that must agree
on the array base:

- `Script_GetLootSlotInfo` — `lea eax,[esi-1]` / `cmp eax,12h` / `shl eax,5` / `mov eax,[eax+base+0Ch]`
- the item counter prologue — `push esi` / `lea ecx,[eax+2]` / `mov edx,base+24h` / `lea esi,[eax+3]`

So the patch applies to a client carrying unrelated modifications, and fails loudly rather than
silently mispatching one it does not recognise.

### Caveats

- **Every player on the realm needs a patched client.** An unpatched one still has 18 slots and
  will corrupt its memory if the server sends more.
- The PE checksum is not recalculated. Windows does not verify it for a normal user-mode
  program, and it is already stale on any client patched by anything else.
- Test with a debugger attached the first time, so a fault stops somewhere useful.
