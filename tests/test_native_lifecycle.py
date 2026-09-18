import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentchattr import wrapper_codex
from agentchattr.native_store import NativeStore
from agentchattr.registry import RuntimeRegistry


class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = NativeStore(self.root)
        registry = self.registry = RuntimeRegistry(str(self.root))
        registry.seed({"codex": {"transport": "codex_native"}})
        store = self.store
        self.registrations = []
        registrations = self.registrations
        self.enqueue_on_register = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.reply({} if self.path == "/api/roles" else {"epoch": 1, "rules": []})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/api/register":
                    inst = registry.register("codex")
                    registrations.append(inst)
                    if owner.enqueue_on_register:
                        store.enqueue(inst["identity_id"], {"channel": "testing", "job_id": 4})
                    self.reply(inst)
                else:
                    inst = registry.resolve_token(self.headers.get("Authorization", "").removeprefix("Bearer "))
                    if self.path.startswith("/api/deregister/"):
                        registry.deregister(inst["name"], reclaimable=True)
                    self.reply({"name": inst["name"], "ok": True})

            def reply(self, result):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.command = self.root / "codex"
        fixture = Path(__file__).parent / "fixtures" / "native_codex_cli.py"
        import sys
        self.command.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(fixture)!r}, run_name='__main__')\n")
        self.command.chmod(0o700)
        self.config = {"server": {"data_dir": str(self.root), "port": self.http.server_port},
                       "agents": {"codex": {"command": str(self.command), "cwd": str(self.root), "transport": "codex_native"}}}
        self.args = SimpleNamespace(agent="codex", label=None, resume_runtime=None, no_restart=True)
        self.env = patch.dict(os.environ, {"NATIVE_CODEX_FIXTURE": str(self.root), "CODEX_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_backend_stopped(self):
        pid = int((self.root / "backend-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_owned_backend_terminal_delivery_shutdown_and_same_runtime_resume(self):
        with contextlib.redirect_stdout(io.StringIO()):
            wrapper_codex.main(self.config, self.args, ["-s", "read-only", "-a", "on-request", "--no-alt-screen"])
        self.assert_backend_stopped()
        first_tui_args = json.loads((self.root / "tui-argv.json").read_text())
        self.assertNotEqual(first_tui_args[0], "resume")
        self.assertIn('sandbox_mode="read-only"', first_tui_args)
        self.assertIn('approval_policy="on-request"', first_tui_args)
        listing = self.store.listing()
        self.assertEqual(listing[0]["state"], "completed")
        runtime_id = listing[0]["runtime_id"]
        self.args.resume_runtime = runtime_id
        self.store.enqueue(self.registrations[0]["identity_id"], {"channel": "after-restart"})
        with contextlib.redirect_stdout(io.StringIO()):
            wrapper_codex.main(self.config, self.args, [])
        self.assert_backend_stopped()
        self.assertEqual(len(self.registrations), 1)
        self.assertEqual([r["state"] for r in self.store.listing()], ["completed", "completed"])
        backend_args = json.loads((self.root / "backend-argv.json").read_text())
        tui_args = json.loads((self.root / "tui-argv.json").read_text())
        self.assertIn('sandbox_mode="read-only"', backend_args)
        self.assertIn('approval_policy="on-request"', backend_args)
        self.assertNotIn("-s", tui_args)
        self.assertNotIn("-c", tui_args)
        self.assertIn("test-thread", tui_args)
        self.assertIn("--no-alt-screen", tui_args)
        self.assertIn("/mcp", " ".join(backend_args))
        self.assertNotIn(self.registrations[0]["token"], " ".join(backend_args + tui_args))
        with self.store.lock(runtime_id):
            pass

    def test_empty_startup_and_resume_create_no_dummy_turn(self):
        self.enqueue_on_register = False
        with contextlib.redirect_stdout(io.StringIO()) as output:
            wrapper_codex.main(self.config, self.args, [])
        self.assert_backend_stopped()
        runtime_id = output.getvalue().split("Native runtime ID: ")[1].split()[0]
        self.assertEqual(self.store.runtime(runtime_id)["thread_id"], "test-thread")
        self.assertEqual(self.store.listing(), [])
        self.assertFalse((self.root / "history.json").exists())
        self.assertNotEqual(json.loads((self.root / "tui-argv.json").read_text())[0], "resume")
        self.args.resume_runtime = runtime_id
        with contextlib.redirect_stdout(io.StringIO()):
            wrapper_codex.main(self.config, self.args, [])
        self.assert_backend_stopped()
        self.assertEqual(len(self.registrations), 1)
        self.assertEqual(self.store.listing(), [])
        self.assertFalse((self.root / "history.json").exists())

    def test_empty_terminal_reopens_before_any_notification(self):
        self.enqueue_on_register = False
        self.args.no_restart = False
        with patch.dict(os.environ, {"NATIVE_CODEX_EXIT_SECOND": "1"}), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, "terminal exited with an error"):
                wrapper_codex.main(self.config, self.args, [])
        runtime_id = output.getvalue().split("Native runtime ID: ")[1].split()[0]
        self.assertEqual(self.store.runtime(runtime_id)["thread_id"], "test-thread-2")
        self.assertEqual(self.store.listing(), [])
        self.assertFalse((self.root / "history.json").exists())
        self.assert_backend_stopped()

    def test_backend_startup_failure_retains_notification_and_releases_owner(self):
        with patch.dict(os.environ, {"NATIVE_CODEX_FAIL": "1"}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "backend exited"):
                wrapper_codex.main(self.config, self.args, [])
        self.assert_backend_stopped()
        row = self.store.listing()[0]
        self.assertEqual(row["state"], "pending")
        with self.store.lock(row["runtime_id"]):
            pass

    def test_permission_drift_is_rejected_before_loading_saved_conversation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            wrapper_codex.main(self.config, self.args, [])
        self.args.resume_runtime = self.store.listing()[0]["runtime_id"]
        (self.root / "resume-requests").touch(exist_ok=True)
        previous = (self.root / "resume-requests").read_text()
        with patch.dict(os.environ, {"NATIVE_CODEX_POLICY": "never"}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "settings changed"):
                wrapper_codex.main(self.config, self.args, [])
        self.assertEqual((self.root / "resume-requests").read_text(), previous)
        self.assert_backend_stopped()


class BackendParentDeathTests(unittest.TestCase):
    def test_wrapper_crash_terminates_its_owned_backend(self):
        # The helper's parent is a disposable process, never the test runner.
        script = """
import os, subprocess, sys, time
p = subprocess.Popen([sys.executable, '-m', 'agentchattr.native_backend', str(os.getpid()),
                      sys.executable, '-c', 'import time; print("READY", flush=True); time.sleep(60)'],
                     stdout=subprocess.PIPE, text=True)
assert p.stdout.readline().strip() == 'READY'
print(p.pid, flush=True)
time.sleep(60)
"""
        parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        child_pid = None
        try:
            import select
            self.assertTrue(select.select([parent.stdout], [], [], 5)[0])
            child_pid = int(parent.stdout.readline())
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = Path(f"/proc/{child_pid}/stat")
                if not status.exists() or status.read_text().split()[2] == "Z":
                    break
                time.sleep(0.05)
            else:
                self.fail("Owned backend survived the wrapper's SIGKILL")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            parent.stdout.close()
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
