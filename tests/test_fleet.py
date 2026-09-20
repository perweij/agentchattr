"""Fleet checkout execution and configuration isolation."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agentchattr.config_loader import load_config

ROOT = Path(__file__).resolve().parents[1]


class FleetTests(unittest.TestCase):
    def test_manifest_data(self):
        if not shutil.which("guile"):
            self.skipTest("Guile unavailable")
        subprocess.run(["guile", "--no-auto-compile", "-c", '''
          (call-with-input-file "fleet.scm" (lambda (port)
            (let ((data (read port)))
              (unless (and (eof-object? (read port))
                           (equal? (map car data) '(manifest name command executable requires))
                           (equal? (assoc-ref data 'manifest) 1)
                           (symbol? (assoc-ref data 'name))
                           (equal? (assoc-ref data 'command) "agentchattr")
                           (equal? (assoc-ref data 'executable) "bin/agentchattr")
                           (equal? (assoc-ref data 'requires)
                                   '(python3 coreutils git tmux xdg-utils)))
                (error "invalid manifest")))))
        '''], cwd=ROOT, check=True, capture_output=True)
        self.assertTrue((ROOT / "bin/agentchattr").is_file())
        self.assertTrue(os.access(ROOT / "bin/agentchattr", os.X_OK))

    def test_symlink_help_without_uv_or_state(self):
        with tempfile.TemporaryDirectory(prefix="fleet checkout ") as directory:
            base = Path(directory)
            link = base / "agentchattr"
            link.symlink_to(ROOT / "bin/agentchattr")
            (base / "python3").symlink_to(shutil.which("python3"))
            env = {"PATH": str(base), "HOME": str(base / "home")}
            result = subprocess.run([str(link), "serve", "--help"], cwd=base,
                                    env=env, capture_output=True, text=True, check=True)
            self.assertIn("--config", result.stdout)
            self.assertFalse((base / "home").exists())

    def test_fallback_and_explicit_paths(self):
        with tempfile.TemporaryDirectory(prefix="fleet config ") as directory:
            base = Path(directory)
            env = {"HOME": str(base), "XDG_CONFIG_HOME": str(base / "config"),
                   "XDG_DATA_HOME": str(base / "data")}
            with patch.dict(os.environ, env, clear=True), patch("pathlib.Path.cwd", return_value=base):
                config = load_config()
                self.assertEqual(config["server"]["data_dir"], str(base / "data/agentchattr"))
                self.assertEqual(config["agents"]["codex"]["cwd"], str(base))
                self.assertTrue((ROOT / "src/agentchattr/static/index.html").is_file())
                xdg = base / "config/agentchattr/config.toml"
                xdg.parent.mkdir(parents=True)
                xdg.write_text('[server]\ndata_dir = "saved"\n')
                self.assertEqual(load_config()["server"]["data_dir"], str(xdg.parent / "saved"))
                local = base / "config.toml"
                local.write_text('[server]\ndata_dir = "local"\n')
                self.assertEqual(load_config()["server"]["data_dir"], str(base / "local"))
                with self.assertRaises(FileNotFoundError):
                    load_config(config_path=base / "missing.toml")
