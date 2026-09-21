import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MACOS_SCRIPTS = ROOT / "scripts" / "macos"
WINDOWS_SCRIPTS = ROOT / "scripts" / "windows"


class MacLauncherTests(unittest.TestCase):
    def test_all_clickable_scripts_have_valid_bash_syntax(self) -> None:
        scripts = [
            MACOS_SCRIPTS / "saksham-common.sh",
            MACOS_SCRIPTS / "start-saksham.command",
            MACOS_SCRIPTS / "stop-saksham.command",
            MACOS_SCRIPTS / "install-shortcuts.command",
        ]
        subprocess.run(["bash", "-n", *map(str, scripts)], check=True)

    def test_dotenv_reader_does_not_evaluate_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / ".env"
            marker = root / "must-not-exist"
            env_file.write_text(
                'QUOTED="value with spaces"\n'
                f"LITERAL=$(touch {marker})\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "SAKSHAM_RUNTIME_DIR": str(root / "run"),
                "SAKSHAM_LOG_DIR": str(root / "logs"),
            }
            command = (
                f'source "{MACOS_SCRIPTS / "saksham-common.sh"}"; '
                f'printf "%s\\n" "$(saksham_dotenv_value "{env_file}" QUOTED)"; '
                f'printf "%s\\n" "$(saksham_dotenv_value "{env_file}" LITERAL)"'
            )
            result = subprocess.run(
                ["bash", "-c", command],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(
                ["value with spaces", f"$(touch {marker})"],
                result.stdout.splitlines(),
            )
            self.assertFalse(marker.exists())

    def test_meeting_guard_reports_only_unfinished_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                **os.environ,
                "SAKSHAM_RUNTIME_DIR": str(root / "run"),
                "SAKSHAM_LOG_DIR": str(root / "logs"),
            }
            payload = (
                '{"meetings":['
                '{"title":"Client call","capture_state":"processing"},'
                '{"title":"Old call","capture_state":"completed"},'
                '{"title":"Interview","capture_state":"recording"}'
                "]}"
            )
            command = (
                f'source "{MACOS_SCRIPTS / "saksham-common.sh"}"; '
                'saksham_parse_active_meetings "$(command -v python3)"'
            )
            result = subprocess.run(
                ["bash", "-c", command],
                check=True,
                input=payload,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(
                ["processing: Client call", "recording: Interview"],
                result.stdout.splitlines(),
            )


class WindowsLauncherTests(unittest.TestCase):
    def test_windows_scripts_parse_when_powershell_is_available(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is not installed on this host")
        scripts = sorted(WINDOWS_SCRIPTS.glob("*.ps1"))
        parser = (
            "$errors = @(); "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$args[0], [ref]$null, [ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
        )
        for script in scripts:
            subprocess.run(
                [powershell, "-NoProfile", "-Command", parser, str(script)],
                check=True,
            )

    def test_windows_launchers_do_not_embed_credentials(self) -> None:
        combined = "\n".join(
            script.read_text(encoding="utf-8")
            for script in WINDOWS_SCRIPTS.glob("*.ps1")
        )
        self.assertNotIn("MEETING_INTELLIGENCE_API_KEY=", combined)
        self.assertIn("Get-DotEnvValue", combined)
        self.assertIn("Wait-MeetingWorkerIdle", combined)


if __name__ == "__main__":
    unittest.main()
