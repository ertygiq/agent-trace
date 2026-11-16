import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from agent_trace import AgentTrace, AgentTraceError, codex_history, find_codex_session


THREAD = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
NEXT = "33333333-3333-3333-3333-333333333333"


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def meta(self, base=None, ordinal=0):
        payload = {"id": THREAD, "history_mode": "paginated"}
        if base:
            payload["history_base"] = base
        return {"type": "session_meta", "ordinal": ordinal, "payload": payload}

    def human(self, text, ordinal):
        return {"type": "event_msg", "ordinal": ordinal, "timestamp": "2026-01-01T12:00:00Z",
                "payload": {"type": "item_completed", "item": {
                    "type": "UserMessage", "content": [{"type": "text", "text": text}]}}}

    def write_rollout(self, rollout_id, records, archived=False, compressed=False):
        directory = self.root / ("archived_sessions" if archived else "sessions")
        directory.mkdir(exist_ok=True)
        name = THREAD if rollout_id == THREAD else THREAD + "_" + rollout_id
        path = directory / ("rollout-2026-01-01T12-00-00-" + name + ".jsonl")
        # Non-ASCII exercises byte offsets rather than character counts.
        data = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()
        path.write_bytes(data)
        if compressed:
            if not shutil.which("zstd"):
                self.skipTest("zstd is not installed")
            subprocess.run(["zstd", "-q", str(path)], check=True)
            path.unlink()
            return Path(str(path) + ".zst"), len(data)
        return path, len(data)

    def bookmark(self, rollout_id, end, offset):
        return {"thread_id": rollout_id, "end_ordinal_exclusive": end, "end_byte_offset": offset}

    def index(self, path):
        with contextlib.closing(sqlite3.connect(self.root / "state_5.sqlite")) as connection:
            connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, ?)", (THREAD, str(path)))
            connection.commit()

    def render(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            AgentTrace(["--harness", "codex", "--session", THREAD, "--root", str(self.root),
                        "--human", "--json"]).run()
        return [entry["text"] for entry in json.loads(out.getvalue())]

    def test_compressed_ancestor_and_plain_continuation_with_abandoned_tail(self):
        prefix = [self.meta(), self.human("Привіт", 1)]
        offset = len("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in prefix).encode())
        self.write_rollout(THREAD, prefix + [self.human("Abandoned", 2)], compressed=True, archived=True)
        child, _ = self.write_rollout(CHILD, [self.meta(self.bookmark(THREAD, 2, offset), 2),
                                             self.human("Привіт", 3)])
        self.index(child)
        self.assertEqual(self.render(), ["Привіт", "Привіт"])

    def test_multiple_ancestor_levels(self):
        _, size = self.write_rollout(THREAD, [self.meta(), self.human("First", 1)])
        _, size2 = self.write_rollout(CHILD, [self.meta(self.bookmark(THREAD, 2, size), 2),
                                             self.human("Second", 3)])
        leaf, _ = self.write_rollout(NEXT, [self.meta(self.bookmark(CHILD, 4, size2), 4),
                                           self.human("Third", 5)])
        self.index(leaf)
        self.assertEqual(self.render(), ["First", "Second", "Third"])

    def test_compressed_current_rollout_from_index(self):
        path, _ = self.write_rollout(THREAD, [self.meta(), self.human("Hello", 1)], compressed=True)
        self.index(Path(str(path)[:-4]))  # Codex keeps the original .jsonl index path.
        self.assertEqual(self.render(), ["Hello"])

    def test_standalone_compressed_archived_session(self):
        self.write_rollout(THREAD, [self.meta(), self.human("Archived", 1)], compressed=True, archived=True)
        self.assertEqual(self.render(), ["Archived"])

    def test_index_reads_uncheckpointed_wal(self):
        connection = sqlite3.connect(self.root / "state_5.sqlite")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
        path, _ = self.write_rollout(THREAD, [self.meta(), self.human("Live", 1)])
        connection.execute("INSERT INTO threads VALUES (?, ?)", (THREAD, str(path)))
        connection.commit()
        self.assertEqual(self.render(), ["Live"])

    def test_missing_ancestor_is_error_not_partial_transcript(self):
        path, _ = self.write_rollout(CHILD, [self.meta(self.bookmark(THREAD, 2, 500), 2),
                                            self.human("Tail", 3)])
        with self.assertRaisesRegex(AgentTraceError, "ancestor"):
            list(codex_history(self.root, path))

    def test_truncated_ancestor_is_error(self):
        self.write_rollout(THREAD, [self.meta(), self.human("First", 1)])
        path, _ = self.write_rollout(CHILD, [self.meta(self.bookmark(THREAD, 2, 999999), 2)])
        with self.assertRaisesRegex(AgentTraceError, "Truncated"):
            list(codex_history(self.root, path))

    def test_missing_indexed_file_is_error(self):
        self.index(self.root / "missing.jsonl")
        with self.assertRaisesRegex(AgentTraceError, "missing"):
            find_codex_session(self.root, THREAD)

    def test_cycle_is_error(self):
        path, _ = self.write_rollout(THREAD, [self.meta(self.bookmark(THREAD, 2, 500), 2)])
        with self.assertRaisesRegex(AgentTraceError, "Cycle"):
            list(codex_history(self.root, path))

    def test_invalid_compressed_file_is_error(self):
        if not shutil.which("zstd"):
            self.skipTest("zstd is not installed")
        directory = self.root / "sessions"
        directory.mkdir()
        (directory / ("rollout-" + THREAD + ".jsonl.zst")).write_bytes(b"not zstd")
        with self.assertRaises(AgentTraceError):
            self.render()


if __name__ == "__main__":
    unittest.main()
