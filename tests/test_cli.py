import subprocess
import sys
import unittest


class CliTest(unittest.TestCase):
    def test_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "memtorture", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("evidence, not proof", result.stdout)

    def test_controls_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "memtorture", "controls", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--quick", result.stdout)


if __name__ == "__main__":
    unittest.main()
