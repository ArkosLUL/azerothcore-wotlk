import contextlib
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import report  # noqa: E402

FIXTURES = HERE / "fixtures"
SUMMER = dt.timezone(dt.timedelta(hours=3))
UTC = dt.timezone.utc
WINDOWS = {"ntdll.dll", "kernel32.dll"}


def windows_check(name):
    return name in WINDOWS


def no_cdb():
    raise AssertionError("cdb must not run when the stack is cached")


def write_wer(root, name, fixture, app_dir=None, version=None):
    text = (FIXTURES / "wer" / fixture).read_text(encoding="utf-8")
    if app_dir is not None:
        text = text.replace("A:\\Games\\WoW", str(app_dir))
    if version is not None:
        text = text.replace("3.3.5.12340", version)
    folder = Path(root) / name
    folder.mkdir(parents=True)
    (folder / "Report.wer").write_bytes(text.replace("\n", "\r\n").encode("utf-16"))
    return folder


def make_trace(folder, name, start, end):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (end.timestamp(), end.timestamp()))
    return path


def make_freeze(errors, last_stack="first.stack.txt", detected="2026-10-28T21:21:00Z", kind=None,
                outcome="game closed while frozen"):
    folder = Path(errors) / "Hangs" / f"{detected[:19].replace('T', ' ').replace(':', '.')} wow_loot64.exe"
    folder.mkdir(parents=True)
    kind_line = f"kind: {kind}\n" if kind else ""
    (folder / "freeze.txt").write_text(
        f"exe: wow_loot64.exe\npid: 1234\ndetected_utc: {detected}\n{kind_line}follow_ups: 3\n"
        f"outcome: {outcome}\n", encoding="ascii")
    for name in ("mini-00.dmp", "mini-03.dmp", "full.dmp"):
        (folder / name).write_bytes(b"")
    shutil.copy(FIXTURES / "cdb" / "first.stack.txt", folder / "mini-00.stack.txt")
    shutil.copy(FIXTURES / "cdb" / last_stack, folder / "mini-03.stack.txt")
    return folder


class ClientReportTest(unittest.TestCase):
    def parse(self, name):
        path = FIXTURES / "client" / "Errors" / name
        return report.parse_client_report(path, SUMMER, windows_check)

    def test_crash_in_wow(self):
        event = self.parse("2026-06-13 20.13.12 Crash.txt")
        self.assertEqual(event.kind, "crash")
        self.assertEqual(event.exe, "wow.exe")
        self.assertEqual(event.when, dt.datetime(2026, 6, 13, 17, 13, 12, 66000, UTC))
        self.assertEqual(event.where, "wow.exe+0x124868")
        self.assertEqual([str(f) for f in event.frames[:3]],
                         ["wow.exe+0x124868", "wow.exe+0x124900", "wow.exe+0x2D91CE"])
        self.assertEqual(str(event.frames[-1]), "ntdll.dll+0x6C75B")
        self.assertEqual(event.info["access"], "read of 0x00000018")
        self.assertEqual(event.info["zone"], "Naxxramas")
        self.assertEqual(event.info["addons"], "AtlasLoot DBM-Core Skada")

    def test_third_party_modules(self):
        event = self.parse("2026-06-13 20.13.12 Crash.txt")
        self.assertEqual(event.third_party, {"d3d9.dll", "graphics-hook32.dll", "rtsshooks.dll"})

    def test_same_dll_offset_at_different_bases_is_one_group(self):
        first = self.parse("2026-07-19 11.49.08 Crash.txt")
        second = self.parse("2026-07-23 00.00.34 Crash.txt")
        self.assertEqual(first.where, "wow_optimize.dll+0x3CC35")
        self.assertEqual(second.where, "wow_optimize.dll+0x3CC35")
        groups = report.assign_groups([first, second])
        self.assertEqual(len(groups), 1)
        self.assertEqual({first.group, second.group}, {"G1"})

    def test_assert_in_patched_exe(self):
        event = self.parse("2026-09-07 11.41.22 Error.txt")
        self.assertEqual(event.kind, "assert")
        self.assertEqual(event.exe, "wow_loot32.exe")
        self.assertEqual(event.when, dt.datetime(2026, 9, 7, 8, 41, 22, 436000, UTC))
        self.assertEqual(event.where, "wow.exe+0x372AB5, wow.exe+0x4889CE, wow.exe+0x2CEBA8")
        self.assertTrue(event.info["target"].startswith("Arachnopod Destroyer"))
        self.assertNotIn("exception", event.info)

    def test_crlf_report(self):
        source = FIXTURES / "client" / "Errors" / "2026-06-13 20.13.12 Crash.txt"
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / source.name
            copy.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
            event = report.parse_client_report(copy, SUMMER, windows_check)
        self.assertEqual(event.where, "wow.exe+0x124868")
        self.assertEqual(len(event.frames), 5)


class WerTest(unittest.TestCase):
    def test_filetime(self):
        self.assertEqual(report.filetime_to_utc(134376960657569694),
                         dt.datetime(2026, 10, 28, 21, 21, 5, 756969, UTC))

    def test_hang_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = report.parse_wer(write_wer(tmp, "AppHang_wow_loot64.exe_1", "AppHang.Report.wer.txt"))
        self.assertEqual(event.kind, "freeze")
        self.assertFalse(event.precise)
        self.assertEqual(event.exe, "wow_loot64.exe")
        self.assertEqual(event.where, "hang signature 262b")
        self.assertEqual(event.third_party, {"d3d9.dll", "graphics-hook32.dll", "rtsshooks.dll"})

    def test_stackhash_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = report.parse_wer(write_wer(tmp, "AppCrash_WoW.exe_1", "AppCrash.Report.wer.txt"))
        self.assertEqual(event.kind, "crash")
        self.assertEqual(event.where, "stackhash_0000 PCH_03")

    def test_other_clients_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            beta = write_wer(tmp, "AppHang_wow_1", "AppHang.Report.wer.txt", version="1.60.1.70450")
            other = write_wer(tmp, "AppHang_wow_2", "AppHang.Report.wer.txt")
            self.assertIsNone(report.parse_wer(beta))
            self.assertIsNone(report.parse_wer(other, Path(tmp) / "elsewhere"))
            self.assertIsNotNone(report.parse_wer(other, Path("A:\\Games\\WoW")))


class FreezeTest(unittest.TestCase):
    def test_stuck_main_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = report.parse_freeze(make_freeze(tmp), no_cdb, windows_check)
        self.assertEqual(event.when, dt.datetime(2026, 10, 28, 21, 21, 0, tzinfo=UTC))
        self.assertTrue(event.stuck)
        self.assertEqual(event.where, "wow.exe+0x1A2B3 (stuck)")
        self.assertEqual(str(event.frames[2]), "d3d9+0x12345")
        self.assertEqual(str(event.frames[3]), "ntdll!RtlUserThreadStart+0x2f")
        self.assertEqual(event.third_party, {"d3d9.dll"})
        self.assertEqual(event.info["outcome"], "game closed while frozen")
        self.assertEqual(len(event.dumps), 3)

    def test_moving_main_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = report.parse_freeze(make_freeze(tmp, "moved.stack.txt"), no_cdb, windows_check)
        self.assertFalse(event.stuck)
        self.assertEqual(str(event.last_frames[0]), "wow.exe+0x1A2F0")

    def test_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = report.parse_freeze(make_freeze(tmp, kind="pause", outcome="recovered after 7 s"), no_cdb,
                                        windows_check)
        self.assertEqual(event.kind, "pause")
        self.assertEqual(event.info["outcome"], "recovered after 7 s")

    def test_no_debugger(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_freeze(tmp)
            for cache in folder.glob("*.stack.txt"):
                cache.unlink()
            event = report.parse_freeze(folder, lambda: None, windows_check)
        self.assertIsNone(event.stuck)
        self.assertIn("WinDbgX -z", event.where)

    def test_cdb_frames(self):
        frames = report.parse_cdb_frames([
            "ChildEBP RetAddr",
            "0019f6d4 0086a1b2     wow_loot64+0x1a2b3",
            "0019f6e0 0086a1c4     KERNELBASE!WaitForMultipleObjectsEx+0x18f",
            "0019f6f0 00000000     0x1234abcd",
            "WARNING: Stack unwind information not available.",
        ], "wow_loot64.exe")
        self.assertEqual([str(f) for f in frames],
                         ["wow.exe+0x1A2B3", "kernelbase!WaitForMultipleObjectsEx+0x18f", "?+0x1234ABCD"])


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.when = dt.datetime(2026, 10, 28, 21, 21, 0, tzinfo=UTC)
        minute = dt.timedelta(minutes=1)

        def trace(boss, start, end):
            name = f"649_3_{boss}_{int(start.timestamp())}.ndjson"
            make_trace(self.tmp.name, name, start, end)

        trace("liandra-suncaller", self.when - 3 * minute, self.when + minute)
        trace("lord-jaraxxus", self.when - 14 * minute, self.when - 10 * minute)
        trace("gormok-the-impaler", self.when - 30 * minute, self.when - 20 * minute)
        self.traces = report.load_traces(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_precise_event_matches_the_running_pull(self):
        event = report.Event(kind="crash", when=self.when, exe="wow.exe", source=Path("x"))
        self.assertEqual([t.boss for t in report.match_traces(event, self.traces)], ["liandra-suncaller"])

    def test_windows_freeze_record_looks_back(self):
        event = report.Event(kind="freeze", when=self.when, exe="wow.exe", source=Path("x"), precise=False)
        self.assertEqual([t.boss for t in report.match_traces(event, self.traces)],
                         ["lord-jaraxxus", "liandra-suncaller"])

    def test_trace_name(self):
        trace = self.traces[-1]
        self.assertEqual((trace.map_id, trace.difficulty, trace.boss), (649, 3, "liandra-suncaller"))
        self.assertEqual(trace.start, self.when - dt.timedelta(minutes=3))


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.client = root / "client"
        shutil.copytree(FIXTURES / "client", self.client)
        self.wer = root / "wer"
        self.wer.mkdir()
        self.traces = root / "traces"
        self.traces.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self):
        return report.collect(self.client, self.wer, self.traces, SUMMER, no_cdb, windows_check)

    def test_windows_record_merges_into_caught_freeze(self):
        folder = make_freeze(self.client / "Errors")
        write_wer(self.wer, "AppHang_wow_loot64.exe_1", "AppHang.Report.wer.txt", app_dir=self.client)
        freezes = [e for e in self.collect() if e.kind == "freeze"]
        self.assertEqual(len(freezes), 1)
        self.assertEqual(freezes[0].source, folder)
        self.assertEqual(freezes[0].wer.name, "AppHang_wow_loot64.exe_1")
        self.assertIn("rtsshooks.dll", freezes[0].third_party)

    def test_unmatched_windows_record_stays(self):
        make_freeze(self.client / "Errors", detected="2026-10-28T19:00:00Z")
        write_wer(self.wer, "AppHang_wow_loot64.exe_1", "AppHang.Report.wer.txt", app_dir=self.client)
        freezes = [e for e in self.collect() if e.kind == "freeze"]
        self.assertEqual(sorted(e.precise for e in freezes), [False, True])

    def test_saved_traces_win_over_live_ones(self):
        folder = make_freeze(self.client / "Errors")
        when = dt.datetime(2026, 10, 28, 21, 21, 0, tzinfo=UTC)
        start = when - dt.timedelta(minutes=2)
        name = f"649_3_liandra-suncaller_{int(start.timestamp())}.ndjson"
        make_trace(folder / "traces", name, start, when + dt.timedelta(minutes=2))
        freeze = next(e for e in self.collect() if e.kind == "freeze")
        self.assertTrue(freeze.traces_saved)
        self.assertEqual([t.boss for t in freeze.traces], ["liandra-suncaller"])

    def test_render(self):
        make_freeze(self.client / "Errors")
        events = self.collect()
        groups = report.assign_groups(events)
        text = report.render(events, groups, SUMMER, self.client, self.traces)
        self.assertIn("5 events: 3 crashes, 1 asserts, 1 freezes, 0 pauses", text)
        self.assertIn("| G1 | crash | 2 |", text)
        self.assertIn("| 2026-07-19 11:49 | 08:49 | crash | wow.exe | wow_optimize.dll+0x3CC35 |", text)
        self.assertIn("wow.exe+0x1A2B3 (stuck)", text)
        detail = report.render_detail("G1", events, SUMMER)
        self.assertIn("# G1: 2 x crash", detail)
        self.assertIn("wow_optimize.dll+0x3CC35", detail)

    def test_pause_groups_apart_from_freeze(self):
        make_freeze(self.client / "Errors")
        make_freeze(self.client / "Errors", detected="2026-10-28T22:00:00Z", kind="pause",
                    outcome="recovered after 7 s")
        events = self.collect()
        groups = report.assign_groups(events)
        caught = sorted((e.kind, e.group) for e in events if e.kind in report.CAUGHT_KINDS)
        self.assertEqual(len({group for _, group in caught}), 2)
        self.assertEqual([kind for kind, _ in caught], ["freeze", "pause"])
        self.assertIn("1 freezes, 1 pauses", report.render(events, groups, SUMMER, self.client, self.traces))

    def test_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.main(["--client", str(self.client), "--wer", str(self.wer), "--traces", str(self.traces)])
        self.assertEqual(code, 0)
        self.assertIn("| G1 | crash | 2 |", out.getvalue())


if __name__ == "__main__":
    unittest.main()
