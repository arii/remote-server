"""Redeploy's pull step must not hide a failed pull (#1488)."""

import subprocess
import unittest

from backend.exec import engine


def _pull_shell() -> str:
    steps, err = engine.action_steps("deploy-buttons", "remote-server", "", "")
    assert err is None
    return steps[0][-1]


def _run(git_exit: int) -> int:
    script = f"git() {{ return {git_exit}; }}; {_pull_shell()}"
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode


class DeployPullStepTest(unittest.TestCase):
    def test_failed_pull_fails_the_step(self):
        self.assertNotEqual(_run(1), 0)

    def test_successful_pull_passes_even_if_chmod_has_nothing_to_do(self):
        # /srv/remote-server/scripts does not exist here, so chmod fails;
        # that failure alone stays tolerated.
        self.assertEqual(_run(0), 0)

    def test_pull_is_fast_forward_only_and_precedes_the_rebuild(self):
        steps, _ = engine.action_steps("deploy-buttons", "remote-server", "", "")
        self.assertIn("pull --ff-only", steps[0][-1])
        self.assertIn("up -d --build buttons", steps[1][-1])


if __name__ == "__main__":
    unittest.main()
