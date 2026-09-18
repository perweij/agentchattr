"""Opt-in Codex backend owner with an ordinary interactive terminal client."""

import argparse
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from agentchattr import wrapper
from agentchattr.codex_transport import CodexRpc, DeliveryEngine, RpcError
from agentchattr.mcp_proxy import McpIdentityProxy
from agentchattr.native_store import NativeStore
from websockets.exceptions import InvalidHandshake

log = logging.getLogger(__name__)


def parse_options(extra):
    parser = argparse.ArgumentParser(prog="Codex native options", allow_abbrev=False)
    parser.add_argument("-c", "--config", action="append", default=[])
    parser.add_argument("-m", "--model")
    parser.add_argument("-s", "--sandbox", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("-a", "--ask-for-approval", choices=["untrusted", "on-request", "never"])
    parser.add_argument("--no-alt-screen", action="store_true")
    args = parser.parse_args(extra)
    overrides = list(args.config)
    for key, value in (("model", args.model), ("sandbox_mode", args.sandbox),
                       ("approval_policy", args.ask_for_approval)):
        if value is not None:
            overrides.append(f"{key}={json.dumps(value)}")
    # Match Codex's TOML-or-string semantics. Apply overrides to the backend
    # and fresh terminal; remote resume does not support permission overrides.
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError("Codex -c requires key=value")
        if key.strip().startswith("mcp_servers.agentchattr"):
            raise ValueError("The agentchattr MCP connection is managed by the wrapper")
    return {"overrides": overrides, "no_alt_screen": args.no_alt_screen}


def check_capabilities(command):
    with tempfile.TemporaryDirectory(prefix="agentchattr-schema-") as directory:
        result = subprocess.run([command, "app-server", "generate-json-schema", "--experimental", "--out", directory],
                                capture_output=True, timeout=30)
        required = ("ThreadQueueAddParams", "ThreadQueueListParams", "ThreadTurnsListParams", "ThreadReadResponse", "ThreadLoadedListParams")
        if result.returncode or any(not list(Path(directory).rglob(name + ".json")) for name in required):
            raise ValueError("This Codex CLI lacks the native queue/history API; use tmux or a compatible version")
        params = json.loads(next(Path(directory).rglob("ThreadQueueAddParams.json")).read_text())
        if "clientUserMessageId" not in params.get("properties", {}):
            raise ValueError("Codex queue lacks client message IDs required for delivery recovery")
        history = json.loads(next(Path(directory).rglob("ThreadReadResponse.json")).read_text())
        items = history.get("definitions", {}).get("ThreadItem", {}).get("oneOf", [])
        if not any("clientId" in item.get("properties", {}) for item in items):
            raise ValueError("Codex history lacks client IDs required for delivery recovery")


def require_empty_legacy_queue(data_dir, name):
    path = Path(data_dir) / f"{name}_queue.jsonl"
    if path.exists() and path.stat().st_size:
        raise ValueError(f"Legacy queue is nonempty: {path}. Drain it with the previous transport before using native mode.")


def backend_settings(result):
    """Resolved settings that must not silently change when a runtime resumes."""
    return {key: result.get(key) for key in (
        "model", "modelProvider", "approvalPolicy", "approvalsReviewer", "sandbox", "activePermissionProfile")}


def request_server(port, path, token, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 data=json.dumps(body).encode(),
                                 headers=wrapper._auth_headers(token, include_json=True))
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read())


def stop_process(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def connect_backend(process, socket_path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Codex backend exited; inspect the private backend.log (including sandbox startup errors)")
        try:
            return CodexRpc(socket_path, timeout=3)
        except (OSError, TimeoutError, ConnectionError, InvalidHandshake):
            time.sleep(0.1)
    raise RuntimeError("Codex Unix socket did not become ready; installed CLI may be incompatible")


class PromptBuilder:
    def __init__(self, runtime, port):
        self.runtime, self.port = runtime, port
        self.count, self.epoch = 0, None

    def __call__(self, entry):
        registration = self.runtime["registration"]
        name, token = registration["name"], registration["token"]
        role = wrapper._fetch_role(self.port, name)
        rules = wrapper._fetch_active_rules(self.port, token)
        self.count += 1
        selected = []
        if rules:
            interval = rules.get("refresh_interval", 10)
            if self.epoch != rules["epoch"] or (interval > 0 and self.count % interval == 0):
                selected = rules["rules"]
                self.epoch = rules["epoch"]
        return wrapper.build_trigger_prompt(entry, role=role, rules=selected,
                                            identity_hint=self.count == 1 and registration.get("slot", 1) > 1)


def run_owned(config, args, store, runtime, command):
    """Caller holds the runtime lock until all owned processes and threads stop."""
    port = config["server"].get("port", 8300)
    registration = runtime["registration"]
    agent_cfg = config["agents"][args.agent]
    require_empty_legacy_queue(config["server"]["data_dir"], registration["name"])
    mcp = config.get("mcp", {})
    proxy = McpIdentityProxy(f"http://127.0.0.1:{mcp.get('http_port', 8200)}", "/mcp",
                            registration["name"], registration["token"], port=runtime.get("proxy_port", 0))
    backend = rpc = tui = worker = None
    stop, failure = threading.Event(), []
    runtime_dir = store.root / runtime["id"]
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    handler = logging.FileHandler(runtime_dir / "delivery.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    native_loggers = [logging.getLogger(n) for n in (__name__, "agentchattr.native_store", "agentchattr.codex_transport")]
    old_levels = [logger.level for logger in native_loggers]
    old_propagation = [logger.propagate for logger in native_loggers]
    for logger in native_loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # Short path avoids AF_UNIX's 108-byte limit even for deep data directories.
    with tempfile.TemporaryDirectory(prefix="agentchattr-") as socket_dir:
        socket_path = Path(socket_dir) / "codex.sock"
        try:
            if proxy.start() is False:
                raise RuntimeError("Native MCP proxy port is occupied; refusing to share another instance's proxy")
            runtime["proxy_port"] = proxy.port
            env = {k: v for k, v in os.environ.items() if k not in {"CLAUDECODE", *agent_cfg.get("strip_env", [])}}
            env["CODEX_HOME"] = runtime["codex_home"]
            # Reuse the existing identity proxy launch configuration; native mode
            # uses Codex's standard proxy injection, including for named aliases.
            launch_args, env, inject_env, _ = wrapper._build_provider_launch(
                "codex", {}, registration["name"], store.root, proxy.url + "/mcp", [], env,
                token=registration["token"], mcp_cfg=mcp, project_dir=Path(runtime["cwd"]))
            env.update(inject_env)
            backend_args = [command, "app-server", "--listen", f"unix://{socket_path}"]
            for override in runtime["options"]["overrides"]:
                backend_args += ["-c", override]
            backend_args += launch_args
            store.save_runtime(runtime)
            with (runtime_dir / "backend.log").open("a") as output:
                backend = subprocess.Popen([sys.executable, "-m", "agentchattr.native_backend", str(os.getpid()), *backend_args],
                                           cwd=runtime["cwd"], env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=output, start_new_session=True)
            rpc = connect_backend(backend, socket_path)
            rpc.timeout = 15
            # Resolve settings without creating the terminal's conversation. Codex
            # cannot resume a zero-turn thread, even while it is loaded in memory.
            probe = rpc.call("thread/start", {"cwd": runtime["cwd"], "ephemeral": True})
            if runtime.get("backend_settings") and backend_settings(probe) != runtime["backend_settings"]:
                raise RuntimeError("Codex model or permission settings changed; restore the original Codex configuration before resuming this runtime")
            runtime["backend_settings"] = backend_settings(probe)
            store.save_runtime(runtime)
            engine = DeliveryEngine(store, runtime, rpc, PromptBuilder(runtime, port))
            print(f"  Native runtime: {runtime['id']} (experimental)")
            print(f"  Delivery log: {runtime_dir / 'delivery.log'}")
            print(f"  Resume later: agentchattr agent {args.agent} --resume-runtime {runtime['id']} (use matching --config/--data-dir)")

            def pump():
                nonlocal rpc
                next_heartbeat = 0
                try:
                    while not stop.is_set():
                        if backend.poll() is not None:
                            raise RuntimeError("Codex backend exited; resume the runtime to reconcile delivery")
                        if rpc.closed.is_set():
                            rpc.close()
                            rpc = connect_backend(backend, socket_path)
                            rpc.timeout = 15
                            rpc.thread_id = runtime["thread_id"]
                            try:
                                rpc.call("thread/resume", {"threadId": rpc.thread_id})
                            except RpcError as exc:
                                # An empty live TUI thread has no rollout to resume.
                                # Rejoin only the exact thread still in this backend.
                                if not exc.missing_thread or rpc.thread_id not in set(rpc.pages("thread/loaded/list", None)):
                                    raise
                            engine.rpc = rpc
                            engine.reconcile(recovering=True)
                        if stop.is_set():
                            break
                        if time.monotonic() >= next_heartbeat:
                            next_heartbeat = time.monotonic() + 5
                            try:
                                thread_info = rpc.call("thread/read", {"threadId": rpc.thread_id})["thread"]
                                status = thread_info["status"]
                                rpc.active = status["type"] == "active"
                                if not runtime.get("has_history") and (thread_info.get("preview") or rpc.active):
                                    runtime["has_history"] = True
                                    store.save_runtime(runtime)
                                reply = request_server(port, f"/api/heartbeat/{registration['name']}",
                                                       registration["token"], {"active": rpc.active})
                                if reply["name"] != registration["name"]:
                                    registration["name"] = reply["name"]
                                    proxy.agent_name = reply["name"]
                                    store.save_runtime(runtime)
                                require_empty_legacy_queue(config["server"]["data_dir"], registration["name"])
                            except urllib.error.HTTPError as exc:
                                if exc.code in (401, 403, 409):
                                    raise RuntimeError("Agent identity is no longer valid; refusing to create a replacement identity") from None
                                log.warning("Chat server heartbeat unavailable")
                            except (urllib.error.URLError, TimeoutError):
                                log.warning("Chat server heartbeat unavailable")
                        try:
                            if not stop.is_set():
                                engine.step()
                        except (ConnectionError, TimeoutError, OSError):
                            # A timeout may have lost only the receipt. Closing the
                            # connection forces reconciliation before any more work.
                            rpc.close()
                        stop.wait(1)
                except Exception as exc:
                    failure.append(exc)
                    stop.set()

            while not stop.is_set():
                if runtime.get("thread_id"):
                    try:
                        result = rpc.call("thread/resume", {"threadId": runtime["thread_id"]})
                    except RpcError as exc:
                        rows = store.deliveries(runtime["identity_id"])
                        if exc.missing_thread:
                            for row in rows:
                                if row["state"] in ("submitting", "accepted"):
                                    store.transition(row["id"], "uncertain", detail="Backend conversation missing; restore or inspect Codex history before retrying")
                        if not exc.missing_thread or any(r["attempt_id"] for r in rows) or runtime.get("has_history"):
                            raise
                        runtime["thread_id"] = None
                    else:
                        if backend_settings(result) != runtime["backend_settings"]:
                            raise RuntimeError("Codex did not preserve the runtime's model and permission settings")
                attach = [command, "--remote", f"unix://{socket_path}"]
                if runtime.get("thread_id"):
                    attach = [command, "resume", "--remote", f"unix://{socket_path}", runtime["thread_id"]]
                    print("  Attach: " + shlex.join(["env", "CODEX_HOME=" + runtime["codex_home"], *attach]))
                else:
                    # A fresh remote TUI supplies thread/start overrides of its
                    # own. Keep them aligned with the backend. Codex disallows
                    # these overrides only when resuming a remote conversation.
                    for override in runtime["options"]["overrides"]:
                        attach += ["-c", override]
                if runtime["options"]["no_alt_screen"]:
                    attach.append("--no-alt-screen")
                baseline = set(rpc.pages("thread/loaded/list", None))
                store.save_runtime(runtime)
                tui = subprocess.Popen(attach, cwd=runtime["cwd"], env=env)
                if not runtime.get("thread_id"):
                    # This backend belongs exclusively to this runtime. Only the
                    # newly launched TUI may create its conversation; exclude the
                    # settings probe and any previously loaded empty threads.
                    deadline = time.monotonic() + 120
                    next_heartbeat = 0
                    while True:
                        if tui.poll() is not None:
                            raise RuntimeError("Codex terminal exited before creating its conversation")
                        created = set(rpc.pages("thread/loaded/list", None)) - baseline
                        if len(created) > 1:
                            raise RuntimeError("Multiple Codex conversations appeared during startup; refusing ambiguous delivery")
                        if created:
                            thread_id = created.pop()
                            info = rpc.call("thread/read", {"threadId": thread_id})["thread"]
                            if info.get("ephemeral") or Path(info["cwd"]).resolve() != Path(runtime["cwd"]).resolve():
                                raise RuntimeError("Codex terminal created a conversation with unexpected settings")
                            runtime["thread_id"] = thread_id
                            store.save_runtime(runtime)
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Codex terminal did not create its conversation within 120 seconds")
                        if time.monotonic() >= next_heartbeat:
                            request_server(port, f"/api/heartbeat/{registration['name']}", registration["token"], {})
                            next_heartbeat = time.monotonic() + 5
                        time.sleep(0.1)
                rpc.thread_id = runtime["thread_id"]
                log.info("Terminal conversation: %s; attach: %s", rpc.thread_id,
                         shlex.join(["env", "CODEX_HOME=" + runtime["codex_home"], command,
                                     "resume", "--remote", f"unix://{socket_path}", rpc.thread_id]))
                list(rpc.pages("thread/queue/list", rpc.thread_id))
                engine.reconcile(recovering=True)
                worker = threading.Thread(target=pump, name="native-codex", daemon=True)
                worker.start()
                while tui.poll() is None and not stop.wait(0.2):
                    pass
                stopped = stop.is_set()
                stop.set()
                worker.join()
                if stopped or failure:
                    break
                if tui.returncode:
                    raise RuntimeError("Codex terminal exited with an error; inspect backend.log before resuming")
                if args.no_restart:
                    break
                # Never change the delivery thread while its worker is running.
                stop.clear()
                print("  Codex terminal closed; reopening in 3s (Ctrl+C stops this runtime).")
                stop.wait(3)
            if failure:
                raise failure[0]
        finally:
            stop.set()
            if rpc:
                rpc.close()
            if worker:
                worker.join()
            if tui and tui.poll() is None:
                tui.terminate()
                try:
                    tui.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    tui.kill()
                    tui.wait()
            stop_process(backend)
            proxy.stop()
            for logger, level, propagate in zip(native_loggers, old_levels, old_propagation):
                logger.removeHandler(handler)
                logger.setLevel(level)
                logger.propagate = propagate
            handler.close()


def main(config, args, extra):
    options = parse_options(extra)
    agent_cfg = config["agents"][args.agent]
    command = shutil.which(agent_cfg.get("command", args.agent))
    if not command:
        raise ValueError("Codex CLI is not on PATH")
    check_capabilities(command)
    data_dir = Path(config["server"]["data_dir"])
    store = NativeStore(data_dir)
    port = config["server"].get("port", 8300)
    codex_home = str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve())
    if args.resume_runtime:
        if extra:
            raise ValueError("A resumed runtime retains its backend options; do not supply new Codex arguments")
        runtime = store.runtime(args.resume_runtime)
        if runtime["base"] != args.agent or runtime["codex_home"] != codex_home:
            raise ValueError("Runtime belongs to a different agent or CODEX_HOME")
    else:
        require_empty_legacy_queue(data_dir, args.agent)
        registration = wrapper._register_instance(port, args.agent, args.label)
        runtime = store.create_runtime(args.agent, registration, cwd=agent_cfg["cwd"],
                                       codex_home=codex_home, options=options)
    print(f"  Native runtime ID: {runtime['id']}")
    with store.lock(runtime["id"]):
        registration = runtime["registration"]
        try:
            # Reclaim the stored identity, never silently register a fresh one.
            reply = request_server(port, f"/api/heartbeat/{registration['name']}", registration["token"], {})
            registration["name"] = reply["name"]
            store.save_runtime(runtime)
            run_owned(config, args, store, runtime, command)
        finally:
            try:
                request_server(port, f"/api/deregister/{registration['name']}", registration["token"], {})
            except Exception:
                pass
