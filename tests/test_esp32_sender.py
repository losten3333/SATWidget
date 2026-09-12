import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from modules.esp32_sender import Esp32Sender


class Esp32SnapshotTests(unittest.TestCase):
    def test_empty_snapshot_is_a_valid_clear_command(self):
        sender = Esp32Sender(minutes=1)

        with patch("modules.esp32_sender.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(2026, 8, 30, 18, 36)
            self.assertEqual(
                sender._snapshot_lines([]),
                ["BEGIN", "CONFIG|1", "TIME|18:36", "END"],
            )

    def test_image_transfer_uses_fast_method_when_transport_supports_it(self):
        class Transport:
            def __init__(self):
                self.fast_lines = []
                self.started = False
                self.ended = False

            def write(self, data):
                if data.startswith(b"IMG_BEGIN|"):
                    self.started = True
                elif data == b"IMG_END\n":
                    self.ended = True
                else:
                    raise AssertionError("ordinary writes must not be used for image data")

            def flush(self):
                pass

            def readline(self):
                if self.ended:
                    return b"IMG_OK|53370\n"
                if self.started and not self.fast_lines:
                    return b"IMG_READY|53370\n"
                return b"IMG_NEXT\n"

            def write_image_data(self, data):
                self.fast_lines.append(data)

        sender = Esp32Sender()
        transport = Transport()
        image = bytes(150 * 150 * 2)
        sender._send_image(transport, "53370", image)

        self.assertGreater(len(transport.fast_lines), 1)

    def test_ble_send_retries_once_after_a_transient_failure(self):
        sender = Esp32Sender()
        sender._ble_retry_delay = 0
        calls = []

        def send_once(_records):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("device is restarting advertising")
            return "OK|1|1"

        sender._send_ble_once = send_once

        self.assertEqual(sender._send_ble([]), "OK|1|1")
        self.assertEqual(len(calls), 2)

    def test_template_is_temporary_and_real_image_replaces_it(self):
        with TemporaryDirectory() as directory:
            images = Path(directory)
            template = bytes([1]) * (150 * 150 * 2)
            real = bytes([2]) * (150 * 150 * 2)
            (images / "template.rgb565").write_bytes(template)

            with patch("modules.esp32_sender.IMAGE_DIRECTORY", images):
                sender = Esp32Sender()
                temporary, is_temporary, is_template = sender._image_bytes_for_record(
                    {"norad": "12345", "color": "#000000"}
                )
                self.assertEqual(temporary, template)
                self.assertTrue(is_temporary)
                self.assertTrue(is_template)

                (images / "12345.rgb565").write_bytes(real)
                image, is_temporary, is_template = sender._image_bytes_for_record(
                    {"norad": "12345", "color": "#000000"}
                )
                self.assertEqual(image, real)
                self.assertFalse(is_temporary)
                self.assertFalse(is_template)


if __name__ == "__main__":
    unittest.main()
