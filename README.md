# agent-trace

Universal text transcript CLI for agent/harness sessions. Output is text only. No HTML/TUI.

Useful for inspecting agent runs, debugging what happened, and evaluating agent results — including handing filtered transcripts to another agent for review.

Implemented in Python using only the standard library.

## Supported harnesses

- Claude Code
- Pi

More harness adapters can be added later.

## CLI shape

```bash
agent-trace --harness claude --session <session-id> <filter>...
agent-trace --harness pi --session <session-id> <filter>...
agent-trace --harness claude --session <session-id> --root /path/to/claude/projects <filter>...
agent-trace --harness pi --session <session-id> --root /path/to/.pi <filter>...
```

## Harness defaults

- `claude`: `~/.claude/projects`
- `pi`: `~/.pi`

`--root` overrides the default harness data root. For Claude Code, use the projects directory that contains encoded project folders and session `.jsonl` files. For Pi, use the `.pi` directory.

## Filter model

Filters select event categories. At least one filter is required. Multiple filters are combined as union/OR.

Examples:

```bash
# human + assistant messages
agent-trace --harness claude --session <id> --human --assistant

# thinking + Bash commands
agent-trace --harness claude --session <id> --thinking --bash-command

# Bash command, description, and output
agent-trace --harness claude --session <id> --bash
```

Filters:

- `--human`: human-authored messages only
- `--user`: alias for `--human`
- `--assistant`: assistant text messages only
- `--thinking`: assistant thinking blocks
- `--bash-description`: harness-provided description for a Bash command, when available
- `--bash-command`: Bash command text
- `--bash-output`: Bash command output/result
- `--bash`: shortcut for `--bash-description --bash-command --bash-output`
- `--all`: all supported categories

Generic `tool` filtering is intentionally skipped for now.

## Example with custom root

```bash
agent-trace \
  --harness pi \
  --session <session-id> \
  --root /path/to/.pi \
  --bash-command
```
