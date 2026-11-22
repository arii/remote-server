"""buttons-volume-check.sh passes on a populated dir and fails loudly otherwise (#1517)."""

import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join("scripts", "buttons-volume-check.sh"))


def run_check(data_dir, **extra):
    env = dict(os.environ, BUTTONS_DATA_DIR=data_dir, **extra)
    return subprocess.run(
        ["sh", SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        timeout=30,
    )


class ButtonsVolumeCheckTest(unittest.TestCase):
    def test_passes_with_required_state(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "jobs_schedules.json"), "w") as f:
                f.write("{}")
            res = run_check(d)
            self.assertEqual(res.returncode, 0, res.stdout)
            self.assertIn("jobs_schedules.json is readable", res.stdout)

    def test_fails_when_state_file_missing(self):
        with tempfile.TemporaryDirectory() as d:
            res = run_check(d)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("FAIL", res.stdout)
            self.assertIn("jobs_schedules.json", res.stdout)

    def test_fails_when_directory_absent(self):
        with tempfile.TemporaryDirectory() as d:
            res = run_check(os.path.join(d, "nope"))
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("not on the host", res.stdout)

    def test_does_not_modify_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "jobs_schedules.json"), "w") as f:
                f.write("{}")
            before = sorted(os.listdir(d))
            run_check(d)
            self.assertEqual(sorted(os.listdir(d)), before)


if __name__ == "__main__":
    unittest.main()
