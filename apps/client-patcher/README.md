# Client patcher

Binary patches for the WoW 3.3.5a (build 12340) client, for server features the stock client
cannot support.

## wow_loot32_patch.py — raise the loot window from 18 slots to 32

Pairs with the `MaxLootItems` option in `worldserver.conf`. Leave that at 18 until every player
on the realm runs a patched client.

### Why the client needs patching

The client holds loot in **two** parallel 18-slot global arrays and uses the slot number the
server sends as a direct index into both:

| | Address | Layout | Ends at | Slack |
|---|---|---|---|---|
| records | `0x00C9D340` | 18 × `0x18` | `0xC9D4F0` | none |
| display | `0x00BFA690` | 18 × `0x20` | `0xBFA8D0` | none |

`records` holds the slots the loot packet is parsed into, reached through six bounds-checked
field accessors at `0x6CEB80`–`0x6CEC70` that call a fatal assert when the index is out of
range. `display` is a cache the loot frame reads, filled by a loop that copies six fields out
of `records` into it.

Both arrays end exactly where the next global begins, so neither can grow in place — which is
why this is a relocation rather than a changed constant. The patcher appends a section holding
a 36-entry copy of each, repoints every reference, and raises every bound.

Raising one array without the other does not work: the display loop then calls the record
accessors with an index they still reject, and the client dies on `ERROR #134 Fatal Condition`
the moment you loot anything.

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

### What it changes — 107 sites

| Change | Sites |
|---|---|
| `display` references repointed to the new section | 52 |
| `display` slot bound checks `cmp r32, 18` → `32` | 21 |
| `display` loop limits `0x240` → `0x400` bytes | 3 |
| `display` memset sizes `0x240` → `0x480` bytes | 2 |
| `display` item counter unroll, 3 iterations → 6 | 1 |
| `records` references repointed to the new section | 20 |
| `records` bound checks (6 accessors + slot clear) `18` → `32` | 7 |
| `records` memset size `0x1B0` → `0x360` bytes | 1 |

Each array gets room for 36 entries rather than 32 because the display array's item counter is
a 6-way unrolled loop and 32 is not a multiple of 6; 36 lets it run six iterations unchanged.
The four spare slots stay zero — the bound checks cap real writes at 32.

Nothing is appended to the file: the new section carries no raw data and is zero-filled by the
loader, so the file size is unchanged and the Authenticode overlay at the end is left alone.
This also relies on the client having no ASLR and no relocation directory, so the absolute
addresses the patcher writes stay valid.

### Locating, not hardcoding

Addresses are derived from the file at run time. The `display` base comes from two independent
anchors that must agree:

- `Script_GetLootSlotInfo` — `lea eax,[esi-1]` / `cmp eax,12h` / `shl eax,5` / `mov eax,[eax+base+0Ch]`
- the item counter prologue — `push esi` / `lea ecx,[eax+2]` / `mov edx,base+24h` / `lea esi,[eax+3]`

The `records` base comes from its six field accessors, which must be found exactly six times
and must describe a contiguous six-field struct.

References to `records` are additionally required to be `push imm32` or a `[reg*8 + disp32]`
SIB operand, since the array is always indexed as `index*3*8`. Without that filter a dword
straddling a ModRM byte and an immediate elsewhere in the binary matches by coincidence and
would be corrupted.

The patcher fails loudly rather than silently mispatching a client it does not recognise.

### Caveats

- **Every player on the realm needs a patched client.** An unpatched one still has 18 slots and
  will corrupt its memory if the server sends more.
- Two arrays were found by tracing from a crash. Nothing on the paths traced so far points at a
  third, but that cannot be proven from static analysis alone. If a new `ERROR #134` appears,
  the stack's innermost `wow.exe` frame names the accessor, and its `cmp reg,12h` names the next
  table.
- The PE checksum is not recalculated. Windows does not verify it for a normal user-mode
  program, and it is already stale on any client patched by anything else.
- Test with a debugger attached the first time, so a fault stops somewhere useful.
