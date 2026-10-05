import sys
from threading import RLock

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout


class _TeeStream:
    """Copies a standard stream to the original destination and the UI log."""

    def __init__(self, destination, capture):
        self._destination = destination
        self._capture = capture

    @property
    def encoding(self):
        return getattr(self._destination, "encoding", "utf-8")

    def write(self, text):
        if not text:
            return 0
        self._destination.write(text)
        self._capture.append(text)
        return len(text)

    def flush(self):
        self._destination.flush()

    def isatty(self):
        return self._destination.isatty()

    def fileno(self):
        return self._destination.fileno()

    def writable(self):
        return True


class ConsoleCapture(QObject):
    """Retains application stdout/stderr and notifies an optional console UI."""

    text_written = Signal(str)

    def __init__(self):
        super().__init__()
        self._lock = RLock()
        self._chunks = []
        self._original_stdout = None
        self._original_stderr = None

    def append(self, text: str) -> None:
        with self._lock:
            self._chunks.append(text)
        self.text_written.emit(text)

    def snapshot(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def install(self) -> None:
        if self._original_stdout is not None:
            return
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = _TeeStream(self._original_stdout, self)
        sys.stderr = _TeeStream(self._original_stderr, self)

    def uninstall(self) -> None:
        if self._original_stdout is None:
            return
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self._original_stdout = None
        self._original_stderr = None


class ConsoleWindow(QDialog):
    """A hideable live view over ConsoleCapture's retained output."""

    def __init__(self, capture: ConsoleCapture, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Консоль SAT Widget")
        self.resize(760, 440)

        self.output = QPlainTextEdit(self)
        self.output.setReadOnly(True)
        self.output.setPlainText(capture.snapshot())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.output)
        capture.text_written.connect(self.append_text)
        self._scroll_to_end()

    def append_text(self, text: str) -> None:
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(text)
        self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        scrollbar = self.output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
