"""Adoption ownership, identity, project routing and authenticated chat contracts."""

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentchattr import adoption, app, cli, mcp_bridge, wrapper_unix
from agentchattr.agents import AgentTrigger
from agentchattr.native_store import NativeStore
from agentchattr.projects import project_context
from agentchattr.registry import RuntimeRegistry
from agentchattr.router import Router
from agentchattr.store import MessageStore


class Request:
    def __init__(self, body, token=""):
        self.body = body
        self.headers = {"authorization": "Bearer " + token}

    async def json(self):
        return self.body


class ProjectTests(unittest.TestCase):
    def test_project_subfolders_symlinks_same_basename_and_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = root / "a" / "project", root / "b" / "project"
            for path in (one, two):
                path.mkdir(parents=True)
                subprocess.run(["git", "init", "-q", str(path)], check=True)
            sub = one / "sub"
            sub.mkdir()
            link = root / "linked"
            link.symlink_to(one)
            first = project_context(one)
            self.assertEqual(first["project_channel"], project_context(sub)["project_channel"])
            self.assertEqual(first["project_channel"], project_context(link)["project_channel"])
            self.assertNotEqual(first["project_channel"], project_context(two)["project_channel"])
            subprocess.run(["git", "-C", str(one), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "test"], check=True)
            worktree = root / "worktree"
            subprocess.run(["git", "-C", str(one), "worktree", "add", "--detach", "-q", str(worktree)], check=True)
            self.assertNotEqual(first["project_channel"], project_context(worktree)["project_channel"])


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.registry = RuntimeRegistry(str(self.root))
        self.registry.seed({"codex": {"transport": "codex_native"}, "claude": {}})
        self.store = MessageStore(str(self.root / "messages.jsonl"))
        self.native = NativeStore(self.root)
        self.router = Router(["codex", "claude"], default_mention="all", membership_checker=self.registry.in_channel)
        self.agents = AgentTrigger(self.registry, str(self.root))
        self.settings = {"channels": ["general"]}
        for module in (app, mcp_bridge):
            for key, value in {"registry": self.registry, "store": self.store, "router": self.router,
                               "agents": self.agents, "room_settings": self.settings,
                               "jobs": None, "config": {"server": {"data_dir": str(self.root)}}}.items():
                self.stack.enter_context(patch.object(module, key, value))
        self.stack.enter_context(patch.object(app, "_event_loop", None))
        self.stack.enter_context(patch.object(app, "_save_settings"))
        self.target = {"pane": "%7", "socket": "/tmp/test-tmux", "pid": 17, "start": "1234",
                       "pane_pid": 11, "base": "codex", "cwd": str(self.root), "key": "target-key"}
        self.stack.enter_context(patch.object(adoption, "inspect_pane", return_value=self.target))

    def register(self, target=None, channels=None, notify="manual"):
        target = target or self.target
        response = asyncio.run(app.register_agent(Request({"base": target["base"], "adoption": {
            "target": target, "channels": channels, "notify": notify}})))
        self.assertEqual(response.status_code, 200, response.body)
        return json.loads(response.body)

    def chat(self, inst, action="read", **body):
        return asyncio.run(app.adopted_chat(Request({"action": action, **body}, inst["token"])))

    def test_project_membership_persists_and_same_pane_cannot_register_twice(self):
        inst = self.register()
        channel = inst["channels"][0]
        self.assertNotEqual(channel, "general")
        self.assertIn(channel, self.settings["channels"])
        duplicate = asyncio.run(app.register_agent(Request({"base": "codex", "adoption": {
            "target": self.target, "channels": None, "notify": "manual"}})))
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(len(self.registry.get_all()), 1)
        restored = RuntimeRegistry(str(self.root))
        restored.seed({"codex": {}})
        reclaimed = restored.resolve_token(inst["token"])
        self.assertEqual(reclaimed["channels"], [channel])
        self.assertEqual(reclaimed["context"]["project_root"], str(self.root))

    def test_routing_filters_family_mentions_all_defaults_and_direct_triggers(self):
        first = self.registry.register("codex", channels=["one"])
        second = self.registry.register("codex", channels=["two"])
        legacy = self.registry.register("claude")
        self.router.update_agents(["codex", "codex-1", "codex-2", "claude"])
        self.assertEqual(set(self.router.get_targets("human", "@all hi", "one")), {"codex", "codex-1"})
        self.assertEqual(self.router.get_targets("human", "@codex-2 hi", "one"), [])
        self.assertEqual(self.router.get_targets("human", "hi", "general"), [legacy["name"]])
        self.agents.trigger_sync(second["name"], channel="one")
        self.assertEqual(self.native.deliveries(second["identity_id"]), [])
        self.agents.trigger_sync("codex-1", channel="one")
        self.assertEqual(len(self.native.deliveries(first["identity_id"])), 1)

    def test_bridge_auth_identity_project_default_and_disconnect_revocation(self):
        inst = self.register()
        channel = inst["channels"][0]
        self.store.add("human", "project task", channel=channel)
        self.store.add("human", "other project task", channel="unrelated")
        event = self.native.enqueue(inst["identity_id"], {"channel": channel})
        response = self.chat(inst)
        self.assertEqual(response.status_code, 200)
        self.assertIn("project task", json.loads(response.body)["result"])
        self.assertNotIn("other project task", json.loads(response.body)["result"])
        self.assertEqual(self.native.deliveries()[0]["id"], event)
        self.assertEqual(self.native.deliveries()[0]["state"], "delivered")
        self.registry.rename(inst["name"], "project-worker")
        response = self.chat(inst, "send", message="result", sender="claude")
        self.assertEqual(response.status_code, 200)
        message = self.store.get_recent(1)[0]
        self.assertEqual((message["sender"], message["channel"]), ("project-worker", channel))
        self.assertEqual(self.chat(inst, channel="unrelated").status_code, 400)
        self.assertEqual(self.chat(inst, "send", message="no", channel="unrelated").status_code, 400)
        response = asyncio.run(app.deregister_agent("stale-name", Request({}, inst["token"])))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.chat(inst).status_code, 403)

    def test_membership_change_and_channel_rename(self):
        inst = self.register()
        old = inst["channels"][0]
        self.registry.rename_channel(old, "renamed-project")
        self.settings["channels"].append("renamed-project")
        response = asyncio.run(app.set_agent_channels(inst["name"], Request({"channels": ["general", "renamed-project"]})))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.registry.in_channel(inst["name"], "general"))
        self.assertFalse(self.registry.in_channel(inst["name"], old))
        self.assertEqual(asyncio.run(app.set_agent_channels(inst["name"], Request({"channels": []}))).status_code, 400)

    def test_detach_monitor_never_terminates_agent_or_injects_in_manual_mode(self):
        args = SimpleNamespace(pane="%7", tmux_socket=None, port=8300, channel=None, label=None,
                               notify="manual", remote=None, thread=None)
        inst = self.register()
        with patch.object(adoption, "request", side_effect=[{"protocol": 1}, inst, {"ok": True}]) as request, \
             patch.object(adoption, "still_attached", return_value=False), \
             patch.object(wrapper_unix, "inject") as inject, \
             patch("os.kill") as kill, patch("os.killpg") as killpg, contextlib.redirect_stdout(io.StringIO()):
            adoption.run(args)
        inject.assert_not_called()
        kill.assert_not_called()
        killpg.assert_not_called()
        self.assertIn("/api/deregister/", request.call_args.args[1])
        self.assertEqual(list(self.native.root.glob("*/chat.json")), [])

    def test_terminal_partial_delivery_stops_and_never_retries(self):
        inst = self.register(notify="tmux")
        runtime = self.native.create_runtime("codex", inst, target=self.target,
                                             project=project_context(self.root), kind="adopted")
        self.native.enqueue(inst["identity_id"], {"channel": inst["channels"][0]})
        with patch.object(adoption, "still_attached", return_value=True), \
             patch.object(wrapper_unix, "inject", return_value=False) as inject:
            adoption.deliver_terminal(self.native, runtime, self.root / "chat.json", self.native.deliveries())
            self.assertEqual(self.native.deliveries()[0]["state"], "uncertain")
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                adoption.deliver_terminal(self.native, runtime, self.root / "chat.json", self.native.deliveries())
        inject.assert_called_once()

    def test_atomic_pending_claim_does_not_replay_an_already_read_notification(self):
        inst = self.register()
        event = self.native.enqueue(inst["identity_id"], {"channel": inst["channels"][0]})
        self.assertTrue(self.native.transition(event, "delivered", expected_state="pending"))
        self.assertFalse(self.native.transition(event, "submitting", expected_state="pending"))
        self.assertEqual(self.native.deliveries()[0]["state"], "delivered")

    def test_monitor_pauses_during_server_restart_and_reuses_identity(self):
        args = SimpleNamespace(pane="%7", tmux_socket=None, port=8300, channel=None, label=None,
                               notify="manual", remote=None, thread=None)
        inst = self.register()
        replies = [{"protocol": 1}, inst, adoption.ServerUnavailable("offline"),
                   {"name": inst["name"], "channels": inst["channels"]}, {"ok": True}]
        with patch.object(adoption, "request", side_effect=replies) as request, \
             patch.object(adoption, "still_attached", side_effect=[True, True, False]), \
             patch.object(adoption.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as output:
            adoption.run(args)
        self.assertIn("reconnected", output.getvalue())
        self.assertEqual(sum(c.args[1] == "/api/register" for c in request.call_args_list), 1)
        tokens = [c.args[2] for c in request.call_args_list if c.args[1].startswith("/api/heartbeat/")]
        self.assertEqual(tokens, [inst["token"], inst["token"]])

    def test_native_thread_cannot_be_adopted_twice_through_different_panes(self):
        first = {"source": "adopted", "adoption_target": "pane1", "adoption_thread": "socket:thread"}
        self.registry.register("codex", context=first)
        with self.assertRaisesRegex(ValueError, "conversation is already adopted"):
            self.registry.register("codex", context={**first, "adoption_target": "pane2"})

    def test_crash_recovery_uses_original_runtime_and_checks_owner_before_heartbeat(self):
        inst = self.register()
        runtime = self.native.create_runtime("codex", inst, target=self.target,
                                             project=project_context(self.root), kind="adopted", notify="manual")
        args = SimpleNamespace(pane=None, tmux_socket=None, port=8300, channel=None, label=None,
                               notify="manual", remote=None, thread=None, resume_runtime=runtime["id"])
        capabilities = {"protocol": 1, "data_dir": str(self.root)}
        with self.native.lock(runtime["id"]), patch.object(adoption, "request", return_value=capabilities) as request:
            with self.assertRaisesRegex(ValueError, "already owned"):
                adoption.run(args)
        self.assertEqual(request.call_count, 1)
        replies = [capabilities, {"name": inst["name"], "channels": inst["channels"]}, {"ok": True}]
        with patch.object(adoption, "request", side_effect=replies) as request, \
             patch.object(adoption, "still_attached", side_effect=[True, False]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            adoption.run(args)
        self.assertIn(runtime["id"], output.getvalue())
        self.assertFalse(any(c.args[1] == "/api/register" for c in request.call_args_list))
        self.assertEqual(self.native.deliveries(), [])

    def test_failed_monitor_can_reclaim_but_normal_disconnect_revokes(self):
        inst = self.register()
        response = asyncio.run(app.deregister_agent(inst["name"], Request({"recoverable": True}, inst["token"])))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.registry.resolve_token(inst["token"])["identity_id"], inst["identity_id"])
        asyncio.run(app.deregister_agent(inst["name"], Request({}, inst["token"])))
        self.assertIsNone(self.registry.resolve_token(inst["token"]))

    def test_adopt_and_chat_do_not_require_config_in_project(self):
        with patch.object(adoption, "run") as run:
            cli.main(["adopt", "--pane", "%7", "--notify", "tmux"])
        self.assertEqual(run.call_args.args[0].notify, "tmux")
        with patch.object(adoption, "chat") as chat:
            cli.main(["chat", "--session", "/tmp/private-session", "send", "--message", "hello"])
        self.assertEqual(chat.call_args.args[0].message, "hello")


class TerminalTargetTests(unittest.TestCase):
    def test_process_replacement_and_cwd_change_disconnect(self):
        target = {"socket": "/tmp/tmux", "pane": "%7", "pane_pid": 1, "pid": 2,
                  "start": "100", "base": "codex", "cwd": "/project"}
        for changed in ({"pid": 3}, {"start": "101"}, {"cwd": "/another"}, {"base": "bash"}):
            with patch.object(adoption, "inspect_pane", return_value={**target, **changed}):
                self.assertFalse(adoption.still_attached(target))

    def test_socket_is_pinned_and_process_checked_before_enter(self):
        commands = []
        def run(cmd, **kwargs):
            commands.append(cmd)
            return SimpleNamespace(returncode=0, stdout=b"%7\n", stderr=b"")
        with patch.object(wrapper_unix.subprocess, "run", side_effect=run), \
             patch.object(wrapper_unix.time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()):
            result = wrapper_unix.inject("hello", tmux_session="%7", socket_path="/tmp/private-tmux",
                                         before_send=iter([True, False]).__next__)
        self.assertFalse(result)
        self.assertTrue(all(c[:3] == ["tmux", "-S", "/tmp/private-tmux"] for c in commands))
        self.assertFalse(any("send-keys" in c for c in commands))
