"""`[triage].command`: a hosted agent CLI (Claude Code, Codex) answers triage instead of an HTTP endpoint."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from factory import config, triage


def load(toml: str) -> config.Config:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / config.CONFIG_NAME).write_text('[repo]\nslug = "acme/widgets"\n' + toml)
        with patch.object(config, "repo_root", return_value=root), \
                patch.object(config, "host_config", return_value={}):
            return config.load(root)


class TriageCommandConfigTest(unittest.TestCase):
    def test_command_string_becomes_argv_and_names_the_model(self) -> None:
        cfg = load('[triage]\ncommand = "claude -p --model haiku --tools \'\'"\n')
        self.assertEqual(cfg.triage_command, ["claude", "-p", "--model", "haiku", "--tools", ""])
        self.assertEqual(cfg.llm_model, "haiku")
        self.assertEqual(load('[triage]\ncommand = ["codex", "exec", "-"]\n').llm_model, "codex")

    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(config.ConfigError):
            load('[triage]\ncommand = []\n')


class TriageCommandCallTest(unittest.TestCase):
    def call(self, script: str, timeout: int = 10):
        execution = SimpleNamespace(outcome=None, reason=None, emit=lambda *a, **k: None)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(Path(tmp), "acme/widgets", signoff=False)
            cfg.triage_command = ["/bin/sh", "-c", script]
            cfg.triage_timeout = timeout
            stderr = io.StringIO()
            with patch.object(triage, "cfg", cfg, create=True), redirect_stderr(stderr):
                try:
                    reply = triage.call_llm([{"role": "system", "content": "SYS"},
                                             {"role": "user", "content": "USER"}], execution)
                except SystemExit as exc:
                    return exc, execution, stderr.getvalue()
        return reply, execution, stderr.getvalue()

    def test_prompt_goes_to_stdin_and_stdout_is_the_reply(self) -> None:
        reply, execution, _ = self.call('read a; read b; read c; printf \'{"echo": "%s|%s"}\\n\' "$a" "$c"')
        self.assertEqual(reply, '{"echo": "SYS|USER"}')
        self.assertIsNone(execution.outcome)

    def test_failure_is_a_mechanism_failure_with_its_stderr(self) -> None:
        exc, execution, stderr = self.call("echo 'usage limit reached' >&2; exit 1")
        self.assertIsInstance(exc, SystemExit)
        self.assertNotIsInstance(exc, triage.EndpointTimeout)
        self.assertEqual((execution.outcome, execution.reason), ("mechanism_failure", "triage_endpoint_unavailable"))
        self.assertIn("usage limit reached", stderr)

    def test_empty_reply_is_a_failure(self) -> None:
        exc, execution, _ = self.call("exit 0")
        self.assertIsInstance(exc, SystemExit)
        self.assertEqual(execution.reason, "triage_endpoint_unavailable")

    def test_timeout_lets_the_pass_continue(self) -> None:
        exc, execution, _ = self.call("sleep 5", timeout=1)
        self.assertIsInstance(exc, triage.EndpointTimeout)
        self.assertEqual((execution.outcome, execution.reason), ("unknown", "triage_endpoint_timeout"))


if __name__ == "__main__":
    unittest.main()
