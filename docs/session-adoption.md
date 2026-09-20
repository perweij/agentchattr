# Adopt an existing terminal session

Adoption connects a running Codex or Claude conversation to agentchattr without
relaunching it or taking ownership of its process. The server must be running on
the same machine and under the same user account. Restart an older server after
updating agentchattr; the client checks adoption support before registering.

From a different terminal or tmux pane, find the target pane:

```bash
tmux list-panes -a -F '#{pane_id} #{pane_current_command} #{pane_current_path}'
uv run agentchattr adopt --pane %7
```

This starts a foreground monitor and prints an instruction to paste into the
agent. The instruction includes authenticated read/reply commands. No provider
configuration files, agent instructions, or MCP settings are modified.

The `adopt` command connects to localhost port 8300, or `--port PORT` /
`AGENTCHATTR_PORT`. It does not read the project's `config.toml`. If agentchattr is
installed as a tool, run `agentchattr adopt` from any folder. When using this
checkout, run `uv run --project /path/to/agentchattr-foundation agentchattr adopt`.
The project comes from the target agent's process, not the monitor's directory.
For a non-default tmux server, pass `--tmux-socket /absolute/socket/path`.

## Notification modes

| Mode | Command option | Behavior |
| --- | --- | --- |
| Manual (default) | `--notify manual` | Prints instructions for pending notifications; never types into the terminal. |
| Terminal | `--notify tmux` | Pastes a notification and presses Enter in the selected pane. Works with ordinary Codex and Claude sessions. |
| Native Codex | `--notify codex --remote unix:///path/to/codex.sock --thread UUID` | Queues on the explicitly selected, already-loaded Codex conversation. Leaves the terminal composer alone. |

For automatic delivery to an ordinary tmux session:

```bash
uv run agentchattr adopt --pane %7 --notify tmux
```

Clear any draft before choosing terminal delivery and keep the composer empty
while notifications can arrive. Tmux acceptance is not proof that the model read
the message. The monitor checks the original process, process start time, pane,
socket, and cwd before pasting and before Enter, but this is not an atomic lock
on terminal input. It cannot reliably distinguish a composer from an approval
dialog or eliminate the race where a process exits immediately before input.
Use manual delivery when interacting with drafts or dialogs.

Native Codex adoption is experimental and requires a known app-server socket and
exact thread ID. It never guesses the thread from "most recent" history and never
starts or resumes a backend. An ordinary Codex invocation is not guaranteed to
expose a usable socket. If you already run its remote TUI:

```bash
uv run agentchattr adopt --pane %7 --notify codex \
  --remote unix:///path/to/codex.sock --thread YOUR_THREAD_UUID
```

The selected thread must be loaded and its cwd must match the pane's agent. The
binding stays on that thread: switching the TUI to a different conversation does
not move the binding. Stop the monitor and adopt the new conversation explicitly.
Native delivery uses stable client message IDs and the existing conservative
delivery reconciliation. The observer never answers approval requests.

## Identity and projects

Each connection has its own token and identity. Multiple instances receive names
such as `codex-1` and `codex-2`; CLI replies follow renames through the token rather
than trusting a sender supplied by the model. An already-adopted pane or native
thread is rejected, including when it is opened in another pane.

By default adoption joins one project channel. The project is the canonical Git
worktree root, or the canonical cwd outside a Git worktree. Subdirectories and
symlink aliases share a channel. Separate worktrees and same-named directories
under different parents have different channels. A short hash of the canonical
path distinguishes names, for example `frontend-a482fd0157`. Channel renames are
remembered for subsequent automatic adoptions of that project.

Use repeated `--channel` options to choose memberships instead:

```bash
uv run agentchattr adopt --pane %7 --channel frontend --channel review --notify tmux
```

The UI shows the project beside the agent label; the tooltip shows its root, cwd,
and memberships. Click the agent pill to change its channels. Mention suggestions,
family mentions, `@all`, default routing, and direct notifications are scoped to
membership. Join agents from different projects to a shared channel deliberately
when they should collaborate. `general` remains available.

Agents launched with the existing `agent` command default to `general`, including
persisted instances that have no membership metadata. To use other channels,
change membership in the UI or configure a base agent's `channels` list. This is
a change from the previous behavior where every instance was eligible everywhere.
Membership is a coordination boundary, not filesystem or tenant isolation.

## Chat bridge and lifecycle

The monitor prints an absolute command of this form:

```bash
/path/to/python -m agentchattr chat --session /private/runtime/chat.json read
/path/to/python -m agentchattr chat --session /private/runtime/chat.json send --message 'Done.'
```

Use `--channel NAME` or `--job ID` to target a conversation. A send without
`--message` reads stdin, which also supports multiline replies. The credential
stays in a mode-0600 file under the server's private native runtime directory;
the token is not placed in the prompt or command arguments. The bridge requires
the harness's normal shell-tool access to that file and the local server. It does
not bypass the harness's sandbox or permission prompts.

Keep the monitor running. `Ctrl+C` disconnects it and removes the bridge credential
file; the original agent, backend, and tmux session stay running. Process exit,
replacement, or a change to its process cwd also disconnects adoption. Temporary
chat-server unavailability pauses delivery, and a still-running monitor reclaims
its identity when the same server data returns. An expired token stops the monitor
instead of silently registering a replacement.

If the monitor crashes while the original agent remains running, recover it with
the runtime ID printed at adoption:

```bash
uv run agentchattr adopt --resume-runtime RUNTIME_ID --port 8300
```

Recovery retains the original process binding, identity, notification mode, and
queue. It refuses a changed process or a runtime already owned by another monitor.
A normal `Ctrl+C` disconnect revokes the token; use a fresh adoption after that.

Uncertain terminal submission is never retried automatically. `delivered` means
terminal acceptance or a manual bridge read, not model completion; native
`completed` means the matching backend turn completed. Delivery records remain
in the native SQLite store for inspection. A new adoption creates a new identity
and does not replay the disconnected identity's queue. Adopted runtimes cannot
be resumed by the backend-owning `agent --resume-runtime` command. After an
uncertain send, inspect the original conversation before reconnecting and asking
the agent to read chat again. While its monitor is stopped, the existing
`delivery retry` / `delivery discard` commands can resolve an uncertain event
before `adopt --resume-runtime`; retry can duplicate work that already ran.

## Research and validation

Codex documents a remote TUI, queue operations, and MCP configuration reload via
its [app-server interface](https://learn.chatgpt.com/docs/app-server). Those APIs
require a reachable backend; they do not establish arbitrary running-process
adoption. Claude's [channel reference](https://code.claude.com/docs/en/channels-reference)
describes a startup-configured MCP subprocess and channel enablement flags.
The CLI bridge avoids relying on hot insertion of per-instance MCP configuration.

Automated tests exercise project identity, routing, authenticated replies, duplicate
adoption, process replacement, partial terminal delivery, server reconnect, and a
real server/tmux/bridge lifecycle using a deterministic harness. A separate Codex
0.154 check with an isolated local model stub verified native delivery, retained
unsent drafts, and survival of the original backend and TUI after disconnect.
Browser checks verified project labels, membership editing, and mention filtering.
No paid model calls were used. Live Claude model behavior and sandbox-restricted
bridge execution remain dependent on the user's harness configuration.
