"""Command line entry point. Parsing never starts servers or agents."""

import argparse
import sys
import tomllib
from pathlib import Path

from agentchattr.config_loader import load_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentchattr", description="Local agent coordination forum (Linux)")
    commands = parser.add_subparsers(dest="command", required=True)
    from agentchattr.adoption import add_commands
    add_commands(commands)
    serve = commands.add_parser("serve", help="Start the web UI and MCP servers")
    serve.add_argument("--allow-network", action="store_true", help="Allow non-localhost binding (with confirmation)")
    agent = commands.add_parser("agent", help="Connect a configured CLI or API agent to a running server")
    agent.add_argument("agent", help="Agent name from configuration")
    agent.add_argument("--label", help="Custom display label")
    agent.add_argument("--no-restart", action="store_true", help="Do not restart a CLI agent on exit")
    agent.add_argument("--resume-runtime", help="Resume an existing native Codex runtime")
    delivery = commands.add_parser("delivery", help="Inspect or resolve native notification delivery")
    delivery.add_argument("action", choices=["list", "retry", "discard"])
    delivery.add_argument("event_id", nargs="?")
    for command in (serve, agent, delivery):
        command.add_argument("--config", type=Path, help="Config file (default: ./config.toml, then XDG config, then bundled defaults)")
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
    if args.command != "agent" and extra:
        parser.error("agent arguments after -- are only supported by the agent command")
    if args.command in ("adopt", "chat"):
        from agentchattr import adoption
        try:
            (adoption.run if args.command == "adopt" else adoption.chat)(args)
        except KeyboardInterrupt:
            print("Disconnected from agentchattr; the adopted agent is still running.")
        except (ValueError, OSError, RuntimeError) as exc:
            parser.error(str(exc))
        return
    try:
        config = load_config(config_path=args.config, overrides=vars(args))
    except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError) as exc:
        parser.error(f"Cannot load configuration: {exc}")
    if args.command == "delivery":
        import json
        from agentchattr.native_store import NativeStore
        if (args.action == "list") != (args.event_id is None):
            parser.error("delivery list takes no event ID; retry/discard require an event ID")
        try:
            store = NativeStore(config["server"]["data_dir"])
            if args.action == "list":
                print(json.dumps(store.listing(), indent=2))
            else:
                store.resolve(args.event_id, args.action)
                print(f"Delivery {args.event_id}: {args.action}. This does not cancel any backend work.")
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    elif args.command == "serve":
        from agentchattr import run
        run.main(config, allow_network=args.allow_network)
    else:
        agents = config.get("agents", {})
        if args.agent not in agents:
            parser.error(f"Unknown agent {args.agent!r}; configured agents: {', '.join(agents)}")
        native = agents[args.agent].get("transport") == "codex_native"
        if args.resume_runtime and not native:
            parser.error("--resume-runtime requires codex_native transport")
        if native:
            from agentchattr import wrapper_codex
            try:
                wrapper_codex.main(config, args, extra)
            except KeyboardInterrupt:
                print("Native Codex wrapper stopped; its runtime can be resumed.")
            except (ValueError, OSError, RuntimeError) as exc:
                parser.error(str(exc))
        elif agents[args.agent].get("type") == "api":
            if extra or args.no_restart:
                parser.error("API agents do not accept CLI pass-through arguments or --no-restart")
            from agentchattr import wrapper_api
            wrapper_api.main(config, args)
        else:
            from agentchattr import wrapper
            wrapper.main(config, args, extra)
