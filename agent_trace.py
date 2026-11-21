import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from pathlib import Path

DEFAULT_ROOTS = {
    "claude-code": Path.home() / ".claude",
    "codex": Path.home() / ".codex",
    "cursor": Path.home() / ".cursor",
    "pi": Path.home() / ".pi",
}

# Accepted --harness values, normalized to the canonical names above.
HARNESS_ALIASES = {
    "claude-code": "claude-code",
    "cc": "claude-code",
    "claude": "claude-code",
    "codex": "codex",
    "cursor": "cursor",
    "pi": "pi",
}

# Harnesses whose session file is named exactly after the session id.
EXACT_SESSION_FILENAME_HARNESSES = ("claude-code", "cursor")

FILTERS = (
    "human",
    "assistant",
    "thinking",
    "bash_description",
    "bash_command",
    "bash_output",
)

FILTER_FLAGS = {
    "human": "--human",
    "assistant": "--assistant",
    "thinking": "--thinking",
    "bash_description": "--bash-description",
    "bash_command": "--bash-command",
    "bash_output": "--bash-output",
}

# Categories a harness never records, so no filter can ever produce output for them.
UNSUPPORTED_FILTERS = {}

VALUE_OPTIONS = {"--harness", "--session", "--root"}
FLAG_OPTIONS = {
    "--human",
    "--user",
    "--assistant",
    "--thinking",
    "--bash-description",
    "--bash-command",
    "--bash-output",
    "--bash",
    "--all",
    "--json",
    "-h",
    "--help",
}
KNOWN_OPTIONS = VALUE_OPTIONS | FLAG_OPTIONS

USAGE = """Usage: agent-trace --harness HARNESS --session SESSION_ID [--root PATH] FILTER...

Options:
        --harness HARNESS            Harness type. Supported: claude-code (aliases: cc, claude), codex, cursor, pi
        --session SESSION_ID         Session ID
        --root PATH                  Optional. Overrides the default harness data root. May point at the harness config directory or the directory that contains session/project folders.
        --json                       Output a JSON array of {type, timestamp, text} entries instead of text
    -h, --help                       Show help

Filters:
        --human                      Show human-authored messages
        --user                       Alias for --human
        --assistant                  Show assistant text messages
        --thinking                   Show assistant thinking blocks
        --bash-description           Show Bash tool-use descriptions
        --bash-command               Show Bash commands
        --bash-output                Show Bash command outputs
        --bash                       Shortcut for --bash-description --bash-command --bash-output
        --all                        Show all supported categories

Notes:
        Not every harness records every category. Cursor database extraction uses Cursor's
        undocumented local schema and falls back to its less detailed JSONL transcript.
"""


class AgentTraceError(Exception):
    pass


class AgentTrace:
    def __init__(self, argv):
        self.options = {"filters": [], "json": False}
        self.records = []
        self.last_output_date = None
        self.parse(argv)

    def run(self):
        self.validate()
        harness = self.options["harness"]
        renderers = {
            "claude-code": self.render_claude,
            "codex": self.render_codex,
            "cursor": self.render_cursor,
            "pi": self.render_pi,
        }
        renderer = renderers.get(harness)
        if renderer is None:
            raise AgentTraceError(f"unsupported harness: {harness!r}. Available harnesses: {available_harnesses_text()}")
        renderer()

        if self.options["json"]:
            print(json.dumps(self.records, indent=2, ensure_ascii=False))

    def parse(self, argv):
        argv = list(argv)
        self.validate_argv(argv)

        index = 0
        while index < len(argv):
            arg = argv[index]
            option, separator, inline_value = arg.partition("=")

            if option in VALUE_OPTIONS:
                if separator:
                    value = inline_value
                else:
                    index += 1
                    value = argv[index]
                if option == "--harness":
                    self.options["harness"] = value
                elif option == "--session":
                    self.options["session_id"] = value
                elif option == "--root":
                    self.options["root"] = value
            elif option in FLAG_OPTIONS:
                self.apply_flag(option)

            index += 1

    def validate_argv(self, argv):
        index = 0
        while index < len(argv):
            arg = argv[index]

            if arg.startswith("--"):
                option, separator, value = arg.partition("=")
                if option not in KNOWN_OPTIONS:
                    raise AgentTraceError(f"unknown option: {option}")

                if option in VALUE_OPTIONS:
                    if separator:
                        if not present(value):
                            raise AgentTraceError(f"missing value for {option}")
                    else:
                        if index + 1 >= len(argv) or not present(argv[index + 1]) or argv[index + 1].startswith("-"):
                            raise AgentTraceError(f"missing value for {option}")
                        index += 1
                elif separator:
                    raise AgentTraceError(f"{option} does not accept a value")
            elif arg.startswith("-"):
                if arg not in KNOWN_OPTIONS:
                    raise AgentTraceError(f"unknown option: {arg}")
            else:
                raise AgentTraceError(f"unexpected argument: {arg}")

            index += 1

    def apply_flag(self, option):
        if option in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        if option == "--json":
            self.options["json"] = True
        elif option in ("--human", "--user"):
            self.add_filter("human")
        elif option == "--assistant":
            self.add_filter("assistant")
        elif option == "--thinking":
            self.add_filter("thinking")
        elif option == "--bash-description":
            self.add_filter("bash_description")
        elif option == "--bash-command":
            self.add_filter("bash_command")
        elif option == "--bash-output":
            self.add_filter("bash_output")
        elif option == "--bash":
            self.add_filter("bash_description")
            self.add_filter("bash_command")
            self.add_filter("bash_output")
        elif option == "--all":
            self.options["filters"] = list(FILTERS)
        else:
            raise AgentTraceError(f"unknown option: {option}")

    def add_filter(self, filter_name):
        if filter_name not in FILTERS:
            raise AgentTraceError(f"unknown filter: {filter_name!r}")
        if filter_name not in self.options["filters"]:
            self.options["filters"].append(filter_name)

    def validate(self):
        if not present(self.options.get("harness")):
            raise AgentTraceError("missing --harness")
        if not present(self.options.get("session_id")):
            raise AgentTraceError("missing --session")
        canonical = HARNESS_ALIASES.get(self.options["harness"])
        if canonical is None:
            raise AgentTraceError(f"unsupported harness: {self.options['harness']!r}. Available harnesses: {available_harnesses_text()}")
        self.options["harness"] = canonical
        if not self.options["filters"]:
            raise AgentTraceError("at least one filter must be specified. Available filters: " + available_filters_text())

        unsupported = UNSUPPORTED_FILTERS.get(canonical, ())
        if unsupported and all(name in unsupported for name in self.options["filters"]):
            flags = ", ".join(FILTER_FLAGS[name] for name in unsupported)
            raise AgentTraceError(f"{canonical} transcripts do not record: {flags}")

    def render_claude(self):
        session_file = self.find_session_file("claude-code")
        bash_tool_ids = set()

        with session_file.open("r", encoding="utf-8") as lines:
            for line_number, line in enumerate(lines, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AgentTraceError(f"invalid JSON in {session_file}:{line_number}: {error}") from error

                if record.get("sessionId") != self.options["session_id"]:
                    continue
                self.render_claude_record(record, bash_tool_ids)

    def render_claude_record(self, record, bash_tool_ids):
        timestamp = record.get("timestamp")
        message = record.get("message") or {}
        content = message.get("content")

        if record.get("type") == "user":
            self.render_user(record, timestamp, content, bash_tool_ids)
        elif record.get("type") == "assistant":
            self.render_assistant(timestamp, content, bash_tool_ids)

    def render_user(self, record, timestamp, content, bash_tool_ids):
        human_text = self.extract_human_text(record, content)
        if human_text is not None:
            self.emit("human", timestamp, human_text)

        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            if block.get("tool_use_id") not in bash_tool_ids:
                continue
            self.emit("bash_output", timestamp, extract_tool_result_text(block))

    def extract_human_text(self, record, content):
        if record.get("isMeta"):
            return None
        if "toolUseResult" in record or "sourceToolAssistantUUID" in record:
            return None

        if isinstance(content, str):
            return extract_command_message(content) or content

        if not isinstance(content, list):
            return None

        parts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        text = "\n\n".join(part for part in parts if present(part))
        return text if present(text) else None

    def render_assistant(self, timestamp, content, bash_tool_ids):
        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                self.emit("assistant", timestamp, str(block.get("text", "")))
            elif block_type == "thinking":
                self.emit("thinking", timestamp, extract_thinking_text(block))
            elif block_type == "tool_use":
                self.render_tool_use(timestamp, block, bash_tool_ids)

    def render_tool_use(self, timestamp, block, bash_tool_ids):
        if block.get("name") != "Bash":
            return

        tool_id = block.get("id")
        if not present(tool_id):
            raise AgentTraceError("Bash tool_use is missing id")
        bash_tool_ids.add(tool_id)

        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            raise AgentTraceError(f"Bash tool_use {tool_id} has invalid input")
        if "command" not in tool_input:
            raise AgentTraceError(f"Bash tool_use {tool_id} is missing input.command")

        if "description" in tool_input:
            self.emit("bash_description", timestamp, str(tool_input["description"]))
        self.emit("bash_command", timestamp, str(tool_input["command"]))

    def emit(self, filter_name, timestamp, text, allow_empty=False):
        if filter_name not in self.options["filters"]:
            return
        if not allow_empty and not present(text):
            return

        if self.options["json"]:
            # Same field set for every harness: the category, the timestamp as the harness
            # recorded it (null when it records none), and the text.
            self.records.append(
                {
                    "type": filter_name,
                    "timestamp": str(timestamp) if present(timestamp) else None,
                    "text": str(text),
                }
            )
            return

        glyph, label = BLOCK_STYLES.get(filter_name, ("▌", filter_name.replace("_", " ").capitalize()))
        date_key, date_label, clock = timestamp_parts(timestamp)
        if date_key is not None and date_key != self.last_output_date:
            clock = " ".join(part for part in (date_label, clock) if part)
        if date_key is not None:
            self.last_output_date = date_key
        header = f"{glyph} {label}"
        if clock:
            header = f"{header}   {clock}"
        print(header)
        print(text)
        print()

    def render_codex(self):
        root = Path(self.options.get("root") or DEFAULT_ROOTS["codex"]).expanduser()
        session_file = find_codex_session(root, self.options["session_id"])
        bash_call_ids = set()
        for entry in codex_history(root, session_file):
            self.render_codex_entry(entry, bash_call_ids)

    def render_codex_entry(self, entry, bash_call_ids):
        timestamp = entry.get("timestamp")

        if entry.get("type") == "event_msg":
            payload = entry.get("payload") or {}
            if payload.get("type") == "user_message":
                self.emit("human", timestamp, str(payload.get("message", "")))
            elif payload.get("type") == "item_completed":
                # Paginated history uses completed TurnItems instead of legacy
                # user_message events. Raw user-role response items can also
                # contain injected context, so they are not human messages.
                item = payload.get("item") or {}
                if item.get("type") == "UserMessage":
                    self.emit("human", timestamp, extract_codex_message_text(item.get("content")))
                elif item.get("type") == "CommandExecution":
                    command = item.get("command")
                    if isinstance(command, list):
                        command = shlex.join(command)
                    if isinstance(command, str):
                        self.emit("bash_command", timestamp, command)
                    output = item.get("aggregated_output")
                    if output is not None:
                        self.emit("bash_output", timestamp, output, allow_empty=True)
            return

        if entry.get("type") == "response_item":
            payload = entry.get("payload") or {}
            payload_type = payload.get("type")

            if payload_type == "message":
                if payload.get("role") == "assistant":
                    self.emit("assistant", timestamp, extract_codex_message_text(payload.get("content")))
            elif payload_type == "reasoning":
                self.emit("thinking", timestamp, extract_codex_reasoning_text(payload))
            elif payload_type == "function_call":
                self.render_codex_function_call(timestamp, payload, bash_call_ids)
            elif payload_type == "function_call_output" and payload.get("call_id") in bash_call_ids:
                raw_output = payload.get("output")
                self.emit("bash_output", timestamp, extract_codex_bash_output(raw_output), allow_empty=is_codex_terminal_bash_output(raw_output))

    def render_codex_function_call(self, timestamp, payload, bash_call_ids):
        call_name = payload.get("name")
        if call_name not in ("exec_command", "write_stdin"):
            return

        call_id = payload.get("call_id")
        if not present(call_id):
            raise AgentTraceError(f"Codex {call_name} function_call is missing call_id")
        bash_call_ids.add(call_id)

        if call_name != "exec_command":
            return

        arguments = parse_codex_arguments(payload.get("arguments"), call_id)
        if "cmd" not in arguments:
            raise AgentTraceError(f"Codex exec_command {call_id} is missing arguments.cmd")
        self.emit("bash_command", timestamp, str(arguments["cmd"]))

    def render_cursor(self):
        # The JSONL transcript is an intentionally lossy export: it has no assistant
        # timestamps and may merge thinking with answer text. Cursor's UI state database
        # retains separate bubbles, so prefer it when available and retain JSONL as a
        # portable fallback for exports and future unknown database layouts.
        database = find_cursor_state_database(self.options.get("root"))
        if database is not None:
            try:
                entries = load_cursor_database_entries(database, self.options["session_id"])
            except (OSError, sqlite3.Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                entries = None
            if entries is not None:
                for entry in entries:
                    self.render_cursor_database_entry(entry)
                return

        self.render_cursor_jsonl()

    def render_cursor_jsonl(self):
        session_file = self.find_session_file("cursor")

        with session_file.open("r", encoding="utf-8") as lines:
            for line_number, line in enumerate(lines, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AgentTraceError(f"invalid JSON in {session_file}:{line_number}: {error}") from error

                self.render_cursor_entry(entry)

    def render_cursor_database_entry(self, entry):
        timestamp = entry.get("createdAt")
        role = entry.get("type")

        if role == 1:
            self.emit("human", timestamp, str(entry.get("text", "")))
            return
        if role != 2:
            return

        thinking = entry.get("thinking")
        if isinstance(thinking, dict):
            self.emit("thinking", timestamp, extract_thinking_text(thinking))
        elif present(thinking):
            self.emit("thinking", timestamp, str(thinking))

        self.emit("assistant", timestamp, str(entry.get("text", "")))
        self.render_cursor_database_tool(entry, timestamp)

    def render_cursor_database_tool(self, entry, timestamp):
        tool = entry.get("toolFormerData")
        if not isinstance(tool, dict) or not is_cursor_shell_tool(tool):
            return

        parameters = parse_json_object(tool.get("params")) or parse_json_object(tool.get("rawArgs")) or {}
        command = parameters.get("command")
        if not present(command):
            return

        description = parameters.get("commandDescription") or parameters.get("description")
        if present(description):
            self.emit("bash_description", timestamp, str(description))
        self.emit("bash_command", timestamp, str(command))

        output = extract_cursor_tool_output(tool.get("result"))
        if output is not None:
            self.emit("bash_output", timestamp, output, allow_empty=True)

    def render_cursor_entry(self, entry):
        # Turn markers such as {"type": "turn_ended", ...} carry no role and no content.
        role = entry.get("role")
        if role not in ("user", "assistant"):
            return

        message = entry.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text", ""))
                if role == "user":
                    # The fallback JSONL has no record-level timestamps; user turns carry
                    # a human-readable one inline.
                    self.emit("human", extract_tag(text, "timestamp"), extract_cursor_user_text(text))
                else:
                    self.emit("assistant", None, text)
            elif block_type == "tool_use" and role == "assistant":
                self.render_cursor_tool_use(block)

    def render_cursor_tool_use(self, block):
        if block.get("name") != "Shell":
            return

        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            raise AgentTraceError("Cursor Shell tool_use has invalid input")
        if "command" not in tool_input:
            raise AgentTraceError("Cursor Shell tool_use is missing input.command")

        if "description" in tool_input:
            self.emit("bash_description", None, str(tool_input["description"]))
        self.emit("bash_command", None, str(tool_input["command"]))

    def render_pi(self):
        session_file = self.find_session_file("pi")
        bash_tool_ids = set()

        with session_file.open("r", encoding="utf-8") as lines:
            for line_number, line in enumerate(lines, start=1):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AgentTraceError(f"invalid JSON in {session_file}:{line_number}: {error}") from error

                self.render_pi_entry(entry, bash_tool_ids)

    def render_pi_entry(self, entry, bash_tool_ids):
        if entry.get("type") == "session":
            if entry.get("id") != self.options["session_id"]:
                raise AgentTraceError(f"session file id mismatch: expected {self.options['session_id']}, got {entry.get('id')}")
            return
        if entry.get("type") != "message":
            return

        timestamp = entry.get("timestamp")
        message = entry.get("message") or {}
        role = message.get("role")
        content = message.get("content")

        if role == "user":
            self.emit("human", timestamp, extract_text_content(content))
        elif role == "assistant":
            self.render_pi_assistant(timestamp, content, bash_tool_ids)
        elif role == "toolResult":
            if message.get("toolName") == "bash" or message.get("toolCallId") in bash_tool_ids:
                self.emit("bash_output", timestamp, extract_text_content(content))
        elif role == "bashExecution":
            self.emit("bash_command", timestamp, str(message.get("command", "")))
            self.emit("bash_output", timestamp, str(message.get("output", "")))

    def render_pi_assistant(self, timestamp, content, bash_tool_ids):
        if not isinstance(content, list):
            return

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                self.emit("assistant", timestamp, str(block.get("text", "")))
            elif block_type == "thinking":
                self.emit("thinking", timestamp, extract_thinking_text(block))
            elif block_type == "toolCall" and block.get("name") == "bash":
                tool_id = block.get("id")
                if not present(tool_id):
                    raise AgentTraceError("Pi bash toolCall is missing id")
                bash_tool_ids.add(tool_id)
                arguments = block.get("arguments")
                if not isinstance(arguments, dict):
                    raise AgentTraceError(f"Pi bash toolCall {tool_id} has invalid arguments")
                if "command" not in arguments:
                    raise AgentTraceError(f"Pi bash toolCall {tool_id} is missing arguments.command")
                if "description" in arguments:
                    self.emit("bash_description", timestamp, str(arguments["description"]))
                self.emit("bash_command", timestamp, str(arguments["command"]))

    def find_session_file(self, harness):
        root = Path(self.options.get("root") or DEFAULT_ROOTS[harness]).expanduser()
        if not root.exists():
            raise AgentTraceError(f"root does not exist: {root}")
        if not root.is_dir():
            raise AgentTraceError(f"root is not a directory: {root}")

        if harness in EXACT_SESSION_FILENAME_HARNESSES:
            pattern = f"{self.options['session_id']}.jsonl"
        else:
            pattern = f"*{self.options['session_id']}*.jsonl"
        search_root = harness_sessions_root(harness, root)
        matches = list(search_root.rglob(pattern))
        if not matches:
            raise AgentTraceError(f"session not found under {search_root}: {self.options['session_id']}")
        if len(matches) > 1:
            raise AgentTraceError("multiple session files found: " + ", ".join(str(match) for match in matches))
        return matches[0]


def find_cursor_state_database(root_override):
    if present(root_override):
        root = Path(root_override).expanduser()
        candidates = (
            root / "state.vscdb",
            root / "globalStorage" / "state.vscdb",
            root / "User" / "globalStorage" / "state.vscdb",
            root / "Cursor" / "User" / "globalStorage" / "state.vscdb",
        )
    elif sys.platform == "darwin":
        candidates = (Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb",)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        candidates = (Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb",) if appdata else ()
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        candidates = (config_home / "Cursor" / "User" / "globalStorage" / "state.vscdb",)

    return next((candidate for candidate in candidates if candidate.is_file()), None)


def load_cursor_database_entries(database, session_id):
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        composer_row = connection.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{session_id}",),
        ).fetchone()
        bubble_rows = connection.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{session_id}:%",),
        ).fetchall()
    finally:
        connection.close()

    bubbles = {}
    for row in bubble_rows:
        bubble = decode_cursor_json(row[0])
        if not isinstance(bubble, dict) or not present(bubble.get("bubbleId")):
            continue
        bubbles[str(bubble["bubbleId"])] = bubble
    if not bubbles or not any(bubble.get("type") in (1, 2) for bubble in bubbles.values()):
        return None

    composer = decode_cursor_json(composer_row[0]) if composer_row else None
    headers = composer.get("fullConversationHeadersOnly") if isinstance(composer, dict) else None
    if isinstance(headers, list) and headers:
        ordered = []
        seen = set()
        for header in headers:
            if not isinstance(header, dict):
                continue
            bubble_id = str(header.get("bubbleId", ""))
            bubble = bubbles.get(bubble_id)
            if bubble is None or bubble_id in seen:
                continue
            seen.add(bubble_id)
            if not present(bubble.get("createdAt")) and present(header.get("createdAt")):
                bubble = dict(bubble)
                bubble["createdAt"] = header["createdAt"]
            ordered.append(bubble)
        if ordered:
            return ordered

    # Old, partially migrated, and damaged databases do not always retain the
    # header index. ISO createdAt values provide a useful deterministic fallback.
    return sorted(bubbles.values(), key=lambda bubble: str(bubble.get("createdAt", "")))


def decode_cursor_json(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def parse_json_object(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_cursor_shell_tool(tool):
    name = str(tool.get("name", "")).lower()
    return tool.get("tool") == 15 or name == "shell" or "terminal_command" in name


def extract_cursor_tool_output(result):
    if result is None:
        return None
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return result
        if isinstance(parsed, str):
            return parsed
        return extract_cursor_tool_output(parsed)
    if not isinstance(result, dict):
        return None

    for key in ("output", "terminalOutput", "terminal_output"):
        if key in result and result[key] is not None:
            return extract_text_content(result[key])

    streams = []
    for key in ("stdout", "stderr"):
        if key in result and result[key] is not None:
            streams.append(extract_text_content(result[key]))
    if streams:
        return "\n".join(streams)

    nested = result.get("result")
    return extract_cursor_tool_output(nested) if nested is not result else None


@contextmanager
def open_codex_rollout(path, bounded=False):
    """Read plain or zstd-compressed history without modifying Codex files."""
    if path.suffix != ".zst":
        with path.open("r", encoding="utf-8") as stream:
            yield stream
        return
    try:
        process = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8")
    except FileNotFoundError as error:
        raise AgentTraceError("Reading compressed Codex sessions requires zstd on PATH") from error
    try:
        yield process.stdout
        # Metadata and bounded ancestors deliberately stop before EOF.
        if bounded and process.poll() is None:
            process.terminate()
    except BaseException:
        if process.poll() is None:
            process.terminate()
        raise
    finally:
        process.stdout.close()
        error = process.stderr.read()
        process.stderr.close()
        status = process.wait()
    if status not in (0, -15, -13):
        raise AgentTraceError(f"Cannot decompress {path}: {error.strip()}")


def codex_metadata(path):
    with open_codex_rollout(path, bounded=True) as stream:
        try:
            record = json.loads(next(stream))
        except (StopIteration, json.JSONDecodeError) as error:
            raise AgentTraceError(f"Invalid Codex session metadata: {path}") from error
    if record.get("type") != "session_meta":
        raise AgentTraceError(f"Codex session file is missing session_meta: {path}")
    return record.get("payload") or {}


def existing_codex_rollout(path):
    if path.is_file():
        return path
    compressed = Path(str(path) + ".zst")
    return compressed if compressed.is_file() else None


def codex_rollout_paths(root):
    # Include archived sessions, but not unrelated files elsewhere in CODEX_HOME.
    dirs = [root / name for name in ("sessions", "archived_sessions")]
    dirs = [directory for directory in dirs if directory.is_dir()] or [root]
    paths = []
    for directory in dirs:
        paths.extend(directory.rglob("*.jsonl"))
        paths.extend(path for path in directory.rglob("*.jsonl.zst")
                     if not Path(str(path)[:-4]).is_file())
    return paths


def codex_rollout_id(path):
    # Continuations have a new rollout UUID after the thread UUID. Bookmarks
    # point to rollout IDs, not necessarily the conversation's thread ID.
    match = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl(?:\.zst)?$", path.name)
    return match.group(1) if match else None


def find_codex_session(root, session_id):
    databases = sorted(root.glob("state_*.sqlite"),
                       key=lambda path: int(path.stem.split("_")[-1]))
    if databases:
        try:
            with closing(sqlite3.connect(databases[-1].resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute("SELECT rollout_path FROM threads WHERE id = ?", (session_id,)).fetchone()
            if row:
                path = existing_codex_rollout(Path(row[0]))
                if path is None:
                    raise AgentTraceError(f"Codex session's indexed rollout is missing: {row[0]}")
                if codex_metadata(path).get("id") != session_id:
                    raise AgentTraceError(f"Codex session file id mismatch: {path}")
                return path
        except sqlite3.Error as error:
            raise AgentTraceError(f"Cannot read Codex session index: {error}") from error
    matches = [path for path in codex_rollout_paths(root) if session_id in path.name
               and codex_metadata(path).get("id") == session_id]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise AgentTraceError(f"Codex session not found under {root}: {session_id}")
    raise AgentTraceError("Multiple Codex rollouts found; pass --root pointing to CODEX_HOME with its state database")


def codex_history(root, selected):
    """Follow Codex history_base bookmarks, replaying only inherited prefixes.

    Matches codex-rs/thread-store/src/local/rollout_lineage: an ancestor ends
    before end_ordinal_exclusive (and at end_byte_offset); the child starts
    after that ordinal. SQLite is an index/projection, not a second transcript
    to concatenate. No message-text deduplication is involved.
    """
    paths = None
    segments = []
    seen = set()
    path = selected
    end = None
    while True:
        if path in seen:
            raise AgentTraceError(f"Cycle in Codex history: {path}")
        seen.add(path)
        meta = codex_metadata(path)
        if meta.get("attached_thread"):
            raise AgentTraceError("Attached Codex thread history is not supported yet")
        base = meta.get("history_base")
        start = base["end_ordinal_exclusive"] + 1 if base else None
        segments.append((path, start, end))
        if not base:
            break
        if paths is None:
            paths = codex_rollout_paths(root)
        ancestors = [candidate for candidate in paths
                     if codex_rollout_id(candidate) == base["thread_id"]]
        if len(ancestors) != 1:
            raise AgentTraceError(f"Expected one Codex ancestor {base['thread_id']}, found {len(ancestors)}")
        path, end = ancestors[0], base

    for path, start, end in reversed(segments):
        with open_codex_rollout(path, bounded=end is not None) as stream:
            byte_offset = 0
            for line_number, line in enumerate(stream, 1):
                if end and byte_offset >= end["end_byte_offset"]:
                    break
                byte_offset += len(line.encode("utf-8"))
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AgentTraceError(f"Invalid JSON in {path}:{line_number}: {error}") from error
                ordinal = entry.get("ordinal")
                if end and ordinal is not None and ordinal >= end["end_ordinal_exclusive"]:
                    break
                if entry.get("type") == "session_meta":
                    continue
                if (start is not None or end is not None) and ordinal is None:
                    raise AgentTraceError(f"Missing ordinal in linked Codex history: {path}:{line_number}")
                if start is not None and ordinal < start:
                    continue
                yield entry
            if end and byte_offset < end["end_byte_offset"]:
                raise AgentTraceError(f"Truncated Codex ancestor: {path}")


def parse_codex_arguments(value, call_id):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise AgentTraceError(f"Codex function_call {call_id} has invalid JSON arguments: {error}") from error
        if isinstance(parsed, dict):
            return parsed
    raise AgentTraceError(f"Codex function_call {call_id} has invalid arguments")


def extract_codex_message_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if isinstance(block, dict):
            value = block.get("text") or block.get("input_text") or block.get("output_text")
        else:
            value = block
        if value is not None:
            parts.append(str(value))
    return "\n".join(parts)


def extract_codex_bash_output(output):
    if output is None:
        return ""
    text = str(output)
    marker = "\nOutput:\n"
    if marker in text:
        return text.split(marker, 1)[1]
    if text.startswith("Output:\n"):
        return text[len("Output:\n"):]
    return text


def is_codex_terminal_bash_output(output):
    if output is None:
        return False
    text = str(output)
    return text.startswith("Exit code:") or "\nProcess exited with code " in text


def extract_codex_reasoning_text(payload):
    summary = payload.get("summary")
    if isinstance(summary, list):
        text = extract_codex_message_text(summary)
        if present(text):
            return text
    elif present(summary):
        return str(summary)
    if present(payload.get("text")):
        return str(payload["text"])
    if present(payload.get("encrypted_content")):
        return "[reasoning block present but plaintext reasoning is not available]"
    return "[reasoning block present but no plaintext reasoning field]"


def extract_cursor_user_text(text):
    # Cursor wraps the typed message in <user_query> and prepends harness-injected context
    # (timestamp, attached files) as sibling tags.
    query = extract_tag(text, "user_query")
    return query.strip() if query is not None else text


def extract_command_message(text):
    command_name = extract_tag(text, "command-name")
    command_args = extract_tag(text, "command-args")
    parts = [part for part in (command_name, command_args) if part is not None]
    return "\n".join(parts) if parts else None


def extract_tag(text, tag):
    match = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text, re.DOTALL)
    return match.group(1) if match else None


def extract_tool_result_text(block):
    return extract_text_content(block.get("content"))


def extract_text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for entry in content:
            if isinstance(entry, dict):
                value = entry.get("text")
            else:
                value = entry
            if value is not None:
                parts.append(str(value))
        return "\n".join(parts)
    return str(content)


def harness_sessions_root(harness, root):
    if harness == "claude-code":
        candidate = root / "projects"
        if candidate.is_dir():
            return candidate
    elif harness == "codex":
        candidate = root / "sessions"
        if candidate.is_dir():
            return candidate
    elif harness == "cursor":
        candidate = root / "projects"
        if candidate.is_dir():
            return candidate
    elif harness == "pi":
        candidate = root / "agent" / "sessions"
        if candidate.is_dir():
            return candidate
    return root


def extract_thinking_text(block):
    text = block.get("thinking") or block.get("text")
    if present(text):
        return text
    return "[thinking block present but no plaintext thinking field]"


def available_harnesses_text():
    return ", ".join(sorted(DEFAULT_ROOTS))


def available_filters_text():
    return ", ".join(tuple(FILTER_FLAGS[name] for name in FILTERS) + ("--bash", "--all"))


BLOCK_STYLES = {
    "human": ("▌", "Human"),
    "assistant": ("▌", "Assistant"),
    "thinking": ("▌", "Thinking"),
    "bash_description": ("▌", "Bash note"),
    "bash_command": ("▌", "Bash"),
    "bash_output": ("▌", "Output"),
}

CLOCK_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2})")
ISO_DATE_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
HUMAN_DATE_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2}),\s+(\d{4}).*?(\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)",
    re.IGNORECASE,
)
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MONTH_LABELS = {name.lower(): name[:3] for name in MONTH_NAMES}


def timestamp_parts(timestamp):
    if not present(timestamp):
        return None, "", ""
    text = str(timestamp)

    iso_date = ISO_DATE_PATTERN.search(text)
    if iso_date:
        year, month, day = iso_date.groups()
        clock = CLOCK_PATTERN.search(text)
        date_label = f"{int(day)} {MONTH_NAMES[int(month) - 1][:3]}"
        return f"{year}-{month}-{day}", date_label, clock.group(1) if clock else text

    human_date = HUMAN_DATE_PATTERN.search(text)
    if human_date:
        month, day, year, clock = human_date.groups()
        month_number = tuple(name.lower() for name in MONTH_NAMES).index(month.lower()) + 1
        return f"{year}-{month_number:02d}-{int(day):02d}", f"{int(day)} {MONTH_LABELS[month.lower()]}", clock

    clock = CLOCK_PATTERN.search(text)
    return None, "", clock.group(1) if clock else text


def format_clock(timestamp):
    return timestamp_parts(timestamp)[2]


def present(value):
    return value is not None and value != ""


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        AgentTrace(argv).run()
        # Flush here so a closed downstream pipe (`| head`) surfaces inside this handler
        # rather than during interpreter shutdown, where nothing can catch it.
        sys.stdout.flush()
    except BrokenPipeError:
        # Point stdout at /dev/null so the shutdown flush of any bytes still buffered has
        # somewhere harmless to go instead of printing to stderr.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except AgentTraceError as error:
        print(f"agent-trace: {error}", file=sys.stderr)
        print("Run 'agent-trace --help' for usage.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
