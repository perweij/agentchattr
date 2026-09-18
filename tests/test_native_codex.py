"""Native transport contracts: durable identity, uncertain delivery and wire I/O."""

import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from websockets.sync.server import unix_serve

from agentchattr import cli, wrapper
from agentchattr.agents import AgentTrigger
from agentchattr.codex_transport import CodexRpc, DeliveryEngine, RpcError
from agentchattr.config_loader import load_config
from agentchattr.native_store import NativeStore
from agentchattr.registry import RuntimeRegistry
from agentchattr.wrapper_codex import parse_options, require_empty_legacy_queue


class Backend:
    """Acknowledgements and persisted history are independent, as on the wire."""
    def __init__(self):
        self.queued, self.turns, self.calls = [], [], []
        self.lose_receipt = False

    def pages(self, method, thread_id, **options):
        return iter(self.queued if method == "thread/queue/list" else self.turns)

    def call(self, method, params):
        self.calls.append((method, params))
        item = dict(id="submission-1", clientUserMessageId=params["clientUserMessageId"], input=params["input"])
        self.queued.append(item)
        if self.lose_receipt:
            raise TimeoutError("Receipt lost after acceptance")
        return {"queuedSubmission": item}

    def finish(self, status="completed", duplicate=False):
        item = self.queued.pop(0)
        self.turns.append({"id": "turn-1", "status": status, "itemsView": "full", "items": [
            {"type": "userMessage", "clientId": item["clientUserMessageId"], "content": item["input"]}]})
        if duplicate:
            self.turns.append(dict(self.turns[-1], id="turn-2"))


class NativeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = NativeStore(self.root)
        self.runtime = self.store.create_runtime("codex", {"identity_id": "identity-1", "name": "codex", "token": "secret"},
                                                thread_id="thread-1")
        self.backend = Backend()
        self.engine = DeliveryEngine(self.store, self.runtime, self.backend, wrapper.build_trigger_prompt)

    def enqueue(self, **payload):
        return self.store.enqueue("identity-1", {"channel": "general", **payload})

    def row(self, event):
        return next(r for r in self.store.deliveries() if r["id"] == event)

    def test_long_prompt_saved_before_wire_and_completion_is_not_acceptance(self):
        text = "long message\n" * 10000
        event = self.enqueue(prompt=text)
        original = self.backend.call

        def call(method, params):
            self.assertEqual(self.row(event)["state"], "submitting")
            self.assertEqual(self.row(event)["prompt"], text.strip())
            return original(method, params)

        self.backend.call = call
        self.engine.step()
        self.assertEqual(self.row(event)["state"], "accepted")
        self.backend.finish()
        self.engine.step()
        self.assertEqual(self.row(event)["state"], "completed")
        self.assertEqual(self.row(event)["turn_id"], "turn-1")

    def test_missing_ack_reconciles_without_duplicate_and_preserves_order(self):
        first = self.enqueue(channel="one")
        second = self.enqueue(channel="two", job_id=7)
        self.backend.lose_receipt = True
        with self.assertRaises(TimeoutError):
            self.engine.step()
        self.assertEqual(self.row(first)["state"], "uncertain")
        # Reopen the on-disk database, as a resumed wrapper would.
        self.engine.store = NativeStore(self.root)
        self.engine.reconcile(recovering=True)
        self.assertEqual(self.row(first)["state"], "accepted")
        self.engine.step()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.row(second)["state"], "pending")
        self.backend.finish()
        self.backend.lose_receipt = False
        self.engine.step()
        self.assertEqual(len(self.backend.calls), 2)
        self.assertIn("job_id=7", self.row(second)["prompt"])

    def test_crash_before_or_after_send_is_uncertain_if_no_receipt(self):
        event = self.enqueue()
        self.store.transition(event, "submitting", prompt="saved", attempt_id="attempt")
        self.enqueue(channel="later")
        self.engine.reconcile(recovering=True)
        self.engine.step()
        self.assertEqual(self.row(event)["state"], "uncertain")
        self.assertFalse(self.backend.calls)

    def test_completed_history_reconciles_missing_queue_receipt(self):
        event = self.enqueue()
        self.backend.lose_receipt = True
        with self.assertRaises(TimeoutError):
            self.engine.step()
        self.backend.finish()
        self.engine.reconcile(recovering=True)
        self.assertEqual(self.row(event)["state"], "completed")
        self.assertEqual(len(self.backend.calls), 1)

    def test_empty_thread_history_does_not_invalidate_queue_receipt(self):
        event = self.enqueue()
        self.engine.step()
        original = self.backend.pages

        def pages(method, thread_id, **options):
            if method == "thread/turns/list":
                raise RpcError(method, {"code": -32600, "message": "thread/turns/list is unavailable before first user message"})
            return original(method, thread_id, **options)

        self.backend.pages = pages
        self.engine.reconcile(recovering=True)
        self.assertEqual(self.row(event)["state"], "accepted")
        self.assertEqual(len(self.backend.calls), 1)

    def test_duplicates_require_operator_intervention(self):
        event = self.enqueue()
        self.engine.step()
        self.backend.finish(duplicate=True)
        self.engine.step()
        self.assertEqual(self.row(event)["state"], "uncertain")
        self.assertEqual(len(self.backend.calls), 1)

    def test_human_turn_does_not_complete_native_delivery(self):
        event = self.enqueue()
        self.engine.step()
        self.backend.turns.append({"id": "human-turn", "status": "completed", "items": [
            {"type": "userMessage", "clientId": "human-client-id"}]})
        self.engine.step()
        self.assertEqual(self.row(event)["state"], "accepted")

    def test_failed_turn_blocks_until_explicit_resolution(self):
        event = self.enqueue()
        self.engine.step()
        self.backend.finish(status="interrupted")
        self.engine.step()
        self.assertEqual(self.row(event)["state"], "failed")
        self.store.resolve(event, "discard")
        self.assertEqual(self.row(event)["state"], "discarded")

    def test_retry_uses_new_attempt_and_retains_audit_history(self):
        event = self.enqueue()
        self.engine.step()
        old_attempt = self.row(event)["attempt_id"]
        self.backend.queued.clear()
        self.engine.reconcile(recovering=True)
        self.store.resolve(event, "retry")
        self.engine.step()
        self.assertNotEqual(old_attempt, self.row(event)["attempt_id"])
        with self.store.connect() as db:
            history = db.execute("SELECT attempt_id FROM delivery_history WHERE event_id=?", (event,)).fetchall()
        self.assertIn(old_attempt, [r["attempt_id"] for r in history])

    def test_runtime_lock_excludes_recovery_and_second_owner(self):
        event = self.enqueue()
        with self.store.lock(self.runtime["id"]):
            with self.assertRaisesRegex(ValueError, "already owned"):
                with self.store.lock(self.runtime["id"]):
                    pass
            with self.assertRaisesRegex(ValueError, "already owned"):
                self.store.resolve(event, "retry")
        self.store.resolve(event, "retry")
        self.assertNotIn("secret", json.dumps(self.store.listing()))

    def test_async_and_sync_native_routing_survive_rename_and_isolate_instances(self):
        registry = RuntimeRegistry(str(self.root))
        registry.seed({"codex": {"transport": "codex_native"}, "claude": {}})
        first = registry.register("codex")
        triggers = AgentTrigger(registry, str(self.root))
        asyncio.run(triggers.trigger(first["name"], "hello", channel="a"))
        registry.rename(first["name"], "worker")
        triggers.trigger_sync("worker", "again", channel="b", job_id=3)
        second = registry.register("codex")
        triggers.trigger_sync(second["name"], "second")
        rows = self.store.deliveries(first["identity_id"])
        self.assertEqual([json.loads(r["payload"])["channel"] for r in rows], ["a", "b"])
        self.assertEqual(len(self.store.deliveries(second["identity_id"])), 1)
        legacy = registry.register("claude")
        triggers.trigger_sync(legacy["name"], "legacy")
        self.assertTrue((self.root / "claude_queue.jsonl").exists())
        self.assertFalse((self.root / "worker_queue.jsonl").exists())

    def test_legacy_queue_is_never_truncated(self):
        path = self.root / "codex_queue.jsonl"
        path.write_text('{"channel":"old"}\n')
        with self.assertRaisesRegex(ValueError, "Drain"):
            require_empty_legacy_queue(self.root, "codex")
        self.assertEqual(path.read_text(), '{"channel":"old"}\n')

    def test_native_deregister_retains_identity_for_explicit_resume_only(self):
        from agentchattr import app, mcp_bridge
        registry = RuntimeRegistry(str(self.root))
        registry.seed({"codex": {"transport": "codex_native"}, "claude": {}})
        for base, reclaimable in (("codex", True), ("claude", False)):
            inst = registry.register(base)

            class Request:
                headers = {"authorization": "Bearer " + inst["token"]}

            with patch.object(app, "registry", registry), patch.object(mcp_bridge, "registry", registry):
                response = asyncio.run(app.deregister_agent(inst["name"], Request()))
            self.assertEqual(response.status_code, 200)
            recovered = registry.resolve_token(inst["token"])
            self.assertEqual(recovered is not None, reclaimable)
            if recovered:
                self.assertEqual(recovered["identity_id"], inst["identity_id"])


class NativeOptionsTests(unittest.TestCase):
    def test_backend_overrides_and_terminal_option_are_separate(self):
        opts = parse_options(["-m", "model", "-s", "read-only", "-a", "on-request", "--no-alt-screen",
                              "-c", "model_reasoning_effort=high"])
        self.assertTrue(opts["no_alt_screen"])
        self.assertIn('sandbox_mode="read-only"', opts["overrides"])
        self.assertIn('approval_policy="on-request"', opts["overrides"])

    def test_unsupported_options_and_managed_mcp_override_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_options(["--dangerously-bypass-approvals-and-sandbox"])
        with self.assertRaises(ValueError):
            parse_options(["-c", "mcp_servers.agentchattr.url=other"])

    def test_config_validation_and_native_cli_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[agents.worker]\ncommand="codex"\ntransport="codex_native"\n')
            with patch("agentchattr.wrapper_codex.main") as main:
                cli.main(["agent", "worker", "--config", str(path), "--resume-runtime", "abc"])
                self.assertEqual(main.call_args.args[1].resume_runtime, "abc")
            path.write_text('[agents.worker]\ncommand="claude"\ntransport="codex_native"\n')
            with self.assertRaisesRegex(ValueError, "requires a Codex"):
                load_config(config_path=path)


class NativeProtocolTests(unittest.TestCase):
    def test_private_unix_rpc_pagination_activity_and_no_automatic_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "rpc.sock")
            received, extensions, delivered = [], [], threading.Event()

            def handle(ws):
                extensions.append(ws.request.headers.get("Sec-WebSocket-Extensions"))
                for raw in ws:
                    msg = json.loads(raw)
                    received.append(msg)
                    if "id" not in msg:
                        continue
                    if msg.get("method") == "initialize":
                        result = {}
                    elif msg.get("method") == "thread/queue/list":
                        if msg["params"].get("cursor"):
                            result = {"data": [{"id": "second"}], "nextCursor": None}
                        else:
                            result = {"data": [{"id": "first"}], "nextCursor": "page-2"}
                        ws.send(json.dumps({"id": "approval-1", "method": "item/commandExecution/requestApproval",
                                            "params": {"threadId": "thread"}}))
                        ws.send(json.dumps({"method": "turn/started", "params": {"threadId": "thread"}}))
                    else:
                        ws.send(json.dumps({"id": msg["id"], "error": {"code": -32601, "message": "private provider data"}}))
                        continue
                    ws.send(json.dumps({"id": msg["id"], "result": result}))
                delivered.set()

            with unix_serve(handle, path) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                rpc = CodexRpc(path)
                try:
                    rpc.thread_id = "thread"
                    rows = list(rpc.pages("thread/queue/list", "thread"))
                    self.assertEqual([r["id"] for r in rows], ["first", "second"])
                    self.assertTrue(rpc.active)
                    with self.assertRaises(RpcError) as error:
                        rpc.call("missing", {})
                    self.assertNotIn("private provider data", str(error.exception))
                finally:
                    rpc.close()
                    server.shutdown()
                    thread.join(timeout=3)
                self.assertTrue(delivered.wait(2))
                self.assertFalse(any(m.get("id") == "approval-1" for m in received))
                self.assertEqual(extensions, [None])


if __name__ == "__main__":
    unittest.main()
