"""Shared configuration and path resolution for the server and wrappers.

Precedence: config.toml < config.local.toml < environment < CLI.
Config paths are relative to the selected config directory; environment and CLI
path overrides are relative to the invocation directory. Callers receive absolute
paths and must not join them to the package location.
"""

import os
import tomllib
from pathlib import Path


# Mapping: env var name → (config section, key, is_int)
_ENV_OVERRIDES = [
    ("AGENTCHATTR_DATA_DIR",      "server", "data_dir",   False),
    ("AGENTCHATTR_PORT",          "server", "port",       True),
    ("AGENTCHATTR_MCP_HTTP_PORT", "mcp",    "http_port",  True),
    ("AGENTCHATTR_MCP_SSE_PORT",  "mcp",    "sse_port",   True),
    ("AGENTCHATTR_UPLOAD_DIR",    "images", "upload_dir", False),
]

# Mapping: CLI flag → environment variable
CLI_OVERRIDE_FLAGS = [
    ("--data-dir",      "AGENTCHATTR_DATA_DIR"),
    ("--port",          "AGENTCHATTR_PORT"),
    ("--mcp-http-port", "AGENTCHATTR_MCP_HTTP_PORT"),
    ("--mcp-sse-port",  "AGENTCHATTR_MCP_SSE_PORT"),
    ("--upload-dir",    "AGENTCHATTR_UPLOAD_DIR"),
]


def _apply_env_overrides(config: dict) -> None:
    """Apply AGENTCHATTR_* env vars to the config dict in-place."""
    for env_var, section, key, is_int in _ENV_OVERRIDES:
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        if is_int:
            try:
                value = int(raw)
            except ValueError:
                print(f"  Warning: {env_var}={raw!r} is not a valid integer, ignoring")
                continue
        else:
            # Path values: resolve relative paths against current working dir,
            # not against agentchattr's install directory.
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = (Path.cwd() / p).resolve()
            value = str(p)
        config.setdefault(section, {})[key] = value


def _merge(base: dict, local: dict) -> None:
    """Merge tables recursively; replace scalars and lists."""
    for key, value in local.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _resolve_path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    return str((base / path).resolve())


def load_config(root: Path | None = None, *, config_path: Path | None = None,
                overrides: dict | None = None) -> dict:
    """Load defaults, local settings and invocation overrides once.

    ``root`` is an optional config directory for library callers. CLI callers
    supply ``config_path``. Otherwise prefer local config, then XDG config,
    then bundled defaults with XDG data and the caller's working directory.
    """
    path = (config_path or (root or Path.cwd()) / "config.toml").expanduser().resolve()
    bundled = False
    if config_path is None and root is None and not path.exists():
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        path = (config_home / "agentchattr" / "config.toml").expanduser().resolve()
        if not path.exists():
            path = Path(__file__).resolve().with_name("defaults.toml")
            bundled = True
    with path.open("rb") as f:
        config = tomllib.load(f)
    local_path = path.with_name("config.local.toml")
    if not bundled and local_path != path and local_path.exists():
        with local_path.open("rb") as f:
            _merge(config, tomllib.load(f))

    if bundled:
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        data = (data_home / "agentchattr").expanduser().resolve()
        config.setdefault("server", {})["data_dir"] = str(data)
        config.setdefault("images", {})["upload_dir"] = str(data / "uploads")
        for agent in config.get("agents", {}).values():
            agent["cwd"] = str(Path.cwd())

    config.setdefault("server", {})["data_dir"] = _resolve_path(
        config.get("server", {}).get("data_dir", "./data"), path.parent)
    config.setdefault("images", {})["upload_dir"] = _resolve_path(
        config.get("images", {}).get("upload_dir", "./uploads"), path.parent)
    for agent in config.get("agents", {}).values():
        agent["cwd"] = _resolve_path(agent.get("cwd", "."), path.parent)

    for name, agent in config.get("agents", {}).items():
        transport = agent.get("transport", "tmux")
        if transport not in ("tmux", "codex_native"):
            raise ValueError(f"Unknown transport for {name}: {transport}")
        if transport == "codex_native":
            from agentchattr.wrapper import _provider_from_command
            if agent.get("type") == "api" or _provider_from_command(agent.get("command", name)) != "codex":
                raise ValueError("codex_native transport requires a Codex CLI agent")

    _apply_env_overrides(config)
    for flag, env in CLI_OVERRIDE_FLAGS:
        key = flag[2:].replace("-", "_")
        value = (overrides or {}).get(key)
        if value is not None:
            _, section, field, is_int = next(row for row in _ENV_OVERRIDES if row[0] == env)
            config.setdefault(section, {})[field] = (
                int(value) if is_int else _resolve_path(value, Path.cwd()))
    return config
