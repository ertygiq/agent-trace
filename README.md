# agent-trace

Universal text transcript CLI for agent/harness sessions.

## Initial scope

Start with Claude Code. Add more harness adapters later, with Pi planned next.

## CLI shape

```bash
agent-trace --harness claude --session <session-id> [filters]
agent-trace --harness claude --session <session-id> --root /path/to/claude/projects [filters]
```

## Harness defaults

- `claude`: `~/.claude/projects`
- future `pi`: TBD

`--root` overrides the default transcript root for the selected harness. For Claude Code, this means the directory containing encoded project folders and session `.jsonl` files.

## Output

Text only. No HTML/TUI.

## Filter model

Filters select event categories. Multiple filters are combined as union/OR. If no filter is passed, all currently supported categories are shown.

Examples:

```bash
# human + assistant messages
agent-trace --harness claude --session <id> --human --assistant

# thinking + Bash commands
agent-trace --harness claude --session <id> --thinking --bash-command

# Bash command, description, and output
agent-trace --harness claude --session <id> --bash
```

Draft categories:

- `human`: human-authored messages only (`--user` is an alias)
- `assistant`: assistant text messages only
- `thinking`: assistant thinking blocks
- `bash-description`: Claude Code's description for a Bash command
- `bash-command`: Bash command text
- `bash-output`: Bash command output/result

`--bash` is a shortcut for `--bash-description --bash-command --bash-output`.

Generic `tool` filtering is intentionally skipped for now.

## Example with custom root

```bash
agent-trace \
  --harness claude \
  --session <session-id> \
  --root /path/to/claude/projects \
  --bash-command
```
