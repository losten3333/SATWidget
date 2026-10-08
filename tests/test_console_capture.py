import io
import sys
import unittest

from modules.console_capture import ConsoleCapture


class ConsoleCaptureTests(unittest.TestCase):
    def test_capture_preserves_stdout_and_stderr_while_hidden(self):
        capture = ConsoleCapture()
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        stdout = io.StringIO()
        stderr = io.StringIO()
        sys.stdout = stdout
        sys.stderr = stderr
        try:
            capture.install()
            print("ordinary output")
            print("error output", file=sys.stderr)
        finally:
            capture.uninstall()
        try:
            self.assertEqual(sys.stdout, stdout)
            self.assertEqual(sys.stderr, stderr)
            self.assertIn("ordinary output\n", stdout.getvalue())
            self.assertIn("error output\n", stderr.getvalue())
            self.assertIn("ordinary output\n", capture.snapshot())
            self.assertIn("error output\n", capture.snapshot())
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def test_capture_works_without_system_console_streams(self):
        capture = ConsoleCapture()
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        sys.stdout = None
        sys.stderr = None
        try:
            capture.install()
            sys.stdout.write("windowed output\n")
            sys.stderr.write("windowed error\n")
            capture.uninstall()
            self.assertIsNone(sys.stdout)
            self.assertIsNone(sys.stderr)
            self.assertIn("windowed output\n", capture.snapshot())
            self.assertIn("windowed error\n", capture.snapshot())
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


if __name__ == "__main__":
    unittest.main()
