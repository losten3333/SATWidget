import unittest
from unittest import mock

from modules.chrome_cdp import ChromeCdpClient


class ChromeCdpTests(unittest.TestCase):
    def test_build_url_encodes_query_parameters(self):
        url = ChromeCdpClient.build_url(
            "https://celestrak.org/NORAD/elements/gp.php",
            {"GROUP": "active", "FORMAT": "json"},
        )

        self.assertEqual(
            url,
            "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json",
        )

    def test_debug_port_defaults_to_9222(self):
        client = ChromeCdpClient()
        self.assertEqual(client._debug_port(), 9222)

    def test_debug_port_parses_custom(self):
        client = ChromeCdpClient("http://127.0.0.1:9333")
        self.assertEqual(client._debug_port(), 9333)

    def test_find_chrome_uses_which_before_known_paths(self):
        with mock.patch("modules.chrome_cdp.shutil.which",
                        return_value=r"C:\custom\chrome.exe"):
            self.assertEqual(
                ChromeCdpClient._find_chrome(),
                r"C:\custom\chrome.exe",
            )

    def test_ensure_chrome_running_launches_and_waits_for_port(self):
        client = ChromeCdpClient()
        with mock.patch.object(
                ChromeCdpClient, "_find_chrome",
                return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ), mock.patch("modules.chrome_cdp.subprocess.Popen") as popen, \
                mock.patch(
                    "modules.chrome_cdp.urlopen",
                    side_effect=[OSError, mock.MagicMock()],
                ):
            self.assertTrue(client._ensure_chrome_running())
        args = popen.call_args[0][0]
        self.assertIn("--remote-debugging-port=9222", args)
        self.assertTrue(any(str(a).startswith("--user-data-dir=")
                            and "chrome-debug-profile" in a for a in args))

    def test_ensure_chrome_running_gives_up_when_port_stays_closed(self):
        client = ChromeCdpClient()
        with mock.patch.object(
                ChromeCdpClient, "_find_chrome",
                return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ), mock.patch("modules.chrome_cdp.subprocess.Popen"), \
                mock.patch(
                    "modules.chrome_cdp.urlopen",
                    side_effect=OSError,
                ), mock.patch("modules.chrome_cdp.time.monotonic",
                              side_effect=[0.0, 15.1]):
            self.assertFalse(client._ensure_chrome_running())

    def test_ensure_chrome_running_respects_auto_launch_off(self):
        client = ChromeCdpClient(auto_launch=False)
        with mock.patch.object(ChromeCdpClient, "_find_chrome") as find:
            self.assertFalse(client._ensure_chrome_running())
            find.assert_not_called()

    def test_request_launches_chrome_on_first_failure(self):
        client = ChromeCdpClient()
        with mock.patch.object(
                ChromeCdpClient, "_ensure_chrome_running", return_value=True,
        ), mock.patch(
            "modules.chrome_cdp.urlopen",
            side_effect=[OSError("refused"), mock.MagicMock()],
        ):
            self.assertIsNotNone(client._request("/json/version", launch=True))

    def test_request_does_not_launch_chrome_when_not_requested(self):
        client = ChromeCdpClient()
        with mock.patch.object(
                ChromeCdpClient, "_ensure_chrome_running"
        ) as ensure, mock.patch(
            "modules.chrome_cdp.urlopen", side_effect=OSError("refused"),
        ):
            with self.assertRaises(OSError):
                client._request("/json/version", launch=False)
            ensure.assert_not_called()

    def test_close_terminates_launched_chrome_and_resets_state(self):
        client = ChromeCdpClient()
        process = mock.MagicMock()
        process.poll.return_value = None
        client._chrome_process = process
        client._launch_attempted = True

        with mock.patch.object(client, "_close_page") as close_page, \
                mock.patch("modules.chrome_cdp.os.name", "posix"):
            client.close()

        close_page.assert_called_once()
        process.terminate.assert_called_once()
        self.assertIsNone(client._chrome_process)
        self.assertFalse(client._launch_attempted)

    def test_close_uses_taskkill_on_windows(self):
        client = ChromeCdpClient()
        process = mock.MagicMock()
        process.pid = 4242
        process.poll.return_value = None
        client._chrome_process = process

        with mock.patch.object(client, "_close_page"), \
                mock.patch("modules.chrome_cdp.os.name", "nt"), \
                mock.patch("modules.chrome_cdp.subprocess.run") as run:
            client.close()
        run.assert_called_once()
        self.assertIn("/T", run.call_args[0][0])
        self.assertIn("4242", run.call_args[0][0])

    def test_close_does_not_terminate_external_chrome(self):
        client = ChromeCdpClient()
        with mock.patch.object(client, "_close_page"), \
                mock.patch.object(client, "_stop_chrome") as stop:
            client.close()
        stop.assert_called_once()
        self.assertIsNone(client._chrome_process)

    def test_close_ignores_already_exited_process(self):
        client = ChromeCdpClient()
        process = mock.MagicMock()
        process.poll.return_value = 0
        client._chrome_process = process

        with mock.patch.object(client, "_close_page"):
            client.close()
        process.terminate.assert_not_called()

    def test_ensure_chrome_running_again_after_close(self):
        client = ChromeCdpClient()
        first = mock.MagicMock()
        first.poll.return_value = 0
        with mock.patch.object(
                ChromeCdpClient, "_find_chrome",
                return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ), mock.patch(
            "modules.chrome_cdp.subprocess.Popen", return_value=first,
        ), mock.patch("modules.chrome_cdp.urlopen", return_value=mock.MagicMock()):
            self.assertTrue(client._ensure_chrome_running())

        with mock.patch.object(client, "_close_page"):
            client.close()

        second = mock.MagicMock()
        second.poll.return_value = None
        with mock.patch.object(
                ChromeCdpClient, "_find_chrome",
                return_value=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ), mock.patch(
            "modules.chrome_cdp.subprocess.Popen", return_value=second,
        ), mock.patch("modules.chrome_cdp.urlopen", return_value=mock.MagicMock()):
            self.assertTrue(client._ensure_chrome_running())
        self.assertIs(client._chrome_process, second)


if __name__ == "__main__":
    unittest.main()
