# Native Codex transport (experimental)

Native mode sends forum notifications through Codex's app-server queue and opens
the normal Codex terminal UI on the same conversation. You can watch its work,
type messages, and use its terminal controls. Tmux remains the default; native
mode does not create tmux sessions or windows.

## Enable and launch

In `config.local.toml`:

```toml
[agents.codex]
transport = "codex_native"
cwd = "/absolute/path/to/project"
```

Restart the chat server so it uses the same configuration as the wrapper:

```bash
uv run agentchattr serve
```

In another terminal:

```bash
uv run agentchattr agent codex
```

The wrapper starts a dedicated Codex backend, connects the existing authenticated
MCP identity proxy, and opens Codex's UI in this terminal. For a fresh runtime,
the UI creates the conversation before notification delivery begins.
The backend listens on a Unix socket in a private temporary directory. The launch
output prints a runtime ID and the delivery log path. The log records the exact
attach command once the terminal creates its conversation; that command works
after the first message gives Codex saved history to resume. Run the wrapper in your own tmux window if you want terminal
detach/reattach. Detaching tmux keeps the wrapper running.

This option requires a Codex executable (`command = "codex"`, or its absolute
path), including when the configured agent has a custom name. Native mode uses
the standard Codex MCP proxy integration; provider-specific `mcp_inject` settings
are not used. Other CLI and API agents retain their existing transport.

### Backend options

Supported arguments after `--` are `-c/--config`, `-m/--model`, `-s/--sandbox`,
`-a/--ask-for-approval`, and `--no-alt-screen`. For example:

```bash
uv run agentchattr agent codex -- --no-alt-screen -s workspace-write -a on-request
```

Model, sandbox, approval, and configuration overrides go to the backend and the
fresh terminal so both use the same settings. A resumed terminal receives no
configuration overrides. `--no-alt-screen` goes to the UI. Unspecified settings come from Codex's
normal configuration. Unsupported arguments are rejected. The wrapper never
disables sandboxing to work around startup errors and never answers permission
requests. Use the Codex terminal to handle them.

### Stop and resume

Closing Codex's UI normally reopens it after three seconds. `--no-restart` makes
closing the UI stop the wrapper. To stop the default wrapper, exit the UI and
press `Ctrl+C` during the restart delay; inside the UI, Codex handles `Ctrl+C`
itself. When the wrapper stops, it terminates its
owned backend and proxy. Backend startup failure or an abnormal terminal exit
stops the wrapper rather than retrying indefinitely. The Linux backend launcher
also requests termination if the wrapper dies unexpectedly.

Resume an existing runtime explicitly:

```bash
uv run agentchattr agent codex --resume-runtime RUNTIME_ID
```

Use the same configuration/data directory, agent name, and `CODEX_HOME`. Supply
matching `--config` and `--data-dir` options if you used them originally. A resumed
runtime retains its working directory, backend options, thread UUID, and agent
identity; new pass-through arguments are rejected. Starting without
`--resume-runtime` creates a new identity and conversation.

Resume checks resolved model and permission settings before loading the saved
conversation. If your global Codex configuration changed those settings, restore
the original configuration first; the wrapper refuses to run old queued work with
different permissions.

Only one wrapper may own a runtime. Identity recovery fails if a fresh agent has
superseded the old registration; it never silently attaches the old conversation
to a new identity. Native deregistration retains the token as reclaimable for this
explicit recovery path. Default tmux/API deregistration still invalidates tokens.
Stopping the chat server does not automatically stop wrappers or backends.

Codex does not persist an empty conversation until its first user message. Fresh
startup therefore uses the normal terminal start flow, without a dummy model turn. An
empty runtime with no submitted notification or observed history can therefore
receive a new thread UUID on resume. Missing history after submission is treated
as uncertain, not as permission to replay the notification into a fresh thread.

## Delivery and recovery

Native notifications are stored in `DATA_DIR/native/deliveries.sqlite3`, keyed by
stable agent identity. Renaming the agent does not move its queue. Each event keeps
its channel, job, custom prompt, and the exact assembled wake-up prompt. Different
events are processed in order; the wrapper submits one outstanding notification
at a time while human input remains available in Codex.

The database and runtime logs are in a private directory. Runtime records include
the registration credential, so treat this directory as private application state.
Do not delete it to recover a stuck message.

```bash
uv run agentchattr delivery list
```

This lists event and runtime IDs, delivery states, and backend receipt/turn IDs,
without printing credentials or message bodies. States mean:

| State | Meaning |
| --- | --- |
| `pending` | Saved locally, not yet submitted by this attempt. |
| `submitting` | Prompt and attempt ID saved before the network operation. |
| `accepted` | Backend queue receipt or matching conversation item found. |
| `completed` | Matching backend turn completed; this does not certify the answer. |
| `failed` | Matching turn failed or was interrupted. |
| `uncertain` | Available backend evidence cannot establish the outcome, or duplicate matches exist. |
| `discarded` | Operator chose to stop tracking/retrying this notification. |

On reconnection, queue records and full conversation history are matched by client
message ID. Acceptance alone is not completion. Identical client IDs are **not**
assumed to deduplicate Codex submissions. Uncertain or failed work pauses new
automatic delivery for that runtime. The adapter does not switch to terminal
pasting if submission fails.

Inspect the printed delivery log and the Codex conversation before deciding
whether to retry. Stop the wrapper before using either recovery command:

```bash
uv run agentchattr delivery retry EVENT_ID
uv run agentchattr delivery discard EVENT_ID
uv run agentchattr agent codex --resume-runtime RUNTIME_ID
```

Retry creates a new attempt and can duplicate work if the old attempt ran.
Discard does **not** cancel work already queued or running in Codex. Recovery
commands accept pending, failed, and uncertain events; a submitting or accepted
event must first be reconciled by resuming its runtime. Attempt history is kept
for inspection. These commands require the same data-directory configuration as
the wrapper and server.

Existing JSONL queues are not migrated or truncated. If an instance has a nonempty
legacy queue, native startup refuses it. Restore the previous transport, drain
that queue, stop its wrapper, and then enable native mode in both processes.

## Compatibility and validation

Tested against Codex CLI 0.154.0. Startup checks its generated queue/history schema
and initializes the private Unix connection. The WebSocket client disables
compression negotiation because this Codex version rejects that extension on its
Unix listener. Missing required methods or startup failures stop the native path;
there is no silent tmux fallback.

Automated tests cover delivery persistence and ordering, lost receipts, duplicate
matches, identity isolation and rename, operator locks, protocol pagination,
ignoring approval requests, empty startup and terminal reopening, backend startup
failure, explicit runtime resume,
and backend termination after a wrapper crash. Existing transport and server
tests remain in place.

A real-CLI check with an isolated local model stub verified empty terminal startup
without a model request, the first subsequent notification,
notification delivery and completion tracking, preservation of an unsent draft,
same-conversation resume, and recovery of a queued notification after killing the
wrapper. No paid model calls were used. Because this environment cannot initialize
Codex's normal Linux sandbox, that check used an unsandboxed, isolated stub backend.

**Keep this feature experimental:** representative approval/denial dialogs,
terminal disconnection during approval, and the authenticated production model/MCP
path still require validation on a host with working sandbox support. Automated
tests prove that the adapter never sends approval decisions; they do not establish
how every Codex version routes approval prompts among connected clients.

See the [transport evaluation](transport-evaluation.md) for the original comparison
with tmux and Claude channels.
