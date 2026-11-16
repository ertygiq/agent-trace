import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_trace import AgentTrace


def event(payload):
    return {"type": "event_msg", "timestamp": "2026-01-01T12:00:00Z", "payload": payload}


def user_item(text):
    return event({
        "type": "item_completed",
        "item": {"type": "UserMessage", "id": "message-1",
                 "content": [{"type": "text", "text": text, "text_elements": []}]},
    })


def response(role, text):
    return {"type": "response_item", "timestamp": "2026-01-01T12:00:00Z",
            "payload": {"type": "message", "role": role,
                        "content": [{"type": "input_text" if role == "user" else "output_text",
                                     "text": text}]}}


class CodexTranscriptTests(unittest.TestCase):
    def render(self, records, *filters):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sessions" / "rollout-test-session.jsonl"
            path.parent.mkdir()
            metadata = {"type": "session_meta", "payload": {"id": "test-session"}}
            path.write_text("".join(json.dumps(record) + "\n" for record in [metadata, *records]),
                            encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                AgentTrace(["--harness", "codex", "--session", "test-session",
                            "--root", root, "--json", *filters]).run()
            return json.loads(output.getvalue())

    def test_legacy_user_message(self):
        result = self.render([event({"type": "user_message", "message": "Hello"})], "--human")
        self.assertEqual(result, [{"type": "human", "timestamp": "2026-01-01T12:00:00Z",
                                   "text": "Hello"}])

    def test_completed_user_message_with_multiple_parts(self):
        record = user_item("Hello")
        record["payload"]["item"]["content"].extend([
            {"type": "image", "image_url": "https://example.invalid/image.png"},
            {"type": "text", "text": "World", "text_elements": []},
        ])
        result = self.render([record], "--human")
        self.assertEqual(result[0]["text"], "Hello\nWorld")
        self.assertEqual(result[0]["type"], "human")
        self.assertEqual(result[0]["timestamp"], record["timestamp"])

    def test_raw_user_context_and_other_completed_items_are_not_human(self):
        result = self.render([
            response("user", "Injected workspace context"),
            response("user", "Hello"),
            user_item("Hello"),
            event({"type": "item_completed", "item": {"type": "AgentMessage"}}),
            event({"type": "item_completed", "item": {"type": "CommandExecution"}}),
        ], "--human")
        self.assertEqual([entry["text"] for entry in result], ["Hello"])

    def test_repeated_text_is_not_deduplicated(self):
        result = self.render([user_item("Again"), user_item("Again")], "--human")
        self.assertEqual([entry["text"] for entry in result], ["Again", "Again"])

    def test_both_event_types_are_recognized(self):
        result = self.render([
            event({"type": "user_message", "message": "First"}), user_item("Second"),
        ], "--human")
        self.assertEqual([entry["text"] for entry in result], ["First", "Second"])

    def test_compaction_keeps_prior_messages_without_replaying_replacement(self):
        result = self.render([
            user_item("Before compaction"), response("assistant", "First answer"),
            {"type": "compacted", "payload": {"replacement_history": [
                response("user", "Compaction context")["payload"]]}},
            user_item("After compaction"), response("assistant", "Second answer"),
        ], "--human", "--assistant")
        self.assertEqual([entry["text"] for entry in result],
                         ["Before compaction", "First answer", "After compaction", "Second answer"])

    def command(self, output="hello\n", status="completed"):
        return event({"type": "item_completed", "item": {
            "type": "CommandExecution", "id": "exec-1",
            "command": ["/bin/sh", "-lc", "printf 'hello\\n'"],
            "aggregated_output": output, "status": status,
        }})

    def test_completed_command_and_output(self):
        result = self.render([self.command()], "--bash")
        self.assertEqual([entry["type"] for entry in result], ["bash_command", "bash_output"])
        # Shell quoting preserves argv boundaries, including embedded quotes.
        import shlex
        self.assertEqual(shlex.split(result[0]["text"]), ["/bin/sh", "-lc", "printf 'hello\\n'"])
        self.assertEqual(result[1]["text"], "hello\n")
        self.assertEqual(result[1]["timestamp"], "2026-01-01T12:00:00Z")

    def test_completed_command_filters(self):
        for flag, expected in [("--bash-command", ["bash_command"]),
                               ("--bash-output", ["bash_output"]),
                               ("--bash-description", []), ("--human", [])]:
            with self.subTest(flag=flag):
                result = self.render([self.command()], flag)
                self.assertEqual([entry["type"] for entry in result], expected)

    def test_failed_command_output_is_preserved(self):
        result = self.render([self.command("permission denied\n", "failed")], "--bash-output")
        self.assertEqual(result[0]["text"], "permission denied\n")

    def test_empty_output_is_preserved_but_absent_output_is_not_invented(self):
        self.assertEqual(self.render([self.command("")], "--bash-output")[0]["text"], "")
        self.assertEqual(self.render([self.command(None)], "--bash-output"), [])

    def test_raw_exec_wrapper_is_not_exported_as_an_extra_shell_command(self):
        wrapper = {"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "wrapper-1",
            "input": "text(await tools.exec_command({cmd: 'echo hello'}));"}}
        result = self.render([wrapper, self.command()], "--bash")
        self.assertEqual([entry["type"] for entry in result], ["bash_command", "bash_output"])

    def test_legacy_shell_calls_still_work(self):
        records = [
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
             "call_id": "call-1", "arguments": json.dumps({"cmd": "echo hello"})}},
            {"type": "response_item", "payload": {"type": "function_call_output",
             "call_id": "call-1", "output": "hello"}},
        ]
        result = self.render(records, "--bash")
        self.assertEqual([entry["text"] for entry in result], ["echo hello", "hello"])

    def test_assistant_filter_excludes_completed_user_messages(self):
        result = self.render([user_item("Question"), response("assistant", "Answer")], "--assistant")
        self.assertEqual([entry["text"] for entry in result], ["Answer"])


if __name__ == "__main__":
    unittest.main()
