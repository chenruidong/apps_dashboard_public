import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apps_dashboard import launchagent


class FakeLaunchctl:
    """launchd as install sees it: ``print`` answers while the job is loaded; a booted-out job lingers
    for ``linger`` more prints, and ``bootstrap`` fails with 5 while it does (or ``fail`` more times)."""

    def __init__(self, loaded=True, linger=0, fail=0):
        self.loaded = loaded
        self.linger = 0
        self.linger_after_bootout = linger
        self.fail = fail
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args[0])
        ok = subprocess.CompletedProcess(args, 0, "", "")
        err = subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5: Input/output error")
        if args[0] == "print":
            if self.linger:
                self.linger -= 1
                return ok
            return ok if self.loaded else subprocess.CompletedProcess(args, 113, "", "not found")
        if args[0] == "bootout":
            self.loaded = False
            self.linger = self.linger_after_bootout
            return ok
        if args[0] == "bootstrap":
            if self.linger or self.fail:
                self.fail = max(0, self.fail - 1)
                return err
            self.loaded = True
            return ok
        return ok  # kickstart


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.object(launchagent, "PLIST", root / "agent.plist"),
                        mock.patch.object(launchagent, "LOG", root / "logs" / "agent.log")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def install(self, fake, args=()):
        self.now = 0.0
        self.err = io.StringIO()

        def sleep(seconds):
            self.now += seconds

        with mock.patch.object(launchagent, "_launchctl", fake), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(self.err):
            return launchagent.install(list(args), sleep=sleep, clock=lambda: self.now)

    def test_first_install(self):
        fake = FakeLaunchctl(loaded=False)
        self.assertEqual(self.install(fake), 0)
        self.assertEqual(fake.calls, ["print", "bootstrap"])
        self.assertTrue(launchagent.PLIST.is_file())

    def test_unchanged_plist_is_restarted_in_place(self):
        self.install(FakeLaunchctl(loaded=False))
        fake = FakeLaunchctl(loaded=True)
        self.assertEqual(self.install(fake), 0)
        self.assertEqual(fake.calls, ["print", "kickstart"])

    def test_changed_plist_waits_for_the_old_job(self):
        self.install(FakeLaunchctl(loaded=False))
        fake = FakeLaunchctl(loaded=True, linger=3)
        self.assertEqual(self.install(fake, ["--port", "8797"]), 0)
        self.assertEqual(fake.calls, ["print", "bootout", "print", "print", "print", "print", "bootstrap"])
        self.assertIn(b"8797", launchagent.PLIST.read_bytes())

    def test_bootstrap_retried_then_reported(self):
        fake = FakeLaunchctl(loaded=False, fail=2)
        self.assertEqual(self.install(fake), 0)
        self.assertEqual(fake.calls.count("bootstrap"), 3)
        fake = FakeLaunchctl(loaded=False, fail=99)
        self.assertEqual(self.install(fake), 5)
        self.assertEqual(fake.calls.count("bootstrap"), launchagent.BOOTSTRAP_TRIES)

    def test_wait_for_the_old_job_is_bounded(self):
        self.install(FakeLaunchctl(loaded=False))
        fake = FakeLaunchctl(loaded=True, linger=10 ** 6)
        self.assertEqual(self.install(fake, ["--port", "8797"]), 5)
        self.assertEqual(fake.calls.count("bootstrap"), launchagent.BOOTSTRAP_TRIES)
        self.assertLessEqual(self.now, launchagent.GONE_TIMEOUT + 2 * launchagent.BOOTSTRAP_TRIES)
        self.assertIn("load it by hand: launchctl bootstrap", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
