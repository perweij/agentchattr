"""Exercise the installed package from an unrelated directory with fake agents.

Also run this file using a clean wheel environment to verify packaged resources:
    /path/to/clean-env/bin/python tests/test_server_smoke.py
No agent CLI or model endpoint is invoked.
"""
import asyncio
import json
import io
import zipfile
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp import ClientSession
from agentchattr.jobs import JobStore
from agentchattr.rules import RuleStore
from agentchattr.summaries import SummaryStore
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from websockets.sync.client import connect


class InstalledServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Reserve three distinct free ports until just before server startup.
        with ExitStack() as stack:
            sockets = [stack.enter_context(socket.socket()) for _ in range(3)]
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            self.port, self.http_port, self.sse_port = [s.getsockname()[1] for s in sockets]
        self.config = self.root / "project" / "config.toml"
        self.config.parent.mkdir()
        self.config.write_text(
            f'[server]\nport = {self.port}\ndata_dir = "state"\n'
            f'[mcp]\nhttp_port = {self.http_port}\nsse_port = {self.sse_port}\n'
            '[agents.fake]\ncommand = "never-executed"\n'
            '[images]\nupload_dir = "images"\n')
        state = self.config.parent / "state"
        state.mkdir()
        # Existing JSONL records retain their IDs and content after packaging.
        self.legacy = {"id": 41, "sender": "user", "text": "existing history",
                       "type": "chat", "channel": "general", "time": "12:00:00"}
        (state / "agentchattr_log.jsonl").write_text(json.dumps(self.legacy) + "\n")
        JobStore(str(state / "jobs.json")).create("Existing job", "task", "general", "user")
        RuleStore(str(state / "rules.json")).propose("Existing rule", "user")
        SummaryStore(str(state / "summaries.json")).write("general", "Existing summary", "user")
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = (self.root / "server.log").open("w+")
        self.addCleanup(self.log.close)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTCHATTR_")}
        env["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "agentchattr", "serve", "--config", str(self.config)],
            cwd=self.root, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail("Server exited during startup:\n" + self.log.read())
            try:
                self.html = self.request("/").decode()
                break
            except (URLError, TimeoutError):
                time.sleep(0.1)
        else:
            self.fail("Server did not become ready")
        self.token = re.search(r'window.__SESSION_TOKEN__="([a-f0-9]+)"', self.html).group(1)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, path, body=None, headers=None):
        request = Request(self.base + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(request, timeout=3) as response:
            return response.read()

    def test_resources_history_and_message_roundtrip(self):
        headers = {"X-Session-Token": self.token}
        assets = set(re.findall(r'(?:src|href)="(/static/[^"?]+)', self.html))
        self.assertGreater(len(assets), 5)
        for asset in assets:
            self.assertTrue(self.request(asset), asset)
        templates = json.loads(self.request("/api/sessions/templates", headers=headers))
        self.assertEqual({t["id"] for t in templates}, {"code-review", "debate", "design-critique", "planning"})
        self.assertIn(self.legacy, json.loads(self.request("/api/messages", headers=headers)))
        self.assertIn("Existing job", self.request("/api/jobs", headers=headers).decode())
        self.assertIn("Existing rule", self.request("/api/rules", headers=headers).decode())
        with zipfile.ZipFile(io.BytesIO(self.request("/api/export", headers=headers))) as archive:
            self.assertEqual(set(archive.namelist()), {
                "manifest.json", "messages.jsonl", "jobs.json", "rules.json", "summaries.json"})
            self.assertIn("Existing summary", archive.read("summaries.json").decode())
        with self.assertRaises(HTTPError) as forbidden:
            self.request("/api/messages")
        self.assertEqual(forbidden.exception.code, 403)

        registration = json.loads(self.request("/api/register", {"base": "fake"}))
        name, token = registration["name"], registration["token"]
        with connect(f"ws://127.0.0.1:{self.port}/ws?token={self.token}", open_timeout=5) as ws:
            self.assertEqual(json.loads(ws.recv(timeout=5))["type"], "settings")
            ws.send(json.dumps({"type": "message", "text": f"@{name} smoke request", "channel": "general"}))
            queue = self.config.parent / "state" / f"{name}_queue.jsonl"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (queue.exists() and queue.stat().st_size):
                time.sleep(0.05)
            self.assertTrue(queue.exists())
            self.assertIn("smoke request", queue.read_text())
            asyncio.run(self.mcp_roundtrip(name, token))
            deadline = time.monotonic() + 5
            received = False
            while time.monotonic() < deadline:
                event = ws.recv(timeout=5)
                if "smoke response" in event:
                    received = True
                    break
            self.assertTrue(received, "Agent response must be broadcast to the browser")
        messages = json.loads(self.request("/api/messages", headers=headers))
        response = next(m for m in messages if m["text"] == "smoke response")
        self.assertEqual(response["sender"], name)
        self.assertGreater(response["id"], 41)
        self.request(f"/api/deregister/{name}", {}, {"Authorization": f"Bearer {token}"})
        self.stop()
        saved = (self.config.parent / "state" / "agentchattr_log.jsonl").read_text()
        self.assertIn("existing history", saved)
        self.assertIn("smoke response", saved)
        self.assertFalse((self.root / "data").exists(), "State belongs beside config, not the invocation directory")

    async def mcp_roundtrip(self, name, token):
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(f"http://127.0.0.1:{self.http_port}/mcp", headers=headers, timeout=5) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool("chat_read", {"sender": name, "channel": "general"})
                self.assertIn("smoke request", str(result))
                result = await client.call_tool("chat_send", {
                    "sender": "spoofed-name", "message": "smoke response", "choices": [], "channel": "general"})
                self.assertFalse(result.isError)
        async with sse_client(f"http://127.0.0.1:{self.sse_port}/sse", headers=headers, timeout=5) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                self.assertIn("chat_read", {t.name for t in tools.tools})


if __name__ == "__main__":
    unittest.main()
