#!/usr/bin/env python3
"""Group WoW 3.3.5a client crashes and freezes and match each one to the botobs trace running then.

Reads the client's own crash reports (Errors/*.txt), the Windows Error Reporting freeze records, and
the freeze folders watch-freezes.ps1 writes (Errors/Hangs/*). Prints Markdown.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import glob
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

UTC = dt.timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACES = REPO_ROOT / "env" / "dist" / "logs" / "botobs"
DEFAULT_WER = Path(r"C:\ProgramData\Microsoft\Windows\WER\ReportArchive")
CLIENT_VERSION = "3.3.5"
WINDOWS_DIRS = (
    "c:\\windows\\",
    "c:\\program files\\windows defender\\",
    "c:\\programdata\\microsoft\\windows defender\\",
)

# DLLs a stock 3.3.5a client ships with; anything else next to wow.exe was added later.
STOCK_CLIENT_DLLS = {
    "battle.net.dll", "dbghelp.dll", "divxdecoder.dll", "ijl15.dll", "msvcr80.dll", "scan.dll", "unicows.dll",
}

ERROR_KINDS = {"132": "crash", "134": "assert"}
TRACE_SLACK = dt.timedelta(minutes=2)
WER_LOOKBACK = dt.timedelta(minutes=15)
WER_MERGE = dt.timedelta(minutes=15)

REPORT_FRAME = re.compile(r"^([0-9A-F]{8}) [0-9A-F]{8} [0-9A-F]{4}:[0-9A-F]{8} (.+?)\s*$")
REPORT_MODULE = re.compile(r'^DBG-MODULE<([0-9A-F]{8}) ([0-9A-F]{8}) "([^"]+)"')
REPORT_ERROR = re.compile(r"^ERROR #(\d+) \((0x[0-9A-F]+)\) (.+?)\s*$", re.M)
REPORT_EXCEPTION = re.compile(r"^Exception:\s+(0x[0-9A-F]+) \((\w+)\) at [0-9A-F]{4}:([0-9A-F]{8})", re.M)
REPORT_ACCESS = re.compile(r'referenced memory at "(0x[0-9A-F]+)"\.\s*\nThe memory could not be "(\w+)"')
REPORT_FIELDS = {
    "Exe": "exe_path", "Time": "time", "Realm": "realm", "Local Zone": "zone", "Local Player": "player",
    "Current Object Track": "target", "Total lua memory": "lua_memory", "Add Ons": "addons",
}
CDB_FRAME = re.compile(r"^[0-9a-f`]{8,17}\s+[0-9a-f`]{8,17}\s+(\S+)")
CDB_MODULE = re.compile(r"^[0-9a-f`]{8,17}\s+[0-9a-f`]{8,17}\s+\S+\s+(\S.*?)\s*$")
CDB_COMMAND = ".echo ==STACK==; ~0 k 30; .echo ==MODULES==; lmf; .echo ==END==; q"
TRACE_NAME = re.compile(r"^(\d+)_(\d+)_(.+)_(\d+)\.ndjson$")


@dataclass(frozen=True)
class Frame:
    module: str
    offset: int | None = None
    symbol: str = ""

    def __str__(self) -> str:
        if self.symbol:
            return f"{self.module}!{self.symbol}"
        if self.offset is None:
            return self.module
        return f"{self.module}+0x{self.offset:X}"


@dataclass(frozen=True)
class Trace:
    map_id: int
    difficulty: int
    boss: str
    start: dt.datetime
    end: dt.datetime
    path: Path


@dataclass
class Event:
    kind: str
    when: dt.datetime
    exe: str
    source: Path
    where: str = ""
    frames: list[Frame] = field(default_factory=list)
    last_frames: list[Frame] = field(default_factory=list)
    info: dict[str, str] = field(default_factory=dict)
    third_party: set[str] = field(default_factory=set)
    precise: bool = True
    stuck: bool | None = None
    dumps: list[Path] = field(default_factory=list)
    evidence: Path | None = None
    wer: Path | None = None
    traces: list[Trace] = field(default_factory=list)
    traces_saved: bool = False
    group: str = "-"


def to_utc(naive: dt.datetime, tz: dt.tzinfo | None) -> dt.datetime:
    # A naive datetime's astimezone() reads it as system local time, DST rules included.
    return naive.astimezone(UTC) if tz is None else naive.replace(tzinfo=tz).astimezone(UTC)


def to_local(when: dt.datetime, tz: dt.tzinfo | None) -> dt.datetime:
    return when.astimezone(tz) if tz is not None else when.astimezone()


def filetime_to_utc(ticks: int) -> dt.datetime:
    return dt.datetime(1601, 1, 1, tzinfo=UTC) + dt.timedelta(microseconds=ticks // 10)


def normalize_module(name: str, exe: str) -> str:
    low = name.lower()
    stem = exe.lower().removesuffix(".exe")
    return "wow.exe" if low in (exe.lower(), stem) else low


@functools.lru_cache(maxsize=None)
def is_windows_module(name: str) -> bool:
    for folder in (r"C:\Windows\System32", r"C:\Windows\SysWOW64"):
        if os.path.isfile(os.path.join(folder, name)):
            return True
    return bool(glob.glob(os.path.join(r"C:\Windows\System32\DriverStore\FileRepository", "*", glob.escape(name))))


def classify_third_party(modules: dict[str, str], exe: str, windows_check=is_windows_module) -> set[str]:
    """modules maps a lower-case DLL name to its path, or to "" when only the name is known."""
    found = set()
    for name, path in modules.items():
        if name in (exe.lower(), "wow.exe") or name in STOCK_CLIENT_DLLS:
            continue
        if path:
            if path.lower().startswith(WINDOWS_DIRS):
                continue
        elif windows_check(name):
            continue
        found.add(name)
    return found


def parse_client_report(path: Path, tz: dt.tzinfo | None, windows_check=is_windows_module) -> Event | None:
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    head: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key in REPORT_FIELDS and REPORT_FIELDS[key] not in head:
            head[REPORT_FIELDS[key]] = value.strip()
    error = REPORT_ERROR.search(text)
    if "exe_path" not in head or "time" not in head or not error:
        return None

    exe = Path(head["exe_path"].replace("\\", "/")).name
    local = dt.datetime.strptime(" ".join(head["time"].split()), "%b %d, %Y %I:%M:%S.%f %p")
    event = Event(kind=ERROR_KINDS.get(error.group(1), f"error #{error.group(1)}"), when=to_utc(local, tz), exe=exe,
                  source=path)
    event.info["error"] = f"ERROR #{error.group(1)} {error.group(3)}"
    for key in ("realm", "zone", "player", "target", "lua_memory", "addons"):
        if head.get(key):
            event.info[key] = head[key]

    modules = [(int(b, 16), int(s, 16), n) for b, s, n in
               (m.groups() for m in map(REPORT_MODULE.match, lines) if m)]

    def locate(addr: int) -> Frame:
        for base, size, name in modules:
            if base <= addr < base + size:
                return Frame(normalize_module(name, exe), addr - base)
        return Frame("?", addr)

    module_paths = {name.lower(): "" for _, _, name in modules}
    in_manual = in_current = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Stack Trace (Manual)":
            in_manual = True
            continue
        if stripped.startswith("Stack Trace (Using DBGHELP"):
            break
        if not in_manual:
            continue
        if line.startswith("--- Thread ID"):
            in_current = "[Current Thread]" in line
            continue
        frame = REPORT_FRAME.match(line)
        if not frame:
            continue
        module_path = frame.group(2)
        module_paths[Path(module_path.replace("\\", "/")).name.lower()] = module_path
        if in_current:
            event.frames.append(locate(int(frame.group(1), 16)))
    event.third_party = classify_third_party(module_paths, exe, windows_check)

    exception = REPORT_EXCEPTION.search(text)
    if exception:
        event.info["exception"] = f"{exception.group(1)} {exception.group(2)}"
        event.where = str(locate(int(exception.group(3), 16)))
        access = REPORT_ACCESS.search(text)
        if access:
            event.info["access"] = f"{access.group(2)} of {access.group(1)}"
    else:
        event.where = ", ".join(str(f) for f in event.frames[:3])

    dump = path.with_suffix(".dmp")
    if dump.exists():
        event.dumps.append(dump)
    evidence = path.with_name(path.stem + ".evidence")
    if evidence.is_dir():
        event.evidence = evidence
    return event


def parse_wer(folder: Path, client: Path | None = None) -> Event | None:
    report = folder / "Report.wer"
    if not report.is_file():
        return None
    data = report.read_bytes()
    text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8", "replace")
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key] = value
    sig = {values[f"Sig[{i}].Name"]: values.get(f"Sig[{i}].Value", "") for i in range(32)
           if f"Sig[{i}].Name" in values}
    if not sig.get("Application Version", "").startswith(CLIENT_VERSION) or "EventTime" not in values:
        return None
    app_path = values.get("AppPath", "")
    if client is not None and app_path and \
            os.path.normcase(os.path.dirname(app_path)) != os.path.normcase(str(client).rstrip("\\/")):
        return None

    exe = sig.get("Application Name", "?")
    event_type = values.get("EventType", "")
    event = Event(kind="freeze", when=filetime_to_utc(int(values["EventTime"])), exe=exe, source=folder, wer=folder)
    if event_type.startswith("AppHang"):
        event.precise = False
        event.where = f"hang signature {sig.get('Hang Signature', '?')}"
    elif event_type.upper() == "APPCRASH":
        event.kind = "crash"
        module = normalize_module(sig.get("Fault Module Name", "?"), exe)
        offset = sig.get("Exception Offset", "")
        # StackHash_* records carry a placeholder like PCH_03 instead of an offset
        event.where = f"{module}+0x{int(offset, 16):X}" if re.fullmatch(r"[0-9a-fA-F]+", offset) else \
            f"{module} {offset}".strip()
        event.info["exception"] = sig.get("Exception Code", "")
    else:
        return None

    paths = {Path(v.replace("\\", "/")).name.lower(): v for k, v in values.items() if k.startswith("LoadedModule[")}
    event.third_party = classify_third_party(paths, exe, lambda _name: False)
    return event


def parse_cdb_frames(lines: list[str], exe: str) -> list[Frame]:
    frames = []
    for line in lines:
        m = CDB_FRAME.match(line)
        if not m:
            continue
        token = m.group(1)
        if "!" in token:
            module, symbol = token.split("!", 1)
            frames.append(Frame(normalize_module(module, exe), symbol=symbol))
        elif "+0x" in token:
            module, offset = token.split("+0x", 1)
            frames.append(Frame(normalize_module(module, exe), int(offset, 16)))
        else:
            try:
                frames.append(Frame("?", int(token.replace("`", ""), 16)))
            except ValueError:
                continue
    return frames


def split_cdb_output(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    stack: list[str] = []
    modules: list[str] = []
    target = None
    for line in lines:
        marker = line.strip()
        if marker == "==STACK==":
            target = stack
        elif marker == "==MODULES==":
            target = modules
        elif marker == "==END==" or marker.startswith("Unloaded modules"):
            target = None
        elif target is not None:
            target.append(line)
    return stack, modules


def cdb_output(dump: Path, cdb_lookup) -> str | None:
    """Main-thread stack and module list of a dump, cached beside it since cdb takes a while on a full dump."""
    cache = dump.with_suffix(".stack.txt")
    if cache.is_file():
        return cache.read_text(encoding="utf-8", errors="replace")
    cdb = cdb_lookup()
    if not cdb:
        return None
    try:
        run = subprocess.run([cdb, "-z", str(dump), "-c", CDB_COMMAND], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=900)
    except (OSError, subprocess.SubprocessError):
        return None
    stack, modules = split_cdb_output(run.stdout)
    if not stack:
        return None
    text = "\n".join(["==STACK==", *stack, "==MODULES==", *modules, "==END=="]) + "\n"
    try:
        cache.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def read_freeze_meta(folder: Path) -> dict[str, str]:
    meta = {}
    note = folder / "freeze.txt"
    if note.is_file():
        for line in note.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
    return meta


def parse_freeze(folder: Path, cdb_lookup, windows_check=is_windows_module) -> Event | None:
    meta = read_freeze_meta(folder)
    if "detected_utc" not in meta:
        return None
    when = dt.datetime.strptime(meta["detected_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    exe = meta.get("exe", "?")
    event = Event(kind="freeze", when=when, exe=exe, source=folder, evidence=folder)
    for key in ("outcome", "follow_ups"):
        if meta.get(key):
            event.info[key] = meta[key]
    minis = sorted(folder.glob("mini-*.dmp"))
    event.dumps = sorted(folder.glob("*.dmp"))
    if not minis:
        event.where = "no dump written"
        return event

    first = cdb_output(minis[0], cdb_lookup)
    if first is None:
        event.where = f"no stack yet, open with: WinDbgX -z \"{minis[0]}\""
        return event
    stack, module_lines = split_cdb_output(first)
    event.frames = parse_cdb_frames(stack, exe)
    paths = {}
    for line in module_lines:
        m = CDB_MODULE.match(line)
        if m and ("\\" in m.group(1) or "/" in m.group(1)):
            paths[Path(m.group(1).replace("\\", "/")).name.lower()] = m.group(1)
    event.third_party = classify_third_party(paths, exe, windows_check)

    if len(minis) > 1:
        last = cdb_output(minis[-1], cdb_lookup)
        if last is not None:
            event.last_frames = parse_cdb_frames(split_cdb_output(last)[0], exe)
            if event.frames and event.last_frames:
                event.stuck = event.frames[0] == event.last_frames[0]
    top = str(event.frames[0]) if event.frames else "empty stack"
    state = {True: " (stuck)", False: " (moving)", None: ""}[event.stuck]
    event.where = top + state
    return event


def load_traces(folder: Path) -> list[Trace]:
    traces = []
    if not folder.is_dir():
        return traces
    for path in folder.glob("*.ndjson"):
        m = TRACE_NAME.match(path.name)
        if not m:
            continue
        start = dt.datetime.fromtimestamp(int(m.group(4)), UTC)
        end = dt.datetime.fromtimestamp(path.stat().st_mtime, UTC)
        traces.append(Trace(int(m.group(1)), int(m.group(2)), m.group(3), start, max(start, end), path))
    return sorted(traces, key=lambda t: t.start)


def match_traces(event: Event, traces: list[Trace]) -> list[Trace]:
    if event.precise:
        return [t for t in traces if t.start - TRACE_SLACK <= event.when <= t.end + TRACE_SLACK]
    window_start = event.when - WER_LOOKBACK
    return [t for t in traces if t.start <= event.when and t.end >= window_start]


def merge_wer_freezes(events: list[Event]) -> list[Event]:
    """A Windows freeze record logged after a caught freeze describes the same freeze."""
    folders = [e for e in events if e.kind == "freeze" and e.precise]
    kept = []
    for event in events:
        if event.kind == "freeze" and not event.precise:
            twin = next((f for f in folders if f.exe.lower() == event.exe.lower() and f.wer is None
                         and dt.timedelta(0) <= event.when - f.when <= WER_MERGE), None)
            if twin is not None:
                twin.wer = event.source
                twin.third_party |= event.third_party
                continue
        kept.append(event)
    return kept


def group_key(event: Event):
    if event.kind == "crash" and event.where:
        return ("crash", event.where)
    if event.kind in ("assert", "freeze") and event.frames:
        return (event.kind, tuple(str(f) for f in event.frames[:3]))
    return None


def assign_groups(events: list[Event]) -> list[list[Event]]:
    buckets: dict = {}
    for event in events:
        key = group_key(event)
        if key is not None:
            buckets.setdefault(key, []).append(event)
    groups = sorted(buckets.values(), key=lambda evs: (-len(evs), min(e.when for e in evs)))
    for number, members in enumerate(groups, 1):
        for event in members:
            event.group = f"G{number}"
    return groups


def collect(client: Path, wer_root: Path, traces_dir: Path, tz: dt.tzinfo | None, cdb_lookup,
            windows_check=is_windows_module) -> list[Event]:
    errors = client / "Errors"
    events = []
    for report in sorted(errors.glob("*.txt")):
        event = parse_client_report(report, tz, windows_check)
        if event:
            events.append(event)
    hangs = errors / "Hangs"
    if hangs.is_dir():
        for folder in sorted(p for p in hangs.iterdir() if p.is_dir()):
            event = parse_freeze(folder, cdb_lookup, windows_check)
            if event:
                events.append(event)
    if wer_root.is_dir():
        for folder in sorted(wer_root.iterdir()):
            if folder.is_dir() and folder.name.lower().startswith(("apphang_wow", "appcrash_wow")):
                event = parse_wer(folder, client)
                if event:
                    events.append(event)
    events = merge_wer_freezes(events)

    live = load_traces(traces_dir)
    for event in events:
        saved = load_traces(event.evidence / "traces") if event.evidence else []
        if saved:
            event.traces, event.traces_saved = match_traces(event, saved), True
        else:
            event.traces = match_traces(event, live)
    return sorted(events, key=lambda e: e.when)


def make_cdb_lookup(explicit: str | None):
    @functools.lru_cache(maxsize=1)
    def lookup() -> str | None:
        if explicit:
            return explicit if Path(explicit).is_file() else None
        try:
            run = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-AppxPackage Microsoft.WinDbg).InstallLocation"],
                                 capture_output=True, text=True, timeout=60)
            location = run.stdout.strip().splitlines()
            if location and (Path(location[0]) / "x86" / "cdb.exe").is_file():
                return str(Path(location[0]) / "x86" / "cdb.exe")
        except (OSError, subprocess.SubprocessError):
            pass
        kit = Path(r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe")
        if kit.is_file():
            return str(kit)
        return shutil.which("cdb")
    return lookup


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def fmt_time(when: dt.datetime, tz: dt.tzinfo | None) -> str:
    return to_local(when, tz).strftime("%Y-%m-%d %H:%M")


def fmt_trace(trace: Trace, tz: dt.tzinfo | None) -> str:
    start = to_local(trace.start, tz).strftime("%H:%M")
    end = to_local(trace.end, tz).strftime("%H:%M")
    return f"{trace.map_id}/{trace.difficulty} {trace.boss} {start}-{end}"


def fmt_traces(event: Event, tz: dt.tzinfo | None) -> str:
    if not event.traces:
        return ""
    text = ", ".join(fmt_trace(t, tz) for t in event.traces)
    return text + (" (saved)" if event.traces_saved else "")


def fmt_place(event: Event) -> str:
    parts = [event.info.get("zone", ""), event.info.get("player", "").split(",")[0]]
    return " / ".join(p for p in parts if p)


def kind_label(event: Event) -> str:
    return "freeze (WER only)" if event.kind == "freeze" and not event.precise else event.kind


def render(events: list[Event], groups: list[list[Event]], tz: dt.tzinfo | None, client: Path,
           traces_dir: Path) -> str:
    counts = {k: sum(1 for e in events if e.kind == k) for k in ("crash", "assert", "freeze")}
    offset = to_local(dt.datetime.now(UTC), tz).strftime("%z")
    out = ["# Client crashes and freezes", ""]
    out.append(f"Client: `{client}`. {len(events)} events: {counts['crash']} crashes, {counts['assert']} asserts, "
               f"{counts['freeze']} freezes. Times are local (UTC{offset[:3]}:{offset[3:]} now).")
    if not traces_dir.is_dir():
        out.append(f"Trace folder `{traces_dir}` not found, so only saved traces are matched.")
    out += ["", "## Groups", "",
            "| Group | Kind | Count | First seen | Last seen | Where | Top frames | Third-party modules |",
            "|---|---|---|---|---|---|---|---|"]
    for members in groups:
        first = members[0]
        frames = ", ".join(str(f) for f in first.frames[:3])
        modules = ", ".join(sorted(set().union(*(e.third_party for e in members))))
        out.append(f"| {first.group} | {first.kind} | {len(members)} | {fmt_time(min(e.when for e in members), tz)} "
                   f"| {fmt_time(max(e.when for e in members), tz)} | {cell(first.where)} | {cell(frames)} "
                   f"| {cell(modules)} |")
    out += ["", "## Events", "",
            "| Local time | UTC | Kind | Exe | Where | Zone / player | Group | Server trace |",
            "|---|---|---|---|---|---|---|---|"]
    for e in events:
        out.append(f"| {fmt_time(e.when, tz)} | {e.when.strftime('%H:%M')} | {kind_label(e)} | {e.exe} "
                   f"| {cell(e.where)} | {cell(fmt_place(e))} | {e.group} | {cell(fmt_traces(e, tz))} |")
    return "\n".join(out) + "\n"


def render_detail(label: str, events: list[Event], tz: dt.tzinfo | None) -> str:
    members = [e for e in events if e.group == label]
    if not members:
        return f"No group {label}.\n"
    out = [f"# {label}: {len(members)} x {members[0].kind}", ""]
    names = {"error": "Error", "exception": "Exception", "access": "Access", "zone": "Zone", "player": "Player",
             "target": "Target", "realm": "Realm", "lua_memory": "Lua memory", "outcome": "Outcome",
             "follow_ups": "Follow-up dumps", "addons": "Addons"}
    for e in members:
        out += [f"## {fmt_time(e.when, tz)} {kind_label(e)} in {e.exe}", ""]
        out.append(f"- Source: `{e.source}`")
        for key, name in names.items():
            if e.info.get(key):
                out.append(f"- {name}: {e.info[key]}")
        if e.wer and e.wer != e.source:
            out.append(f"- Windows record: `{e.wer}`")
        if e.evidence and e.evidence != e.source:
            out.append(f"- Evidence: `{e.evidence}`")
        if e.kind == "freeze" and e.dumps:
            out.append(f"- Dumps: {', '.join(d.name for d in e.dumps)}")
        else:
            out += [f"- Dump: `{dump}`" for dump in e.dumps]
        if e.third_party:
            out.append(f"- Third-party modules: {', '.join(sorted(e.third_party))}")
        for trace in e.traces:
            out.append(f"- Trace: {fmt_trace(trace, tz)} `{trace.path}`")
        if e.frames:
            title = "Main thread, first dump" if e.kind == "freeze" else "Faulting thread"
            out += ["", f"{title}:", "", "```", *(str(f) for f in e.frames), "```"]
        if e.last_frames:
            out += ["", "Main thread, last dump:", "", "```", *(str(f) for f in e.last_frames), "```"]
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client", default=os.environ.get("WOW_CLIENT_DIR"),
                        help="WoW client folder (default: env WOW_CLIENT_DIR)")
    parser.add_argument("--traces", default=str(DEFAULT_TRACES), help="botobs trace folder")
    parser.add_argument("--wer", default=str(DEFAULT_WER), help="Windows Error Reporting archive")
    parser.add_argument("--since", help="only events on or after this local date, YYYY-MM-DD")
    parser.add_argument("--cdb", help="path to the x86 cdb.exe (default: the WinDbg app, then PATH)")
    parser.add_argument("--detail", metavar="GROUP", help="print every event of one group, e.g. G2")
    args = parser.parse_args(argv)
    if not args.client:
        parser.error("--client or WOW_CLIENT_DIR is required")
    client = Path(args.client)
    if not (client / "Errors").is_dir():
        parser.error(f"no Errors folder in {client}")

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    traces_dir = Path(args.traces)
    events = collect(client, Path(args.wer), traces_dir, None, make_cdb_lookup(args.cdb))
    if args.since:
        since = dt.date.fromisoformat(args.since)
        events = [e for e in events if to_local(e.when, None).date() >= since]
    groups = assign_groups(events)
    if args.detail:
        print(render_detail(args.detail.upper(), events, None), end="")
    else:
        print(render(events, groups, None, client, traces_dir), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
