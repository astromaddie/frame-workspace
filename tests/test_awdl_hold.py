"""The AWDL hold script, run against stand-in ifconfig and pgrep commands."""
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOLD = ROOT / "mac" / "awdl-hold"
SCRIPT = HOLD / "frame-workspace-awdl-hold"


class AwdlHold(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "ifconfig.log"
        self.up = self.dir / "awdl-up"
        self.running = self.dir / "workspace-running"
        self.state = self.dir / "held"
        ifconfig = self.dir / "ifconfig"
        ifconfig.write_text(
            "#!/bin/sh\n"
            f'if [ "$#" -eq 1 ]; then\n'
            f'  if [ -e "{self.up}" ]; then echo "awdl0: flags=8863<UP,BROADCAST> mtu 1500";\n'
            f'  else echo "awdl0: flags=8822<BROADCAST> mtu 1500"; fi; exit 0; fi\n'
            f'echo "$2" >> "{self.log}"\n'
            f'if [ "$2" = up ]; then : > "{self.up}"; else rm -f "{self.up}"; fi\n')
        pgrep = self.dir / "pgrep"
        pgrep.write_text(f'#!/bin/sh\n[ -e "{self.running}" ]\n')
        for f in (ifconfig, pgrep):
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "AWDL_HOLD_IFCONFIG": str(ifconfig),
                    "AWDL_HOLD_PGREP": str(pgrep), "AWDL_HOLD_STATE": str(self.state),
                    "AWDL_HOLD_STEPS": "1"}

    def step(self):
        subprocess.run(["sh", str(SCRIPT)], env=self.env, check=True, timeout=10)

    def actions(self):
        return self.log.read_text().split() if self.log.exists() else []

    def test_holds_down_while_workspace_runs_and_restores_after(self):
        self.up.touch()
        self.running.touch()
        self.step()
        self.assertFalse(self.up.exists())
        self.up.touch()  # macOS brings it back; the next check takes it down again
        self.step()
        self.assertEqual(self.actions(), ["down", "down"])
        self.running.unlink()
        self.step()
        self.assertTrue(self.up.exists())
        self.assertFalse(self.state.exists())
        self.step()  # nothing more once restored
        self.assertEqual(self.actions(), ["down", "down", "up"])

    def test_leaves_awdl_alone_without_a_workspace(self):
        self.step()
        self.up.touch()
        self.step()
        self.assertEqual(self.actions(), [])

    def test_does_not_raise_awdl_it_did_not_lower(self):
        self.running.touch()  # already down when the Workspace starts
        self.step()
        self.running.unlink()
        self.step()
        self.assertEqual(self.actions(), [])
        self.assertFalse(self.up.exists())

    def test_launchd_definition_runs_the_installed_script(self):
        plist = plistlib.loads((HOLD / "com.frame-workspace.awdl-hold.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/usr/local/libexec/frame-workspace-awdl-hold"])
        self.assertTrue(plist["KeepAlive"] and plist["RunAtLoad"])
        for script in ("install.sh", "uninstall.sh", "frame-workspace-awdl-hold"):
            subprocess.run(["sh", "-n", str(HOLD / script)], check=True)


if __name__ == "__main__":
    unittest.main()
