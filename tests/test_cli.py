"""CLI contracts without starting agents or changing provider settings."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentchattr import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.toml"
        self.config.write_text('[agents.fake]\ncommand = "fake"\n[agents.local]\ntype = "api"\n')
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def args(self, *values):
        return [*values, "--config", str(self.config)]

    def assert_error(self, args, text):
        with contextlib.redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            cli.main(args)
        self.assertEqual(error.exception.code, 2)
        self.assertIn(text, output.getvalue())

    def test_serve_receives_normalized_config_and_overrides(self):
        with patch("agentchattr.run.main") as serve:
            cli.main(self.args("serve", "--port=9100", "--allow-network"))
        config = serve.call_args.args[0]
        self.assertEqual(config["server"]["port"], 9100)
        self.assertEqual(config["server"]["data_dir"], str(self.config.parent / "data"))
        self.assertEqual(serve.call_args.kwargs, {"allow_network": True})

    def test_cli_agent_preserves_every_argument_after_separator(self):
        extra = ["--port", "9999", "--config", "agent.toml", "--", "two words"]
        with patch("agentchattr.wrapper.main") as agent:
            cli.main(self.args("agent", "fake", "--port", "9100", "--label", "Worker", "--no-restart") + ["--", *extra])
        config, args, forwarded = agent.call_args.args
        self.assertEqual(config["server"]["port"], 9100)
        self.assertEqual(args.label, "Worker")
        self.assertTrue(args.no_restart)
        self.assertEqual(forwarded, extra)

    def test_api_agent_dispatch(self):
        with patch("agentchattr.wrapper_api.main") as agent, patch("agentchattr.wrapper.main") as other:
            cli.main(self.args("agent", "local"))
        self.assertEqual(agent.call_args.args[1].agent, "local")
        other.assert_not_called()

    def test_local_config_can_change_provider_type(self):
        (self.config.parent / "config.local.toml").write_text('[agents.fake]\ntype = "api"\n')
        with patch("agentchattr.wrapper_api.main") as agent:
            cli.main(self.args("agent", "fake"))
        agent.assert_called_once()

    def test_help_does_not_require_configuration(self):
        for args in (["--help"], ["serve", "--help"], ["agent", "--help"]):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
                cli.main(args)
            self.assertEqual(error.exception.code, 0)

    def test_unknown_agent(self):
        self.assert_error(self.args("agent", "unknown"), "Unknown agent")

    def test_missing_config(self):
        self.assert_error(["serve", "--config", str(self.config.parent / "missing.toml")], "Cannot load configuration")

    def test_malformed_config(self):
        self.config.write_text("[broken")
        self.assert_error(self.args("serve"), "Cannot load configuration")

    def test_api_agent_rejects_cli_options(self):
        self.assert_error(self.args("agent", "local") + ["--", "--foo"], "API agents do not accept")
        self.assert_error(self.args("agent", "local", "--no-restart"), "API agents do not accept")

    def test_unknown_option_is_not_silently_forwarded(self):
        self.assert_error(self.args("agent", "fake", "--typo"), "unrecognized arguments")

    def test_serve_rejects_agent_arguments(self):
        self.assert_error(self.args("serve") + ["--", "--foo"], "only supported by the agent command")

    def test_no_server_produces_actionable_failure(self):
        with patch("agentchattr.wrapper._register_instance", side_effect=ConnectionRefusedError), \
             contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as error:
            cli.main(self.args("agent", "fake"))
        self.assertEqual(error.exception.code, 1)
        self.assertIn("Start the server first", output.getvalue())
