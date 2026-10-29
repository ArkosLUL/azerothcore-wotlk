# Client crash and freeze tools

Tools for finding out why the WoW 3.3.5a (build 12340) client crashes or freezes on this realm:

- `watch-freezes.ps1` dumps the client while it is frozen and saves the server-side evidence.
- `report.py` groups every crash and freeze on record and names the botobs trace that was running.

## What gets written where

| Event | Client writes | Windows writes |
|---|---|---|
| Crash (`ERROR #132`, an exception) | `Errors\<time> Crash.txt` + `.dmp` | nothing, the client's handler catches it |
| Assert (`ERROR #134`, a failed check) | `Errors\<time> Error.txt` + `.dmp` | nothing |
| Crash past the client's handler | nothing | `AppCrash_*` in the WER archive, often a `StackHash_*` placeholder |
| Freeze ("not responding", then closed) | nothing | `AppHang_*` in the WER archive: a hash and modules, no stack |

The WER archive is `C:\ProgramData\Microsoft\Windows\WER\ReportArchive`. Each client report holds the
faulting address, a stack for every thread as raw addresses (wow.exe has no public symbols), the module
list, the addons, and in world the zone, player position and target. The client's `Logs\` folder is
overwritten on every launch.

A freeze therefore leaves nothing to debug unless something dumps the client while it is still frozen,
which is what `watch-freezes.ps1` does.

## Catching freezes

Needs ProcDump: unzip `https://download.sysinternals.com/files/Procdump.zip` into
`%LOCALAPPDATA%\Programs\Sysinternals\` (or put it on `PATH`, or pass `-ProcDump`). Then create the launcher
once:

```
powershell -File apps\client-crash\watch-freezes.ps1 -Launch "<client>\wow_loot64.exe" -CreateShortcut
```

This writes `WoW + freeze catcher.lnk` next to the exe. It starts the game with the catcher running in a
minimized console, and the catcher exits after the game does. `-ProcessId <pid>` attaches to a client that
is already running, and with neither option it waits for any 3.3.5 client and watches each in turn.

What it does:

- Arms 30 s after launch (`-ArmAfterSeconds`), so loading the data files at startup doesn't count.
- Starts dumping when the window ignores a no-op message for 5 s (`-FreezeSeconds`). This is the test
  behind Windows' "not responding".
- Writes a mini dump, then another every 2 s while the window stays silent, up to 8 more
  (`-FollowUpSeconds`, `-FollowUps`).
- If the window answers or the game exits before the last of those, it was a **pause**: the mini dumps are
  all it gets. Otherwise it is a **freeze**: it writes a full dump and beeps three times, about 21 s plus
  the full dump's time into it. After the beep, closing the game loses nothing.
- Puts everything in `Errors\Hangs\<date time> <exe>\`: `mini-00.dmp`, `mini-01.dmp`..., `full.dmp`, a copy
  of `Logs\`, `procdump.log`, and `freeze.txt` with the detection time (UTC), the kind (`pause` or
  `freeze`) and the outcome: `recovered after N s`, `game exited` (a pause) or `game closed while frozen`.
- Once the game has exited, saves the server side next to every freeze and every new client crash report
  (`Errors\<report name>.evidence\` for those): `worldserver.log` holds `docker logs ac-worldserver` from
  5 min before to 1 min after, and `traces\` the botobs traces overlapping the last 15 min. It waits
  until a minute after the event if needed. Botobs traces age out after 4-5 days, so this copy is often the
  only one left by the time someone looks.
- Keeps the newest 5 freeze folders and 5 crash evidence folders (`-Keep`). Pauses and recovered freezes are
  counted apart, so a run of them can't push the real freezes out.

The client is suspended while ProcDump writes a dump. A mini dump is about 14 MB and takes 0.2 s. A full dump
was 0.8 GB and 3.6 s at the login screen and 1.2-1.3 GB and 8 s in the world, and grows with the client's
memory in a raid, up to a few GB. That is why only a freeze gets one: written at the start, it stretched every
pause by 8 s.

### Traps

- **ProcDump must not run from the client folder.** The client ships a 2005 `dbghelp.dll` (6.6). Started with
  the client folder as working directory, ProcDump loads it and every dump fails with
  `Error writing dump file: 0x80070057`. The catcher runs ProcDump from ProcDump's own folder.
- **ProcDump's own hung-window trigger (`-h`) is not used.** It never fired on a paused test window that
  had been failing the message test for 30 s.
- **Testing:** pause the whole client (`NtSuspendProcess`) for about a minute after it has armed. A paused
  window fails the message test just like a real freeze. Windows' `IsHungAppWindow` stays false for a
  window that was idle-waiting for input when paused, so it is no use as a test check.

## The report

```
python apps\client-crash\report.py --client "<client>" [--since YYYY-MM-DD] [--detail G3]
```

`--client` defaults to `WOW_CLIENT_DIR`. The report reads the client's reports, the WER records of this client
(build 3.3.5, program path inside `--client`), and the freeze and pause folders. It prints two Markdown tables:

- **Groups**: crashes with the same faulting `module+offset`, and asserts, caught freezes or pauses with the
  same kind and top three frames, labelled G1, G2... by count. Offsets are relative to the module base, so a
  DLL loaded at a different address each run still lands in one group. `wow.exe`, `wow_loot32.exe` and `wow_loot64.exe`
  all count as `wow.exe`, since the loot patch adds a section without moving any code.
- **Events**: one row per crash, assert, freeze or pause, in local time and UTC, with the zone and player when the
  client recorded them, and the botobs traces running then as `map/difficulty boss start-end`.

`--detail G<n>` prints every event of one group: the full faulting-thread stack, addons, Lua memory, dumps,
saved evidence, and third-party modules. Third-party means not shipped with Windows (System32, SysWOW64,
the driver store, Defender) and not part of a stock client.

For a caught freeze or pause the report runs the x86 `cdb.exe` from the WinDbg app (found through
`Get-AppxPackage Microsoft.WinDbg`, else `--cdb`) on the first and last mini dump. It reads thread 0, which
is the client's main thread: its bottom frames match the main thread in the client's own crash reports.
If the top frame is the same in both dumps, the freeze is marked `stuck`, otherwise `moving`.
The cdb output is cached as `mini-NN.stack.txt` next to the dump. A freeze without a dump shows the
`WinDbgX -z` command to open it by hand.

Traces match a crash or caught freeze within 2 min of the pull's start and end. A Windows-only freeze
record is written when the window gets closed, possibly minutes into the freeze, so it matches every pull
of the 15 min before. A Windows record within 15 min after a caught freeze or pause of the same exe is folded
into it.

Symbols for the game-folder `d3d9.dll` are the nearest export (`Direct3DCreate9On12Ex+0x...`), not real
function names.

## Known groups (2026-10-30)

- **Session-end pause**, whenever the client's server connection closes: a server restart, a kick (the AFK
  kick too), or quitting the game. The main thread sits in `wow.exe+0x46B28D` (a `Sleep` wrapper) called from
  `wow.exe+0x47DF79`. That function, starting at `wow.exe+0x47DED0`, closes every connection, checks every
  100 ms whether they have all closed, and destroys what is left after 20 tries. With the teardown around it,
  the window stays silent for more than 5 s. Harmless, and the reason pauses get no full dump.

- **10 crashes, 9-16 Jun**, at `wow.exe+0x124868` reading `0x18`: a null pointer read in wow.exe, all in
  Naxxramas. None since 16 Jun.
- **7 crashes, 19-27 Jul**, at `wow_optimize.dll+0x3CC35`: a third-party optimizer DLL, since removed from
  the client.
- **5 asserts, 6-7 Sep**, in Ulduar while the loot patcher was being tested. The 7 Sep pair fails in the loot
  record accessors (`0x6CEB80`-`0x6CEC70`, third frame `wow.exe+0x2CEBA8`); the 6 Sep three in
  `wow.exe+0x2DC7D3`.
- **4 single crashes**, unexplained: 25 May `wow.exe+0x41D559`, 21 Jun in `ntdll`, 23 Sep `wow.exe+0x4E47C2`,
  16 Oct `wow.exe+0x39ABB5`.
- **11 freezes, Jun-Oct**, WER records only. Of the three in October, 21 Oct and 28 Oct 23:21 happened during
  ToC pulls, and 28 Oct 21:18 with no traced raid pull.

The client in use loads several third-party hooks: the game-folder `d3d9.dll` (a D3D9 wrapper), OBS
`graphics-hook32.dll`, RivaTuner `RTSSHooks.dll` and `RTSSVkLayer32.dll`, and an AMD Vulkan driver
(`amdvlk32.dll`) on an NVIDIA machine. None is proven to cause the freezes yet. They are the first thing to
compare against a caught freeze's main-thread stack.
