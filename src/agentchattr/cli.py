"""Command line entry point. Parsing never starts servers or agents."""

import argparse
import sys
import tomllib
from pathlib import Path

from agentchattr.config_loader import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentchattr", description="Local agent coordination forum (Linux)")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the web UI and MCP servers")
    serve.add_argument("--allow-network", action="store_true", help="Allow non-localhost binding (with confirmation)")
    agent = commands.add_parser("agent", help="Connect a configured CLI or API agent to a running server")
    agent.add_argument("agent", help="Agent name from configuration")
    agent.add_argument("--label", help="Custom display label")
    agent.add_argument("--no-restart", action="store_true", help="Do not restart a CLI agent on exit")
    for command in (serve, agent):
        command.add_argument("--config", type=Path, default=Path("config.toml"), help="Config file (default: ./config.toml)")
        command.add_argument("--data-dir", help="Override data directory")
        command.add_argument("--upload-dir", help="Override image upload directory")
        for flag in ("--port", "--mcp-http-port", "--mcp-sse-port"):
            command.add_argument(flag, type=int, help="Override configured port")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1:]
    parser = _parser()
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("agentchattr supports Linux only")
    if args.command == "serve" and extra:
        parser.error("agent arguments after -- are only supported by the agent command")
    try:
        config = load_config(config_path=args.config, overrides=vars(args))
    except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError) as exc:
        parser.error(f"Cannot load configuration: {exc}")
    if args.command == "serve":
        from agentchattr import run
        run.main(config, allow_network=args.allow_network)
    else:
        agents = config.get("agents", {})
        if args.agent not in agents:
            parser.error(f"Unknown agent {args.agent!r}; configured agents: {', '.join(agents)}")
        if agents[args.agent].get("type") == "api":
            if extra or args.no_restart:
                parser.error("API agents do not accept CLI pass-through arguments or --no-restart")
            from agentchattr import wrapper_api
            wrapper_api.main(config, args)
        else:
            from agentchattr import wrapper
            wrapper.main(config, args, extra)
