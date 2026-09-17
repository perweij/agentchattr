"""Regression: a job attachment must not hit a shadowed Path import."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentchattr import mcp_bridge
from agentchattr.jobs import JobStore
from agentchattr.store import MessageStore


class JobImageTests(unittest.TestCase):
    def test_job_attachment_is_copied_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = JobStore(str(root / "jobs.json"))
            job = jobs.create("Example", "task", "general", "user")
            image = root / "example.png"
            image.write_bytes(b"test image content")
            store = MessageStore(str(root / "messages.jsonl"))
            with patch.multiple(mcp_bridge, jobs=jobs, store=store, registry=None,
                                router=None, agents=None,
                                config={"images": {"upload_dir": str(root / "uploads")}}), \
                 patch.dict(mcp_bridge._presence, {}, clear=True):
                result = mcp_bridge.chat_send("user", "attachment", job_id=job["id"], image_path=str(image))
            self.assertIn("Sent to job", result)
            messages = JobStore(str(root / "jobs.json")).get_messages(job["id"])
            attachment = messages[-1]["attachments"][0]
            self.assertEqual((root / attachment["url"].lstrip("/")).read_bytes(), image.read_bytes())
