# Fleet checkout integration

`fleet.scm` declares `agentchattr`, exposed through the executable
`bin/agentchattr`. The launcher resolves symlinks and works from any directory.
It requires Python 3.11 or newer. Help runs with the standard library and does
not download dependencies or create state.

For actual commands, the launcher uses `uv run --locked --no-dev` to prepare
the project's locked Python dependencies automatically. No separate install or
build hook is required. Its environment lives under
`$XDG_CACHE_HOME/agentchattr/<checkout-hash>/venv` (default `~/.cache`).
First use requires access to the Python package index or a populated uv cache.
The launcher's environment takes precedence over `UV_PROJECT_ENVIRONMENT`.

Configuration lookup is `--config`, otherwise `./config.toml`, otherwise
`$XDG_CONFIG_HOME/agentchattr/config.toml` (default `~/.config`), otherwise
the packaged `src/agentchattr/defaults.toml`. Explicit missing files are errors.
Existing configuration files retain their relative-path semantics and adjacent
`config.local.toml` overrides. Bundled defaults store data in
`$XDG_DATA_HOME/agentchattr` (default `~/.local/share/agentchattr`), uploads beneath
that directory, and launch agents in the caller's working directory.
CLI and environment overrides retain their existing precedence.

To customize a shared installation, copy `src/agentchattr/defaults.toml` to
`~/.config/agentchattr/config.toml`, then set absolute data/upload paths and
agent working directories. Use matching configuration for server and wrappers.
Project-local configuration may deliberately choose project-local storage.

The apt requirements are `python3`, `coreutils` (the shebang's `env`), `git`
(project discovery), `tmux` (terminal transport/adoption), and `xdg-utils`
(the UI's open-file action). The selecting Fleet role must declare `uv` and
the desired provider CLIs as external requirements. Install and authenticate
providers separately; API agents also need their configured service and keys.
No credentials or host setup are included in the manifest.

Fleet consumes the source repository's `main` branch. Publish the manifest,
executable with Git mode `100755`, package defaults, and supporting changes
there before enrolling another host. This preparation does not change Fleet
roles, install host packages, or publish the repository.
