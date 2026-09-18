"""Deterministic Codex stand-in for process ownership tests; never calls a model."""

import json
import os
from pathlib import Path
import sys
import threading
import time

from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

root = Path(os.environ["NATIVE_CODEX_FIXTURE"])
args = sys.argv[1:]

if "generate-json-schema" in args:
    target = Path(args[args.index("--out") + 1])
    target.mkdir(exist_ok=True, parents=True)
    for name in ("ThreadQueueAddParams", "ThreadQueueListParams", "ThreadTurnsListParams", "ThreadLoadedListParams"):
        (target / (name + ".json")).write_text(json.dumps({"properties": {"clientUserMessageId": {}}}))
    (target / "ThreadReadResponse.json").write_text(json.dumps({
        "definitions": {"ThreadItem": {"oneOf": [{"properties": {"clientId": {}}}]}}}))
elif args[0] == "app-server":
    (root / "backend-argv.json").write_text(json.dumps(args))
    (root / "backend-pid").write_text(str(os.getpid()))
    if os.environ.get("NATIVE_CODEX_FAIL"):
        sys.exit(7)
    socket_path = args[args.index("--listen") + 1].removeprefix("unix://")
    state_file = root / "history.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"turns": []}
    loaded = set()
    starts = [0]
    lock = threading.Lock()

    def handler(ws):
        for raw in ws:
            request = json.loads(raw)
            if "id" not in request:
                continue
            method, params = request["method"], request.get("params", {})
            with lock:
                if method == "initialize":
                    result = {}
                elif method in ("thread/start", "thread/resume", "thread/read"):
                    if method == "thread/resume":
                        if not state["turns"]:
                            ws.send(json.dumps({"id": request["id"], "error": {
                                "code": -32600, "message": "no rollout found for thread id test-thread"}}))
                            continue
                        with (root / "resume-requests").open("a") as stream:
                            stream.write("resume\n")
                    if method == "thread/start" and not params.get("ephemeral"):
                        starts[0] += 1
                    thread_id = ("probe-thread" if params.get("ephemeral") else
                                 params.get("threadId") if method != "thread/start" else
                                 "test-thread" if starts[0] == 1 else f"test-thread-{starts[0]}")
                    if method in ("thread/start", "thread/resume"):
                        loaded.add(thread_id)
                    result = {"thread": {"id": thread_id, "cwd": str(root), "status": {"type": "idle"},
                                         "preview": "test" if state["turns"] else "", **state},
                              "model": "fixture", "approvalPolicy": os.environ.get("NATIVE_CODEX_POLICY", "on-request"),
                              "sandbox": {"type": "readOnly"}}
                elif method == "thread/loaded/list":
                    result = {"data": sorted(loaded), "nextCursor": None}
                elif method == "thread/queue/list":
                    result = {"data": [], "nextCursor": None}
                elif method == "thread/turns/list":
                    result = {"data": state["turns"], "nextCursor": None}
                elif method == "thread/queue/add":
                    turn_id = f"turn-{len(state['turns']) + 1}"
                    state["turns"].append({"id": turn_id, "status": "completed", "itemsView": "full", "items": [
                        {"type": "userMessage", "clientId": params["clientUserMessageId"], "content": params["input"]}]})
                    state_file.write_text(json.dumps(state))
                    result = {"queuedSubmission": {"id": "submission-" + turn_id,
                                                   "clientUserMessageId": params["clientUserMessageId"]}}
                else:
                    ws.send(json.dumps({"id": request["id"], "error": {"code": -32601}}))
                    continue
                ws.send(json.dumps({"id": request["id"], "result": result}))

    with unix_serve(handler, socket_path) as server:
        server.serve_forever()
elif "--remote" in args:
    (root / "tui-argv.json").write_text(json.dumps(args))
    socket_path = args[args.index("--remote") + 1].removeprefix("unix://")
    with unix_connect(socket_path, compression=None) as ws:
        ws.send(json.dumps({"id": 1, "method": "initialize", "params": {}}))
        ws.recv(timeout=2)
        method = "thread/resume" if args[0] == "resume" else "thread/start"
        ws.send(json.dumps({"id": 2, "method": method, "params": {"threadId": "test-thread"}}))
        response = json.loads(ws.recv(timeout=2))
        if "error" in response:
            sys.exit(1)
        if os.environ.get("NATIVE_CODEX_EXIT_SECOND") and response["result"]["thread"]["id"] == "test-thread-2":
            time.sleep(0.5)
            sys.exit(9)
        # Allow delivery + reconciliation to finish before the terminal exits.
        time.sleep(3)
