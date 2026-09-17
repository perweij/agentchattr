"""Tests for AGENTCHATTR_* env var overrides in config_loader.

These tests exercise load_config() directly (not through run.py) because
wrappers also call load_config(), and the core guarantee is that the
same env vars produce the same config regardless of entry point.
"""

import os
import tempfile
from unittest.mock import patch
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from agentchattr import config_loader


ENV_VARS = [
    "AGENTCHATTR_DATA_DIR",
    "AGENTCHATTR_PORT",
    "AGENTCHATTR_MCP_HTTP_PORT",
    "AGENTCHATTR_MCP_SSE_PORT",
    "AGENTCHATTR_UPLOAD_DIR",
]


class ConfigOverrideTests(unittest.TestCase):
    def setUp(self):
        # Snapshot and clear all override env vars so tests don't interfere.
        self._saved = {k: os.environ.get(k) for k in ENV_VARS}
        for k in ENV_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_env_vars_uses_config_toml_values(self):
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8300)
        self.assertEqual(config["server"]["data_dir"], str(ROOT / "data"))

    def test_port_env_var_overrides_config(self):
        os.environ["AGENTCHATTR_PORT"] = "8310"
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8310)

    def test_mcp_ports_env_vars_override_config(self):
        os.environ["AGENTCHATTR_MCP_HTTP_PORT"] = "8210"
        os.environ["AGENTCHATTR_MCP_SSE_PORT"] = "8211"
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["mcp"]["http_port"], 8210)
        self.assertEqual(config["mcp"]["sse_port"], 8211)

    def test_data_dir_absolute_path_preserved(self):
        abs_path = str(Path("/tmp/test-agentchattr").resolve())
        os.environ["AGENTCHATTR_DATA_DIR"] = abs_path
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["data_dir"], abs_path)

    def test_data_dir_relative_path_resolves_to_cwd(self):
        # Relative path should resolve against CWD, not agentchattr install
        os.environ["AGENTCHATTR_DATA_DIR"] = "./my-project-data"
        config = config_loader.load_config(ROOT)
        expected = str((Path.cwd() / "my-project-data").resolve())
        self.assertEqual(config["server"]["data_dir"], expected)

    def test_upload_dir_relative_path_resolves_to_cwd(self):
        os.environ["AGENTCHATTR_UPLOAD_DIR"] = "./my-uploads"
        config = config_loader.load_config(ROOT)
        expected = str((Path.cwd() / "my-uploads").resolve())
        self.assertEqual(config["images"]["upload_dir"], expected)

    def test_empty_env_var_does_not_override(self):
        os.environ["AGENTCHATTR_PORT"] = ""
        config = config_loader.load_config(ROOT)
        # Empty value is ignored, default stays
        self.assertEqual(config["server"]["port"], 8300)

    def test_invalid_int_env_var_warns_and_keeps_default(self):
        os.environ["AGENTCHATTR_PORT"] = "not-a-number"
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["port"], 8300)

    def test_all_overrides_applied_together(self):
        abs_data = str(Path("/tmp/proj-a/.agentchattr").resolve())
        abs_uploads = str(Path("/tmp/proj-a/uploads").resolve())
        os.environ["AGENTCHATTR_DATA_DIR"] = abs_data
        os.environ["AGENTCHATTR_PORT"] = "8310"
        os.environ["AGENTCHATTR_MCP_HTTP_PORT"] = "8210"
        os.environ["AGENTCHATTR_MCP_SSE_PORT"] = "8211"
        os.environ["AGENTCHATTR_UPLOAD_DIR"] = abs_uploads
        config = config_loader.load_config(ROOT)
        self.assertEqual(config["server"]["data_dir"], abs_data)
        self.assertEqual(config["server"]["port"], 8310)
        self.assertEqual(config["mcp"]["http_port"], 8210)
        self.assertEqual(config["mcp"]["sse_port"], 8211)
        self.assertEqual(config["images"]["upload_dir"], abs_uploads)

    def test_agents_section_unchanged_by_overrides(self):
        os.environ["AGENTCHATTR_PORT"] = "8310"
        config = config_loader.load_config(ROOT)
        # Agent definitions must be untouched by path/port overrides
        self.assertIn("claude", config["agents"])
        self.assertEqual(config["agents"]["claude"]["command"], "claude")


class PersonalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "custom.toml"
        self.config.write_text('[server]\nport = 8300\ndata_dir = "state"\n'
                               '[agents.codex]\ncommand = "codex"\ncwd = "project"\n'
                               'tags = ["original"]\n')
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_recursive_local_merge_preserves_siblings_and_replaces_lists(self):
        (self.root / "config.local.toml").write_text(
            '[server]\nport = 8311\n'
            '[agents.codex]\nlabel = "My agent"\ntags = ["replacement"]\n'
            '[agents.local]\ntype = "api"\nmodel = "test"\n')
        config = config_loader.load_config(config_path=self.config)
        self.assertEqual(config["server"]["port"], 8311)
        self.assertEqual(config["server"]["data_dir"], str(self.root / "state"))
        self.assertEqual(config["agents"]["codex"]["command"], "codex")
        self.assertEqual(config["agents"]["codex"]["label"], "My agent")
        self.assertEqual(config["agents"]["codex"]["tags"], ["replacement"])
        self.assertEqual(config["agents"]["local"]["type"], "api")

    def test_cli_beats_environment_and_local_without_mutating_environment(self):
        (self.root / "config.local.toml").write_text('[server]\nport = 8311\n')
        os.environ["AGENTCHATTR_PORT"] = "8312"
        config = config_loader.load_config(config_path=self.config, overrides={"port": 8313})
        self.assertEqual(config["server"]["port"], 8313)
        self.assertEqual(os.environ["AGENTCHATTR_PORT"], "8312")

    def test_config_paths_resolve_against_config_directory(self):
        config = config_loader.load_config(config_path=self.config)
        self.assertEqual(config["server"]["data_dir"], str(self.root / "state"))
        self.assertEqual(config["images"]["upload_dir"], str(self.root / "uploads"))
        self.assertEqual(config["agents"]["codex"]["cwd"], str(self.root / "project"))

    def test_cli_paths_resolve_against_invocation_directory(self):
        config = config_loader.load_config(config_path=self.config,
                                          overrides={"data_dir": "personal", "upload_dir": "images"})
        self.assertEqual(config["server"]["data_dir"], str(Path.cwd() / "personal"))
        self.assertEqual(config["images"]["upload_dir"], str(Path.cwd() / "images"))

    def test_all_cli_overrides(self):
        config = config_loader.load_config(config_path=self.config, overrides={
            "port": 9000, "mcp_http_port": 9001, "mcp_sse_port": 9002,
            "data_dir": str(self.root / "data"), "upload_dir": str(self.root / "images")})
        self.assertEqual(config["server"]["port"], 9000)
        self.assertEqual(config["mcp"], {"http_port": 9001, "sse_port": 9002})
        self.assertEqual(config["server"]["data_dir"], str(self.root / "data"))
        self.assertEqual(config["images"]["upload_dir"], str(self.root / "images"))

    def test_home_paths_are_expanded(self):
        os.environ["AGENTCHATTR_DATA_DIR"] = "~/state"
        config = config_loader.load_config(config_path=self.config)
        self.assertEqual(config["server"]["data_dir"], str(Path.home() / "state"))


if __name__ == "__main__":
    unittest.main()
