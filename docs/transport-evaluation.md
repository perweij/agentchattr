# Agent transport evaluation

Evaluated 2026-09-17 on Linux with Codex CLI **0.154.0**, Claude Code
**2.1.274**, and tmux **3.4**.

The resulting opt-in prototype is now described in [Native Codex transport](native-codex.md).
The findings below record the original evaluation.

## Decision

Keep tmux as the production transport for now. Prototype **native Codex queue
delivery with an attachable Codex terminal UI** next. Keep **Claude channels
experimental** until availability and interactive behavior can be verified with
normal account authentication. Keep tmux for other CLI providers.

The defining requirement is access to the **same live agent conversation**:
watch its work, type directly, and handle its permission prompts. A separate
headless run or a transcript viewer is not equivalent.

This evaluation changes no production transport, configuration, or HTTP/MCP
interface. Shared tmux layout and coordinated shutdown remain separate decisions.

## Method and limits

The real CLIs ran in a temporary working directory with separate `CODEX_HOME`
and `CLAUDE_CONFIG_DIR` directories. A dedicated tmux socket, launched with
`-f /dev/null`, provided terminals for observation. No existing agent sessions
were targeted. Using tmux to observe these experiments does not make it the
native message transport: Codex messages went through its app server; the Claude
probe emitted MCP notifications.

A loopback HTTP stub produced deterministic model responses (`PROBE_REPLY`) and
recorded requests. Codex used a custom Responses provider without credentials;
Claude used a dummy API key and a loopback Anthropic endpoint. No paid model
calls were needed. These tests establish CLI/control-plane behavior, not model
reasoning quality or real-account availability. Title-generation requests were
excluded when counting conversation turns.

Codex initially failed sandbox initialization because this environment cannot
create the required user namespaces. The main transport experiments therefore
used an isolated app server with sandbox mode `danger-full-access` and a text-only
stub. A subsequent approval probe emitted the harmless command
`echo TRANSPORT_APPROVAL_PROBE`; it ran without a dialog in that configuration,
so it does **not** count as a successful approval test. Trying to change permissions
when attaching to a remote conversation failed with
`Permission overrides are not supported when resuming a remote task.`
Production permissions must be configured at the backend and tested separately;
do not copy the probe's sandbox setting into the application.

Selected local evidence is preserved in
[transport-evaluation-evidence.json](transport-evaluation-evidence.json).

## Results

### Codex: promising native transport, incomplete rollout validation

The installed CLI advertises:

```text
codex queue --remote <endpoint> --thread <UUID-or-name> --message <text>
codex resume --remote <endpoint> <thread-UUID>
```

The experiment used an explicit loopback WebSocket app server and exact thread
UUIDs. The terminal UI and the queue client connected to that same server.
Official documentation describes the remote TUI, structured turn control, and
event notifications; it labels the app-server/WebSocket interface experimental.
The installed CLI's generated schema additionally exposed `thread/queue/add`,
`thread/queue/list`, and related methods. Queue behavior below comes from local
tests, not an assumed stability guarantee.
[Official app-server documentation](https://learn.chatgpt.com/docs/app-server).

| Scenario | Observed result |
| --- | --- |
| Idle conversation, unsent human draft | Queue command returned a submission ID; the message and reply appeared in the TUI. `HUMAN_DRAFT_UNSUBMITTED` remained in the composer. |
| Busy conversation | With a delayed model response, the second message appeared in the pending queue. Both messages subsequently ran in order, once each. |
| Direct human input | A manually typed and submitted message reached the model stub and received a reply in the same conversation. |
| No terminal client | After closing the TUI, a queued message still reached the stub. A newly attached TUI displayed that message and reply. |
| Two conversations | A message targeted at a second UUID ran there and did not appear in the first conversation's TUI. |
| Unknown UUID | Queue command exited nonzero and reported that no rollout existed. |
| Retry with identical client message ID | Two `thread/queue/add` calls returned different submission IDs and produced two turns. **Do not assume idempotency.** |
| App-server restart | Pending submissions were present before SIGTERM. After restarting with the same isolated home, the TUI reconnected and both marked messages appeared and reached the stub once. This is a graceful-restart observation, not a crash/durability guarantee. |
| Activity events | Received `turn/started`, `thread/status/changed`, `item/agentMessage/delta`, and `turn/completed`; native activity reporting need not hash terminal output. |
| Empty conversation reconnect | Creating a thread and disconnecting before its first turn left no resumable rollout in this test. Initialization must account for that lifecycle. |
| Approval dialog | **Not verified.** The permission limitations described above prevented a representative test. |

Successful `codex queue` output establishes acceptance with an ID, not successful
completion of a turn. The direct protocol is preferable for a future adapter
because it exposes structured IDs, pending submissions, completion, and errors.
The CLI command is useful for a small proof of concept but would otherwise need
output parsing and additional queries.

Keep a durable mapping from agent instance to backend endpoint and thread UUID.
Agent display names are mutable and should not be the transport identity. An
ambiguous disconnect must be reconciled before retrying: even reuse of the same
`clientUserMessageId` did not suppress duplicates in this version.

### Claude: channel connection succeeds, feature availability blocks delivery

A small stdio MCP probe advertised the documented experimental `claude/channel`
capability and emitted `notifications/claude/channel`, launched with
`--dangerously-load-development-channels server:transport-probe`.

Claude completed MCP initialization, then logged:

```text
Channel notifications skipped: channels feature is not currently available
```

The emitted test message did not start a model request or appear in the terminal;
the manually typed draft remained. **That is blocked delivery, not a passed
draft-preservation test.** It does not establish whether the user's normal
authenticated account has channel access. We did not alter feature gates or
organization policy to force the experiment through.

The documented contract supports delivery into an open session over stdio, queues
events while busy, and can batch them on the next turn. Notifications have no
processing acknowledgement: a completed write can still be silently dropped.
An application-level receipt is needed for stronger delivery tracking.
[Channels reference](https://code.claude.com/docs/en/channels-reference).

Channels remain a research preview with authentication, rollout, and allowlist
constraints. The docs distinguish visible inbound messages from channel reply
text, which is not shown in the terminal. We must inspect actual tool activity
and replies before deciding whether the resulting visibility meets our requirement.
[Channels availability and behavior](https://code.claude.com/docs/en/channels).

Busy delivery, direct interaction after an event, approval dialogs, reconnection,
multiple instances, and duplicate delivery remain unverified for Claude because
the availability gate prevented the first event from being delivered.

### Tmux baseline

The existing five real-tmux injection tests passed:

```bash
uv run --locked python -m unittest discover -s tests -p 'test_inject_transport.py'
```

They verify byte delivery and transport failure handling, including long pastes;
they do not prove that a CLI accepted a chat turn. Inspection of the wrapper
shows these remaining limitations:

- Paste and Enter target terminal input without knowing whether the CLI is in a
  composer, permission dialog, or another screen. Human drafts can collide with
  automatic input. These collision cases were not simulated against a real CLI
  in the baseline run.
- Injection resolves the session's active pane. A shared session would need a
  stable per-agent pane ID before changing window layout.
- Activity is inferred from screen changes rather than agent state.
- The queue watcher clears its file before delivery and does not act on the
  injection result. Switching transports alone will not repair message loss.

Tmux still supplies useful persistence and direct terminal access across providers.
A custom PTY proxy would inherit terminal-input ambiguity while adding resize,
signal, and reconnect handling to this project. Headless SDK/ACP integrations
would need an appropriate frontend to satisfy the same-conversation requirement.

## Follow-up implementation sequence

1. Build an **opt-in Codex adapter**, retaining tmux as the default. Create and
   retain the backend thread UUID, preserve existing per-instance MCP identity,
   deliver wake-up prompts using the native queue, and print an exact TUI attach
   command. Let users place that TUI in terminals or tmux themselves. Do not
   silently fall back to terminal pasting after an uncertain native submission.
2. Repair the application's queue lifecycle before claiming reliable delivery:
   retain events through acceptance, record submission IDs, distinguish queued,
   completed, and uncertain outcomes, and reconcile on reconnect. Establish
   retry policy with tests; do not promise exactly-once processing.
3. Validate approvals on a host with working sandbox support, with permissions
   set on the backend. Test a queued mention while a dialog is open, human
   approval/denial, terminal disconnection during approval, and restart with
   uncertain submissions. Also test hard crashes, idle pending-queue recovery,
   long messages, and the production authenticated model/MCP path.
4. Re-evaluate Claude channels with normal account authentication and an enabled
   development channel. Require the same interactive scenarios, visibility of
   work, per-instance routing, and an explicit receipt strategy before an opt-in
   adapter. Keep the ordinary MCP chat read/reply tools as the forum interface.

The next implementation should be scoped to the Codex adapter and delivery
lifecycle; replacing Claude's transport is not justified by this evaluation.
