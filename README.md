# agent-trace

Universal text transcript CLI for agent/harness sessions. Output is text only — no HTML, no TUI.

Useful for inspecting agent runs, debugging what happened, and evaluating results — including handing a transcript to another agent for review.

Implemented in Python using only the standard library.

## Supported harnesses

- Claude Code
- Codex
- Cursor
- Pi

![agent-trace terminal demo](assets/agent-trace-demo.svg)

## Installation

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/ertygiq/agent-trace.git
```

## Usage

```bash
agent-trace --harness <harness> --session <session-id> <filters...>
```

Three things to specify:

- **`--harness`** — which harness produced the session: `claude-code` (aliases: `cc`, `claude`), `codex`, `cursor`, or `pi`.
- **`--session`** — the session id to read.
- **filters** — what to include in the output (at least one required, see below).

```bash
agent-trace --harness claude-code --session abc123 --human --assistant
```

## Filters

Filters select which event categories appear in the transcript. At least one is required; multiple filters combine as a union (OR).

| Filter | Shows |
| --- | --- |
| `--human` | Human-authored messages (alias: `--user`) |
| `--assistant` | Assistant text messages |
| `--thinking` | Assistant thinking blocks |
| `--bash-command` | Bash command text |
| `--bash-output` | Bash command output |
| `--bash-description` | Harness-provided description for a Bash command, when available |
| `--bash` | Shortcut for the three `--bash-*` filters |
| `--all` | All supported categories |

Only shell tool calls are shown. Other tool calls — file reads, edits, writes, searches — are
skipped for every harness; `--all` means all categories in the table above, not every event in
the session file.

Not every harness records every category. Cursor transcripts keep no tool results and fold the
assistant's reasoning into its regular text, so `--bash-output` and `--thinking` stay empty there.

```bash
# human + assistant messages
agent-trace --harness codex --session abc123 --human --assistant

# thinking + bash commands
agent-trace --harness claude-code --session abc123 --thinking --bash-command

# everything bash: description, command, and output
agent-trace --harness claude-code --session abc123 --bash
```

## Harness data roots

Each harness has a default location agent-trace reads from:

- `claude-code`: `~/.claude`
- `codex`: `~/.codex`
- `cursor`: `~/.cursor`
- `pi`: `~/.pi`

Use `--root` to override the default — for example, to read sessions from another machine's exported data. The path may be either the harness config directory or the directory that directly contains session/project folders.

```bash
agent-trace --harness codex --session abc123 --root /path/to/.codex --bash-command
agent-trace --harness claude-code --session abc123 --root /path/to/.claude --human
```
