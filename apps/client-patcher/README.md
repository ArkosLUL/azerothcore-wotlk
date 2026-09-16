# Client patcher

Binary patches for the WoW 3.3.5a (build 12340) client, for server features the stock client
cannot support.

## wow_loot_patch.py: raise the loot window above 18 slots

Pairs with the `MaxLootItems` option in `worldserver.conf`. Keep that at 18 until every player
on the realm runs a patched client, and never set it above the slot count the clients were
patched for.

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

Both arrays end exactly where the next global begins, so neither can grow in place, which is
why this is a relocation rather than a changed constant. The patcher appends a section holding
a larger copy of each, repoints every reference, and raises every bound.

Two more limits hold the client at 18 without touching either array by address:

- The loot packet handler (`0x6D53B0`) clamps the item count it reads to 18 before parsing,
  so without this patch a client shows at most 18 items and silently ignores the rest.
- The single-slot display update (`0x589A30`) bounds its free-entry search with
  `cmp dword [ebp-4], 12h`, a memory operand rather than a register.

Raising one array without the other does not work: the display loop then calls the record
accessors with an index they still reject, and the client dies on `ERROR #134 Fatal Condition`
the moment you loot anything.

### Usage

```
python apps/client-patcher/wow_loot_patch.py <path-to-wow.exe> --slots N [--dry-run] [--output PATH]
```

- `--slots N` is the slot count the patched client takes, 19 to 127. The server's
  `MaxLootItems` ceiling is 64.
- `--dry-run` lists every site it would change, with before/after bytes, and writes nothing.
- `--output PATH` writes a patched copy instead of modifying the input.
- Patching in place creates `wow.exe.bak` first, unless `--no-backup` is given.
- Re-running on a client already patched for the same count is a no-op. A client patched for a
  different count is refused; patch the unpatched original instead.

The patcher refuses to run unless the file is a build 12340 client, and aborts if anything it
expects is missing or ambiguous rather than patching partially. The patched executable is a
build artifact and is not committed here.

### What it changes: 109 sites

| Change | Sites | 32 slots | 64 slots |
|---|---|---|---|
| `display` references repointed to the new section | 52 | | |
| `display` slot bounds `18` → N | 22 | `0x20` | `0x40` |
| `display` loop limits `0x240` → N × `0x20` bytes | 3 | `0x400` | `0x800` |
| `display` memset sizes `0x240` → A × `0x20` bytes | 2 | `0x480` | `0x840` |
| `display` item counter unroll, 3 iterations → A / 6 | 1 | 6 | 11 |
| `records` references repointed to the new section | 20 | | |
| `records` bounds (6 accessors + slot clear) `18` → N | 7 | `0x20` | `0x40` |
| `records` memset size `0x1B0` → A × `0x18` bytes | 1 | `0x360` | `0x630` |
| loot packet item count clamp `18` → N | 1 | `0x20` | `0x40` |

A is the allocated entry count per array: N rounded up to a multiple of 6 (36 for 32 slots,
66 for 64). The display array's item counter is a 6-way unrolled loop, and rounding up lets it
run whole iterations unchanged. The spare slots stay zero because the bound checks cap real
writes at N.

Nothing is appended to the file: the new section carries no raw data and is zero-filled by the
loader, so the file size is unchanged and the Authenticode overlay at the end is left alone.
This also relies on the client having no ASLR and no relocation directory, so the absolute
addresses the patcher writes stay valid.

N tops out at 127 because the bounds are `cmp r/m32, imm8`, which the CPU sign-extends.

### Locating, not hardcoding

Addresses are derived from the file at run time. The `display` base comes from two independent
anchors that must agree:

- `Script_GetLootSlotInfo`: `lea eax,[esi-1]` / `cmp eax,12h` / `shl eax,5` / `mov eax,[eax+base+0Ch]`
- the item counter prologue: `push esi` / `lea ecx,[eax+2]` / `mov edx,base+24h` / `lea esi,[eax+3]`

The `records` base comes from its six field accessors, which must be found exactly six times
and must describe a contiguous six-field struct. The packet count clamp
(`mov al,12h` / `cmp [ebp+x],al` / `jbe` / `mov [ebp+x],al`) must be found exactly once, with
the handler's `records` memset following it.

References to `records` are additionally required to be `push imm32` or a `[reg*8 + disp32]`
SIB operand, since the array is always indexed as `index*3*8`. Without that filter a dword
straddling a ModRM byte and an immediate elsewhere in the binary matches by coincidence and
would be corrupted.

The patcher fails loudly rather than silently mispatching a client it does not recognise.

### Clients from the first version of this patcher

`wow_loot32_patch.py`, the first version, missed the packet count clamp and the memory-operand
bound. Its clients do not crash, but they still show at most 18 items. The current patcher
detects them and refuses to patch on top; patch the unpatched original again instead.

### Caveats

- **Every player on the realm needs a patched client.** An unpatched one still has 18 slots and
  will corrupt its memory if the server sends more. The same applies to a client patched for
  fewer slots than `MaxLootItems`.
- The inventory was checked by a linear disassembly of all of `.text`, flagging every
  slot-sized constant near loot code. Everything left over was traced to something unrelated
  (equipment and bag slot ranges, a separate 17-entry table, switch values). Static analysis
  cannot prove there is nothing else, though. If a new `ERROR #134` appears, the stack's
  innermost `wow.exe` frame names the accessor, and its `cmp reg,12h` names the next table.
- The PE checksum is not recalculated. Windows does not verify it for a normal user-mode
  program, and it is already stale on any client patched by anything else.
- Test with a debugger attached the first time, so a fault stops somewhere useful.
