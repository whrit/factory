"""macOS `factory install`: LaunchAgents with the systemd units' pass and activation semantics."""

from __future__ import annotations

import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from factory import config, onboard, watch


class SpanTest(unittest.TestCase):
    def test_systemd_spans_become_seconds(self) -> None:
        for value, seconds in (("10min", 600), ("1h 30min", 5400), ("90", 90), ("2d", 172800)):
            with self.subTest(value=value):
                self.assertEqual(onboard.span_seconds(value), seconds)
        for value in ("soon", "10 fortnights", "0min", ""):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                onboard.span_seconds(value)


class PassScriptTest(unittest.TestCase):
    """The pass script against a fake interpreter whose subcommands exit as told."""

    def run_pass(self, **exits: int) -> tuple[int, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            python = Path(tmp) / "python"
            # The script calls `<python> -P -m factory <command>`: the command is $4.
            python.write_text(
                "#!/bin/sh\n"
                f'echo "$4 result=$SERVICE_RESULT status=$EXIT_STATUS" >> {log}\n'
                'case "$4" in\n'
                + "".join(f'  {name}) exit {code};;\n' for name, code in exits.items())
                + "esac\nexit 0\n"
            )
            python.chmod(0o755)
            proc = subprocess.run(["/bin/sh", "-c", onboard.PASS_SCRIPT, str(python)],
                                  env={"PATH": "/usr/bin:/bin"}, check=False)
            return proc.returncode, log.read_text().splitlines()

    def test_success_runs_every_stage_then_watch(self) -> None:
        code, lines = self.run_pass()
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in lines], ["drain-check", "triage", "dispatch", "watch"])
        self.assertEqual(lines[-1], "watch result=success status=0")

    def test_drain_skip_runs_only_watch_and_does_not_fail(self) -> None:
        code, lines = self.run_pass(**{"drain-check": 1})
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["drain-check result= status=", "watch result=exec-condition status=0"])

    def test_triage_failure_still_dispatches_and_dispatch_failure_is_reported(self) -> None:
        code, lines = self.run_pass(triage=2, dispatch=3)
        self.assertEqual(code, 3)
        self.assertIn("dispatch result= status=", lines)
        self.assertEqual(lines[-1], "watch result=exit-code status=3")


class InstallLaunchdTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.agents = self.root / "LaunchAgents"
        self.cfg = config.Config(self.root, "acme/widgets", signoff=False)
        self.cfg.install = {"every": "10min", "dashboard": False, "host": "127.0.0.1", "env": {"A": "1"},
                            "dispatch_env": {"GH_CONFIG_DIR": "/gh"}}
        self.loaded: set[str] = set()
        self.disabled: set[str] = set()
        self.calls: list[list[str]] = []

    def command(self, cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        self.assertEqual(cmd[0], "launchctl")
        action, target = cmd[1], cmd[-1]
        label = target.rsplit("/", 1)[-1].removesuffix(".plist")
        if action == "print":
            return subprocess.CompletedProcess(cmd, 0 if label in self.loaded else 113, "", "")
        if action == "print-disabled":
            out = "".join(f'\t"{name}" => disabled\n' for name in self.disabled)
            return subprocess.CompletedProcess(cmd, 0, f"disabled services = {{\n{out}}}\n", "")
        if action == "bootstrap":
            self.loaded.add(label)
        elif action == "bootout":
            self.loaded.discard(label)
        elif action == "enable":
            self.disabled.discard(label)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def install(self, *args: str) -> str:
        output = io.StringIO()
        with patch.object(onboard.config, "load", return_value=self.cfg), \
                patch.object(onboard, "launch_agents_dir", return_value=self.agents), \
                patch.object(onboard.config, "launchd_log", side_effect=lambda job: self.root / "logs" / f"{job}.log"), \
                patch.object(onboard, "sh", side_effect=self.command), \
                patch.object(onboard.shutil, "which", side_effect=lambda tool: "/bin/launchctl" if tool == "launchctl" else None), \
                redirect_stdout(output):
            self.assertEqual(onboard.install(list(args)), 0)
        return output.getvalue()

    def plist(self, label: str) -> dict:
        return plistlib.loads((self.agents / f"{label}.plist").read_bytes())

    def test_fresh_install_writes_and_loads_the_pass_agent(self) -> None:
        self.install()
        job = self.plist(self.cfg.unit)
        self.assertEqual((job["StartInterval"], job["RunAtLoad"]), (600, True))
        self.assertEqual(job["ProgramArguments"][:3], ["/bin/sh", "-c", onboard.PASS_SCRIPT])
        self.assertEqual(job["EnvironmentVariables"]["GH_CONFIG_DIR"], "/gh")
        self.assertEqual(job["WorkingDirectory"], str(self.root))
        self.assertIn(self.cfg.unit, self.loaded)
        self.assertFalse((self.agents / f"{self.cfg.unit}-dashboard.plist").exists())

    def test_dashboard_gets_identity_free_environment_and_keepalive(self) -> None:
        with patch.object(onboard, "_dashboard_port_check", return_value=(True, "free")):
            self.install("--dashboard")
        job = self.plist(f"{self.cfg.unit}-dashboard")
        self.assertNotIn("GH_CONFIG_DIR", job["EnvironmentVariables"])
        self.assertEqual(job["KeepAlive"], {"SuccessfulExit": False})
        self.assertIn(f"{self.cfg.unit}-dashboard", self.loaded)

    def test_existing_stopped_agent_stays_paused_unless_enabled(self) -> None:
        self.agents.mkdir()
        (self.agents / f"{self.cfg.unit}.plist").write_bytes(b"old")
        self.disabled.add(self.cfg.unit)
        output = self.install()
        self.assertNotIn(self.cfg.unit, self.loaded)
        self.assertIn(f"left paused {self.cfg.unit}", output)
        self.assertEqual(self.plist(self.cfg.unit)["StartInterval"], 600)
        self.install("--enable")
        self.assertIn(self.cfg.unit, self.loaded)

    def test_changed_loaded_agent_is_reloaded_and_unchanged_is_left_alone(self) -> None:
        self.install()
        self.calls.clear()
        self.install()
        self.assertFalse(any(call[1] in ("bootstrap", "bootout") for call in self.calls))
        self.install("--every", "5min")
        self.assertEqual(self.plist(self.cfg.unit)["StartInterval"], 300)
        self.assertIn(["launchctl", "bootout", f"gui/{os.getuid()}/{self.cfg.unit}"], self.calls)
        self.assertIn(self.cfg.unit, self.loaded)


class RuntimeLaunchdTest(unittest.TestCase):
    """Runtime JSON reads the pass agent from `launchctl print` when systemd is absent."""

    def observe(self, command) -> tuple[dict, list[dict]]:
        from factory import runtime_local

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(Path(tmp), "acme/widgets", signoff=False)
            with patch.object(runtime_local.shutil, "which",
                              side_effect=lambda tool: "/bin/launchctl" if tool == "launchctl" else None), \
                    patch.object(runtime_local, "_command", side_effect=command):
                state, errors = runtime_local.dispatcher(cfg, [], {})
        return state, [e for e in errors if e["source"] != "admission"]

    def test_loaded_running_agent_is_active_without_systemctl_errors(self) -> None:
        printed = "gui/501/factory-widgets = {\n\tactive count = 1\n\tstate = running\n\n\tendpoints = {\n\t\tstate = idle\n\t}\n}\n"
        state, errors = self.observe(lambda operation, **kw: printed)
        self.assertEqual((state["service_active"], state["timer_active"], state["next_at"]), (True, True, None))
        self.assertEqual(errors, [])

    def test_unloaded_agent_reports_unit_unavailable(self) -> None:
        from factory import runtime_local

        def fail(operation, **kw):
            raise runtime_local._Unavailable("command_failed")

        state, errors = self.observe(fail)
        self.assertEqual((state["service_active"], state["timer_active"]), (None, None))
        self.assertEqual(errors, [{"source": "launchctl", "scope": "service", "code": "unit_unavailable"}])


class DashboardPortOwnerTest(unittest.TestCase):
    def holder(self, agent_pid: str | None, listeners: str) -> str | None:
        cfg = config.Config(Path("/tmp"), "acme/widgets", signoff=False)

        def command(cmd, cwd=None):
            if cmd[:2] == ["launchctl", "print"]:
                out = f"{cmd[2]} = {{\n\tstate = running\n\tpid = {agent_pid}\n}}\n" if agent_pid else ""
                return subprocess.CompletedProcess(cmd, 0 if agent_pid else 113, out, "")
            self.assertEqual(cmd[:4], ["lsof", "-nP", "-iTCP:8765", "-sTCP:LISTEN"])
            return subprocess.CompletedProcess(cmd, 0, listeners, "")

        with patch.object(onboard, "sh", side_effect=command), \
                patch.object(onboard.shutil, "which", return_value="/bin/launchctl"):
            return onboard._launchd_dashboard_holder(cfg, 8765)

    def test_only_our_dashboard_agent_owns_the_port(self) -> None:
        self.assertEqual(self.holder("42", "42\n"), "owned by factory-widgets-dashboard (pid 42)")
        self.assertIsNone(self.holder("42", "42\n77\n"))  # another listener shares it
        self.assertIsNone(self.holder("42", "77\n"))
        self.assertIsNone(self.holder(None, "77\n"))  # agent not loaded


class WatchWithoutSystemdTest(unittest.TestCase):
    def test_failed_pass_is_reported_from_its_launchd_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(Path(tmp), "acme/widgets", signoff=False)
            log = Path(tmp) / f"{cfg.unit}.log"
            log.write_text("\n".join(f"line {i}" for i in range(30)) + "\nTraceback: boom\n")
            env = {"SERVICE_RESULT": "exit-code", "EXIT_CODE": "exited", "EXIT_STATUS": "1"}
            with patch.object(watch.subprocess, "run", side_effect=FileNotFoundError("systemctl")), \
                    patch.object(watch.config, "launchd_log", return_value=log), \
                    patch.dict(os.environ, env):
                units = watch.failed_units(cfg)
        self.assertEqual([name for name, _ in units], [f"{cfg.unit}.service"])
        self.assertIn("SERVICE_RESULT=exit-code", units[0][1])
        self.assertIn("Traceback: boom", units[0][1])
        self.assertNotIn("line 5\n", units[0][1])


if __name__ == "__main__":
    unittest.main()
