"""Connect to an existing terminal without owning its agent process."""

import hashlib
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

from agentchattr.native_store import NativeStore
from agentchattr.projects import project_context


class ServerUnavailable(RuntimeError):
    pass


def request(port, path, token="", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and path == "/api/adoption/capabilities":
            raise RuntimeError("Restart the chat server with this agentchattr version to enable adoption") from None
        try:
            detail = json.loads(exc.read()).get("error", "")
        except (ValueError, AttributeError):
            detail = ""
        raise RuntimeError(f"Chat server rejected {path} (HTTP {exc.code})" + (f": {detail}" if detail else "")) from None
    except (urllib.error.URLError, TimeoutError):
        raise ServerUnavailable(f"Cannot reach agentchattr on port {port}; start the server first") from None


def process_info(pid):
    path = Path(f"/proc/{int(pid)}")
    # The comm field can contain spaces and parentheses.
    stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
    args = (path / "cmdline").read_bytes().split(b"\0")
    name = Path(os.fsdecode(args[0])).name if args[0] else ""
    return {"pid": int(pid), "parent": int(stat[1]), "group": int(stat[2]),
            "foreground": int(stat[5]), "start": stat[19], "name": name,
            "cwd": str((path / "cwd").resolve(strict=True))}


def inspect_pane(pane, socket=None):
    if not re.fullmatch(r"%[0-9]+", pane or ""):
        raise ValueError("Specify an exact tmux pane ID, such as --pane %7")
    cmd = ["tmux"] + (["-S", socket] if socket else [])
    result = subprocess.run([*cmd, "display-message", "-p", "-t", pane,
                             "#{socket_path}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}"],
                            capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise ValueError("The selected tmux pane is unavailable")
    socket_path, actual, pane_pid, dead = result.stdout.strip().split("\t")
    if actual != pane or dead == "1":
        raise ValueError("The selected tmux pane is no longer running")
    processes = {}
    for proc in Path("/proc").iterdir():
        if proc.name.isdecimal():
            try:
                info = process_info(proc.name)
                processes[info["pid"]] = info
            except (OSError, ValueError, IndexError):
                pass
    def descendant(info):
        seen = set()
        while info["pid"] not in seen:
            if info["pid"] == int(pane_pid):
                return True
            seen.add(info["pid"])
            info = processes.get(info["parent"])
            if not info:
                return False
        return False
    candidates = [p for p in processes.values() if p["name"] in ("codex", "claude")
                  and p["group"] == p["foreground"] and descendant(p)]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one foreground Codex or Claude process in this pane")
    agent = candidates[0]
    target = {"socket": str(Path(socket_path).resolve()), "pane": pane,
              "pane_pid": int(pane_pid), "pid": agent["pid"], "start": agent["start"],
              "base": agent["name"], "cwd": agent["cwd"]}
    target["key"] = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()
    return target


def still_attached(target):
    try:
        current = inspect_pane(target["pane"], target["socket"])
        return all(current[k] == target[k] for k in ("socket", "pane", "pane_pid", "pid", "start", "base", "cwd"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def write_private(path, data):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
    temporary.replace(path)


def chat_command(path):
    return shlex.join([sys.executable, "-m", "agentchattr", "chat", "--session", str(path)])


def notification_prompt(runtime, path, entry):
    command = chat_command(path)
    channel = entry.get("channel", runtime["registration"]["channels"][0])
    target = f"--job {int(entry['job_id'])}" if entry.get("job_id") else "--channel " + shlex.quote(channel)
    return (f"agentchattr notification for project {runtime['project']['project_root']} "
            f"(agent cwd: {runtime['project']['cwd']}). "
            f"Read the addressed conversation using: {command} read {target}. "
            f"Respond in that conversation using: {command} send {target} --message 'your reply'. "
            "These commands authenticate your assigned identity; do not use another session's credentials. "
            "Read once, perform the requested work within this project, then reply in chat. "
            "Do not poll chat in a loop. " + (f"Additional task context: {entry['prompt']}" if entry.get("prompt") else ""))


def run(args):
    capabilities = request(args.port, "/api/adoption/capabilities")
    if capabilities.get("protocol") != 1:
        raise RuntimeError("The running chat server does not support this adoption protocol")
    if getattr(args, "resume_runtime", None):
        if args.pane or args.tmux_socket or args.channel or args.label or args.remote or args.thread or args.notify != "manual":
            raise ValueError("Resume retains the original target and options; supply only --resume-runtime and --port")
        store = NativeStore(capabilities["data_dir"])
        runtime = store.runtime(args.resume_runtime)
        if runtime.get("kind") != "adopted":
            raise ValueError("This runtime was not created by adoption")
        with store.lock(runtime["id"]):
            if not still_attached(runtime["target"]):
                raise ValueError("The original adopted process is no longer in its pane")
            args.notify, args.remote, args.thread = runtime["notify"], runtime.get("remote"), runtime.get("thread_id")
            _run(args, saved=runtime, store=store)
    else:
        if not args.pane:
            raise ValueError("Specify --pane or --resume-runtime")
        _run(args)


def _run(args, saved=None, store=None):
    target = saved["target"] if saved else inspect_pane(args.pane, args.tmux_socket)
    project = saved["project"] if saved else project_context(target["cwd"])
    if saved:
        registration = saved["registration"]
        reply = request(args.port, f"/api/heartbeat/{registration['name']}", registration["token"], {})
        registration["name"] = reply["name"]
        registration["channels"] = reply.get("channels", registration["channels"])
    rpc = None
    if args.notify == "codex":
        if target["base"] != "codex" or not args.remote or not args.remote.startswith("unix:///") or not args.thread:
            raise ValueError("Native adoption requires a Codex pane, --remote unix:///socket/path and --thread THREAD_ID")
        from agentchattr.codex_transport import CodexRpc
        rpc = CodexRpc(args.remote.removeprefix("unix://"))
        try:
            if args.thread not in set(rpc.pages("thread/loaded/list", None)):
                raise ValueError("That thread is not loaded in the selected backend; adoption never starts or resumes a backend")
            info = rpc.call("thread/read", {"threadId": args.thread})["thread"]
            if Path(info["cwd"]).resolve() != Path(target["cwd"]).resolve():
                raise ValueError("Thread and terminal working directories do not match")
            rpc.thread_id = args.thread
        except BaseException:
            rpc.close()
            raise
    elif args.remote or args.thread:
        raise ValueError("--remote and --thread require --notify codex")
    try:
        if not saved:
            registration = request(args.port, "/api/register", body={
                "base": target["base"], "label": args.label,
                "adoption": {"target": target, "channels": args.channel, "notify": args.notify,
                             "remote": args.remote, "thread": args.thread}})
    except BaseException:
        if rpc:
            rpc.close()
        raise
    channels = registration["channels"]
    token = registration["token"]
    runtime = saved
    try:
        if not saved:
            store = NativeStore(registration.pop("data_dir"))
            runtime = store.create_runtime(target["base"], registration, target=target, project=project,
                                           kind="adopted", port=args.port, notify=args.notify,
                                           thread_id=args.thread, remote=args.remote)
        directory = store.root / runtime["id"]
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "chat.json"
        write_private(path, {"port": args.port, "token": token})
        command = chat_command(path)
        print(f"Adopted @{registration['name']} in #{channels[0]}\nProject: {project['project_root']}")
        print(f"Runtime: {runtime['id']}\nChat command: {command}")
        print(f"Recover after a monitor crash: agentchattr adopt --resume-runtime {runtime['id']} --port {args.port}")
        print("Keep this monitor running. Ctrl+C disconnects chat and leaves the agent running.")
        initial = notification_prompt(runtime, path, {"channel": channels[0]})
        if args.notify != "manual":
            if args.notify == "tmux":
                print("Terminal delivery enabled: keep the agent composer empty while receiving notifications.")
            if not saved:
                store.enqueue(registration["identity_id"], {"channel": channels[0]})
        else:
            print("Paste this instruction into your agent when ready:\n" + initial)
        with contextlib.nullcontext() if saved else store.lock(runtime["id"]):
            if rpc:
                from agentchattr.codex_transport import DeliveryEngine
                engine = DeliveryEngine(store, runtime, rpc, lambda entry: notification_prompt(runtime, path, entry))
            offline = False
            while still_attached(target):
                try:
                    reply = request(args.port, f"/api/heartbeat/{registration['name']}", token, {})
                except ServerUnavailable:
                    if not offline:
                        print("Chat server unavailable; notifications paused until it reconnects.", flush=True)
                    offline = True
                    time.sleep(2)
                    continue
                if offline:
                    print("Chat server reconnected.", flush=True)
                    offline = False
                registration["name"] = reply["name"]
                registration["channels"] = reply.get("channels", registration["channels"])
                rows = store.deliveries(registration["identity_id"], open_only=True)
                for row in rows:
                    if row["state"] == "pending" and json.loads(row["payload"]).get("channel", "general") not in registration["channels"]:
                        store.transition(row["id"], "discarded", expected_state="pending", detail="Agent left the notification's channel")
                rows = store.deliveries(registration["identity_id"], open_only=True)
                if rpc:
                    if rpc.closed.is_set():
                        from agentchattr.codex_transport import CodexRpc
                        rpc.close()
                        rpc = CodexRpc(args.remote.removeprefix("unix://"))
                        rpc.thread_id = args.thread
                        if args.thread not in set(rpc.pages("thread/loaded/list", None)):
                            raise RuntimeError("The adopted Codex thread is no longer loaded; refusing to resume it automatically")
                        engine.rpc = rpc
                    try:
                        engine.step()
                    except (ConnectionError, TimeoutError, OSError):
                        rpc.close()
                elif args.notify == "tmux":
                    deliver_terminal(store, runtime, path, rows)
                elif rows and [r["id"] for r in rows] != runtime.get("last_notified"):
                    for row in rows:
                        print(notification_prompt(runtime, path, json.loads(row["payload"])), flush=True)
                    runtime["last_notified"] = [r["id"] for r in rows]
                time.sleep(2)
        print("Original agent process or working directory changed; disconnected from chat.")
    finally:
        recoverable = runtime is not None and sys.exc_info()[0] is not None and sys.exc_info()[0] is not KeyboardInterrupt
        if rpc:
            rpc.close()
        try:
            request(args.port, f"/api/deregister/{registration['name']}", token, {"recoverable": recoverable})
        except RuntimeError:
            print("Could not deregister while the server is unavailable; presence will expire.", file=sys.stderr)
        finally:
            if runtime:
                for row in store.deliveries(registration["identity_id"], open_only=True):
                    if row["state"] == "submitting":
                        store.transition(row["id"], "uncertain", detail="Adoption monitor stopped during submission; inspect the original conversation")
                (store.root / runtime["id"] / "chat.json").unlink(missing_ok=True)


def deliver_terminal(store, runtime, path, rows):
    # A crash or partial paste is not permission to replay into a live composer.
    if any(r["state"] != "pending" for r in rows):
        raise RuntimeError("Delivery is uncertain; inspect the agent conversation before reconnecting")
    if not rows:
        return
    row = rows[0]
    prompt = notification_prompt(runtime, path, json.loads(row["payload"]))
    if not store.transition(row["id"], "submitting", expected_state="pending", prompt=prompt,
                            detail="Offering notification to adopted terminal"):
        return
    from agentchattr import wrapper_unix
    target = runtime["target"]
    try:
        if not still_attached(target):
            raise RuntimeError("Original agent is no longer the foreground terminal process")
        delivered = wrapper_unix.inject(prompt, tmux_session=target["pane"], socket_path=target["socket"],
                                        before_send=lambda: still_attached(target))
        store.transition(row["id"], "delivered" if delivered else "uncertain",
                         detail="Terminal accepted paste and Enter; model completion is unverified" if delivered else
                         "Terminal delivery uncertain; inspect the conversation before retrying")
    except Exception:
        store.transition(row["id"], "uncertain", detail="Terminal delivery interrupted; inspect before retrying")
        raise


def chat(args):
    session = json.loads(args.session.read_text())
    body = {"action": args.action, "channel": args.channel, "job_id": args.job,
            "message": args.message or "", "limit": args.limit}
    if args.action == "send" and args.message is None:
        body["message"] = sys.stdin.read()
    response = request(session["port"], "/api/adoption/chat", session["token"], body)
    if response["result"].startswith("Error:"):
        raise RuntimeError(response["result"])
    print(response["result"])


def add_commands(commands):
    adopt = commands.add_parser("adopt", help="Connect an existing Codex/Claude tmux pane to chat")
    adopt.add_argument("--pane", help="Exact pane ID, e.g. %%7 (tmux list-panes -a)")
    adopt.add_argument("--resume-runtime", help="Recover a crashed adoption monitor for its original process")
    adopt.add_argument("--tmux-socket", help="Explicit tmux socket path")
    adopt.add_argument("--port", type=int, default=os.environ.get("AGENTCHATTR_PORT", "8300"))
    adopt.add_argument("--channel", action="append", help="Join a channel; repeat for multiple (default: project channel)")
    adopt.add_argument("--label")
    adopt.add_argument("--notify", choices=["manual", "tmux", "codex"], default="manual",
                       help="manual: copy instructions; tmux: paste-and-Enter; codex: existing native queue")
    adopt.add_argument("--remote", help="Existing Codex Unix socket URL for native notifications")
    adopt.add_argument("--thread", help="Exact existing Codex thread ID for native notifications")
    chat_parser = commands.add_parser("chat", help="Authenticated chat bridge for an adopted session")
    chat_parser.add_argument("--session", type=Path, required=True)
    chat_parser.add_argument("action", choices=["read", "send"])
    chat_parser.add_argument("--channel", default="")
    chat_parser.add_argument("--job", type=int, default=0)
    chat_parser.add_argument("--message", help="Reply text; omitted send reads stdin")
    chat_parser.add_argument("--limit", type=int, default=20)
