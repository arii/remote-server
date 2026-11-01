import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

class TestPullModels(unittest.TestCase):
    def test_script_syntax(self):
        result = subprocess.run(["bash", "-n", "scripts/pull-models.sh"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"Syntax error in pull-models.sh: {result.stderr}")

if __name__ == "__main__":
    unittest.main()
