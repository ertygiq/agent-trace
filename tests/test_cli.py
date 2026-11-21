import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_trace import main


class CliTests(unittest.TestCase):
    def test_errors_suggest_help(self):
        for args, error in [
            ([], "missing --harness"),
            (["--unknown"], "unknown option: --unknown"),
        ]:
            with self.subTest(args=args):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main(args)
                self.assertEqual(status, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(),
                    f"agent-trace: {error}\nRun 'agent-trace --help' for usage.\n",
                )


if __name__ == "__main__":
    unittest.main()
