# agentchattr

A local work forum where humans and coding agents share channels, jobs, and conversations. Mention an agent in the browser to wake it in its terminal; it reads the forum through MCP and posts its response back.

This personal fork targets **Linux, Python 3.11+, and tmux**. It retains the existing provider integrations and chat features. See the [feature guide](docs/features.md) and [architecture review](docs/architecture.md).

## Start here

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), tmux through your distribution's package manager, and whichever agent CLI you want to use. From this checkout:

```bash
uv sync --locked
cp config.local.toml.example config.local.toml
```

Edit `config.local.toml` to point your agents at their working repository:

```toml
[agents.claude]
cwd = "/absolute/path/to/your/project"

[agents.codex]
cwd = "/absolute/path/to/your/project"
```

Start the server in one terminal:

```bash
uv run agentchattr serve
```

Start each agent in another terminal:

```bash
uv run agentchattr agent claude
uv run agentchattr agent codex
```

Open **http://localhost:8300** and send a message mentioning `@claude` or `@codex`. The server must already be running when an agent connects. CLI agents each have their own tmux session; detach with `Ctrl+B, D` and reattach using the session name printed by the wrapper. The wrapper must stay running to deliver mentions and heartbeats.

Stop each agent wrapper with `Ctrl+C`, then stop the server. Server shutdown does not yet stop agents. Codex also has an opt-in [native transport](docs/native-codex.md) that opens its normal terminal UI and tracks notification delivery durably. It remains experimental; tmux is the default. See the [transport evaluation](docs/transport-evaluation.md) for the comparison.

Pass agent CLI arguments after `--`:

```bash
uv run agentchattr agent codex --no-restart -- --no-alt-screen
```

`--label` sets a display label. `--no-restart` disables CLI restart after exit. Provider-specific arguments, including any approval-mode flags you choose, belong after `--`.

## Personal configuration

Keep shared defaults in `config.toml` and personal settings in ignored `config.local.toml`. Local tables merge recursively, including existing agents; lists and scalar values replace the defaults. For example:

```toml
[server]
port = 8310

[agents.codex]
cwd = "/absolute/path/to/project"
label = "My Codex"

[agents.local]
type = "api"
base_url = "http://localhost:11434/v1"
model = "your-installed-model"
label = "Local model"
```

Launch the API agent with the same command: `uv run agentchattr agent local`. Its `type = "api"` selects the API wrapper; it does not need tmux or a CLI. API agents do not accept CLI arguments after `--`.

Configuration precedence is **defaults → local file → environment → command-line flags**. Both commands accept:

| Flag | Environment variable | Setting |
|---|---|---|
| `--data-dir` | `AGENTCHATTR_DATA_DIR` | `server.data_dir` |
| `--upload-dir` | `AGENTCHATTR_UPLOAD_DIR` | `images.upload_dir` |
| `--port` | `AGENTCHATTR_PORT` | `server.port` |
| `--mcp-http-port` | `AGENTCHATTR_MCP_HTTP_PORT` | `mcp.http_port` |
| `--mcp-sse-port` | `AGENTCHATTR_MCP_SSE_PORT` | `mcp.sse_port` |

By default, commands read `config.toml` in the invocation directory. `--config /path/to/config.toml` chooses another file; its neighboring `config.local.toml` supplies personal overrides. Data directories, upload directories, and agent working directories in TOML resolve against that config directory. Relative CLI/environment path overrides resolve against the directory where you invoke the command. Paths beginning with `~` expand to your home directory.

Provider `mcp_settings_path` retains its existing meaning: relative paths are inside the agent's working directory; `~` and absolute paths select user-wide files. Some integrations update those provider files to register MCP. Existing unrelated MCP entries are preserved.

For an isolated instance, give the server and wrappers matching configuration and ports:

```bash
uv run agentchattr serve --config /path/to/project/config.toml
uv run agentchattr agent codex --config /path/to/project/config.toml
```

Use separate data/upload directories and ports for simultaneously running servers. Changing `--data-dir` alone does not change the upload directory. Existing JSON/JSONL data formats are unchanged; point the new configuration at your existing directories to retain history.

## Providers and security

The configuration includes Claude Code, Codex, Gemini, Antigravity, Kimi, Qwen, Kilo, CodeBuddy, Copilot, and MiniMax. Custom CLI providers can specify MCP injection settings; API providers use an OpenAI-compatible chat-completions endpoint. See comments in the configuration files for examples. Install and authenticate provider tools separately.

The application is intended for localhost use. The browser receives a session token; registered agents receive their own identity tokens. Agent tokens are supplied through generated provider configuration or an instance-specific MCP proxy. The default ports are 8300 for the UI, 8200 for MCP HTTP, and 8201 for MCP SSE. Prefer the wrapper's authenticated setup over manually registering a bare MCP URL.

The existing `serve --allow-network` option retains its confirmation prompt. HTTP is unencrypted, and messages can cause connected agents to execute tools. This fork does not add a remote-access security model.

## Development

Application code and packaged UI/template assets live under `src/agentchattr/`; tests and documentation are separate. No JavaScript build step is required.

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked python -m unittest discover -s tests
uv build
```

`pyproject.toml` owns project metadata, version, dependencies, and tooling. Commit dependency changes together with `uv.lock`. Ruff checks syntax and likely runtime errors without imposing a repository-wide formatting change. Linux CI runs the tests on Python 3.11 and 3.12 and exercises a freshly installed wheel.

The test suite uses temporary data, fake identities, and isolated ports. Tmux transport tests use a separate tmux socket and skip if tmux is absent. The server smoke test starts HTTP, WebSocket, and both MCP transports without invoking a paid agent or touching provider settings.

An optional browser regression script is available:

```bash
uv sync --locked --group browser
uv run --group browser playwright install chromium
# Start a separate test server with temporary data/uploads and unused ports first.
uv run --group browser python tests/browser/voice_typing.py http://127.0.0.1:PORT
```

That script sends test messages; use an isolated server.

## Migration from the original launchers

| Previous entry point | Replacement |
|---|---|
| `python run.py`, `start.sh` | `uv run agentchattr serve` |
| `python wrapper.py NAME`, `start_NAME.sh` | `uv run agentchattr agent NAME` |
| `python wrapper_api.py NAME` | `uv run agentchattr agent NAME` with `type = "api"` |
| Specialized approval-mode scripts | Pass the corresponding provider flags after `--` |
| `build_release.py` | `uv build` |

Agent commands no longer launch the server automatically. Windows launchers and injection backend, macOS launch branches, root-level Python scripts, and the hand-maintained release ZIP are removed. `python -m agentchattr` also exposes the new CLI once the package is installed. A wheel contains application resources; configuration and runtime data remain outside it. Source distributions include example configuration.

MIT licensed; derived from [bcurts/agentchattr](https://github.com/bcurts/agentchattr).
