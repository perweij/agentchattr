"""Real server, tmux and chat bridge; no provider credentials or model requests."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from agentchattr.adoption import inspect_pane


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class AdoptionLifecycleTests(unittest.TestCase):
    def test_existing_terminal_reads_and_replies_and_survives_disconnect(self):
        with tempfile.TemporaryDirectory(prefix="adopt-test-") as directory:
            root = Path(directory)
            web_port, http_port, sse_port = port(), port(), port()
            config = root / "config.toml"
            config.write_text(f'''[server]
port={web_port}
data_dir="{root}/data"
[mcp]
http_port={http_port}
sse_port={sse_port}
[agents.codex]
command="codex"
cwd="{root}"
transport="codex_native"
''')
            env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTCHATTR_")}
            env["PYTHONUNBUFFERED"] = "1"
            sock = str(root / "tmux.sock")
            monitor = server = None
            with (root / "server.log").open("w+") as server_log, (root / "monitor.log").open("w+") as monitor_log:
                try:
                    server = subprocess.Popen([sys.executable, "-m", "agentchattr", "serve", "--config", str(config)],
                                               stdout=server_log, stderr=server_log, env=env)
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{web_port}/api/roles", timeout=1):
                                break
                        except OSError:
                            if time.monotonic() >= deadline or server.poll() is not None:
                                server_log.seek(0)
                                self.fail(server_log.read())
                            time.sleep(0.1)
                    fixture = Path(__file__).parent / "fixtures" / "adopted_terminal.py"
                    command = "exec -a codex " + shlex.join([sys.executable, str(fixture), str(root), sys.executable])
                    subprocess.run(["tmux", "-S", sock, "new-session", "-d", "-s", "existing", "-c", str(root), command],
                                   check=True, capture_output=True)
                    pane = subprocess.check_output(["tmux", "-S", sock, "display-message", "-p", "#{pane_id}"], text=True).strip()
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            target = inspect_pane(pane, sock)
                            break
                        except ValueError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(.1)
                    monitor = subprocess.Popen([sys.executable, "-m", "agentchattr", "adopt", "--pane", pane,
                                                "--tmux-socket", sock, "--port", str(web_port), "--notify", "tmux"],
                                               stdout=monitor_log, stderr=monitor_log, env=env)
                    messages = root / "data" / "agentchattr_log.jsonl"
                    deadline = time.monotonic() + 20
                    while True:
                        if messages.exists() and "Adopted terminal replied" in messages.read_text():
                            break
                        if time.monotonic() >= deadline or monitor.poll() is not None:
                            monitor_log.seek(0)
                            capture = subprocess.run(["tmux", "-S", sock, "capture-pane", "-pt", pane], capture_output=True, text=True)
                            server_log.seek(0)
                            self.fail(monitor_log.read() + "\n" + capture.stdout + "\n" + server_log.read())
                        time.sleep(.1)
                    self.assertEqual(len((root / "notifications.jsonl").read_text().splitlines()), 1)
                    saved = [json.loads(line) for line in messages.read_text().splitlines()]
                    reply = next(m for m in saved if m.get("text") == "Adopted terminal replied")
                    self.assertEqual(reply["sender"], "codex")
                    self.assertNotEqual(reply["channel"], "general")
                    monitor.send_signal(signal.SIGINT)
                    self.assertEqual(monitor.wait(timeout=10), 0)
                    self.assertEqual(inspect_pane(pane, sock)["pid"], target["pid"])
                    self.assertEqual(list((root / "data" / "native").glob("*/chat.json")), [])
                finally:
                    if monitor and monitor.poll() is None:
                        monitor.terminate()
                        monitor.wait(timeout=10)
                    subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)
                    if server and server.poll() is None:
                        server.terminate()
                        server.wait(timeout=10)
