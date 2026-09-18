"""Codex app-server protocol and conservative native delivery reconciliation."""

import json
import logging
import queue
import threading
import uuid

from websockets.sync.client import unix_connect

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    def __init__(self, method, error):
        self.code = error.get("code")
        self.missing_thread = "no rollout found" in error.get("message", "").lower()
        self.empty_thread = "unavailable before first user message" in error.get("message", "").lower()
        # Provider error strings can contain configuration values. Keep them out
        # of the shared terminal/log; identifiers and error codes suffice here.
        super().__init__(f"Codex {method} rejected the request (code {self.code})")


class CodexRpc:
    def __init__(self, socket_path, timeout=15):
        self.timeout = timeout
        self.connection = unix_connect(str(socket_path), open_timeout=timeout,
                                       close_timeout=2, max_size=32 * 1024 * 1024,
                                       compression=None)
        self.socket = self.connection.__enter__()
        self.pending = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.active = False
        self.thread_id = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "agentchattr", "version": "0.5.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.socket.send(json.dumps({"method": "initialized"}))
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for raw in self.socket:
                message = json.loads(raw)
                if "method" in message:
                    params = message.get("params", {})
                    if params.get("threadId") == self.thread_id:
                        if message["method"] == "turn/started":
                            self.active = True
                        elif message["method"] == "turn/completed":
                            self.active = False
                        elif message["method"] == "thread/status/changed":
                            self.active = params.get("status", {}).get("type") == "active"
                    # Requests (including approvals) belong to the interactive
                    # client. This observer never accepts, denies, or answers them.
                    continue
                with self.lock:
                    waiter = self.pending.get(message.get("id"))
                if waiter:
                    waiter.put(message)
        except Exception:
            pass
        finally:
            self.closed.set()
            with self.lock:
                for waiter in self.pending.values():
                    waiter.put(None)

    def call(self, method, params):
        request_id, waiter = uuid.uuid4().hex, queue.Queue()
        with self.lock:
            if self.closed.is_set():
                raise ConnectionError("Codex control connection closed")
            self.pending[request_id] = waiter
        try:
            self.socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
            try:
                response = waiter.get(timeout=self.timeout)
            except queue.Empty:
                raise TimeoutError(f"Codex {method} response timed out") from None
            if response is None:
                raise ConnectionError("Codex control connection closed")
            if "error" in response:
                raise RpcError(method, response["error"])
            return response["result"]
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        self.socket.close()
        self.reader.join(timeout=3)
        self.connection.__exit__(None, None, None)

    def pages(self, method, thread_id, **options):
        cursor, seen = None, set()
        while True:
            params = dict(options, limit=100)
            if thread_id is not None:
                params["threadId"] = thread_id
            if cursor:
                params["cursor"] = cursor
            result = self.call(method, params)
            yield from result["data"]
            cursor = result.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise RuntimeError("Codex returned a repeated pagination cursor")
            seen.add(cursor)


class DeliveryEngine:
    """Single runtime owner; never retry a submission implicitly.

Only one application notification is outstanding at a time. Human turns may
still run concurrently; correlation uses client IDs, never the current turn or
matching prompt text. A completion includes no claim about the model's answer.
"""

    def __init__(self, store, runtime, rpc, prepare_prompt):
        self.store, self.runtime, self.rpc = store, runtime, rpc
        self.prepare_prompt = prepare_prompt

    def reconcile(self, recovering=False):
        rows = [r for r in self.store.deliveries(self.runtime["identity_id"], open_only=True)
                if r["state"] in ("submitting", "accepted", "uncertain")]
        if not rows:
            return
        thread_id = self.runtime["thread_id"]
        pending = list(self.rpc.pages("thread/queue/list", thread_id))
        try:
            turns = list(self.rpc.pages("thread/turns/list", thread_id, itemsView="full"))
        except RpcError as exc:
            # A newly created thread has no on-disk history until its first turn.
            # A pending queue receipt is still conclusive; absence is not.
            if not (exc.missing_thread or exc.empty_thread):
                raise
            turns = []
        for row in rows:
            attempt = row["attempt_id"]
            queued = [q for q in pending if attempt and q.get("clientUserMessageId") == attempt]
            matches = []
            for turn in turns:
                if turn.get("itemsView", "full") != "full":
                    raise RuntimeError("Codex did not return full turn history; cannot reconcile")
                for item in turn.get("items", []):
                    if item.get("type") == "userMessage" and attempt and item.get("clientId") == attempt:
                        matches.append(turn)
            if len(queued) > 1 or len(matches) > 1:
                self._update(row, "uncertain", detail="Multiple backend submissions match this attempt")
            elif matches:
                turn = matches[0]
                status = turn["status"]
                state = "completed" if status == "completed" else (
                    "failed" if status in ("failed", "interrupted") else "accepted")
                self._update(row, state, turn_id=turn["id"], detail=f"Backend turn {status}")
            elif queued:
                self._update(row, "accepted", submission_id=queued[0]["id"], detail="Queued by backend")
            else:
                self._update(row, "uncertain", detail="No conclusive backend receipt; inspect before retrying")

    def _update(self, row, state, **fields):
        if row["state"] != state or any(row.get(k) != v for k, v in fields.items()):
            self.store.transition(row["id"], state, **fields)

    def step(self):
        self.reconcile()
        rows = self.store.deliveries(self.runtime["identity_id"], open_only=True)
        # Uncertainty pauses the whole runtime, even if an earlier notification
        # was explicitly returned to pending by an operator.
        if any(r["state"] in ("uncertain", "failed", "submitting", "accepted") for r in rows):
            return
        if not rows:
            return
        row = rows[0]
        prompt = row["prompt"] or self.prepare_prompt(json.loads(row["payload"]))
        attempt = uuid.uuid4().hex
        self.store.transition(row["id"], "submitting", prompt=prompt, attempt_id=attempt,
                              submission_id=None, turn_id=None, detail="Submission started")
        try:
            result = self.rpc.call("thread/queue/add", {
                "threadId": self.runtime["thread_id"], "clientUserMessageId": attempt,
                "input": [{"type": "text", "text": prompt}],
            })
            submission = result["queuedSubmission"]
            if submission.get("clientUserMessageId") != attempt:
                raise RuntimeError("Codex queue receipt did not match the submitted attempt")
            self.store.transition(row["id"], "accepted", submission_id=submission["id"],
                                  detail="Backend acknowledged queue acceptance")
        except Exception:
            self.store.transition(row["id"], "uncertain", detail="Submission outcome unknown; reconciliation required")
            raise
