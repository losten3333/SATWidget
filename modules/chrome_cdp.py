"""Retrieves data through an already open Google Chrome DevTools session."""
import json
import os
import shutil
import subprocess
import time
from itertools import count
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class ChromeCdpClient:
    """Makes external requests from Chrome rather than the Python process."""

    def __init__(self, debug_url="http://127.0.0.1:9222", auto_launch=True):
        self.debug_url = debug_url.rstrip("/")
        self.auto_launch = auto_launch
        self._socket = None
        self._tab_id = None
        self._ids = count(1)
        self._launch_attempted = False
        self._chrome_process = None

    def _debug_port(self):
        return urlparse(self.debug_url).port or 9222

    @staticmethod
    def _find_chrome():
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(
                Path.home()
                / r"AppData\Local\Google\Chrome\Application\chrome.exe"
            ),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        for name in ("chrome", "google-chrome", "chromium"):
            found = shutil.which(name)
            if found:
                return found
        return next((p for p in candidates if Path(p).exists()), None)

    def _ensure_chrome_running(self) -> bool:
        """Запускает Chrome с портом отладки и отдельным профилем.

        С Chrome 136 флаг --remote-debugging-port игнорируется, если
        используется профиль по умолчанию, поэтому запускаем свой
        экземпляр с --user-data-dir в ~/.satwidget.
        """
        if not self.auto_launch or self._launch_attempted:
            return False
        self._launch_attempted = True
        chrome = self._find_chrome()
        if not chrome:
            return False
        profile = Path.home() / ".satwidget" / "chrome-debug-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            self._chrome_process = subprocess.Popen([
                chrome,
                f"--remote-debugging-port={self._debug_port()}",
                f"--user-data-dir={profile}",
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ])
        except Exception:
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urlopen(self.debug_url + "/json/version", timeout=1):
                    return True
            except Exception:
                time.sleep(0.2)
        return False

    def _request(self, path, method="GET", timeout=3, launch=False):
        url = self.debug_url + path
        try:
            return urlopen(Request(url, method=method), timeout=timeout)
        except Exception as error:
            if not launch or not self._ensure_chrome_running():
                raise
            return urlopen(Request(url, method=method), timeout=timeout)

    def _close_page(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._tab_id is not None:
            try:
                with self._request("/json/close/" + self._tab_id, timeout=3):
                    pass
            except Exception:
                pass
            self._tab_id = None

    def _open_socket(self):
        if self._socket is not None:
            return
        try:
            from websocket import create_connection
            with self._request("/json/version", launch=True) as response:
                browser = json.load(response)
            self._socket = create_connection(
                browser["webSocketDebuggerUrl"], timeout=30
            )
        except Exception as error:
            raise RuntimeError(
                "Chrome DevTools недоступен. Запустите Chrome с параметром "
                "--remote-debugging-port=9222 и повторите обновление."
            ) from error

    def _command(self, method, params=None):
        self._open_socket()
        command_id = next(self._ids)
        self._socket.send(json.dumps({
            "id": command_id,
            "method": method,
            "params": params or {},
        }))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"].get("message", method))
            return message.get("result", {})

    def _evaluate(self, expression):
        result = self._command("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        details = result.get("exceptionDetails")
        if details:
            raise RuntimeError(details.get("text", "Chrome JavaScript error"))
        return result.get("result", {}).get("value")

    @staticmethod
    def build_url(url, params=None):
        if not params:
            return url
        return url + ("&" if "?" in url else "?") + urlencode(params)

    def _wait_for_load(self, timeout=30) -> bool:
        """Ждёт завершения загрузки страницы после Page.navigate."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._evaluate("document.readyState") == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def _open_page(self, url):
        self._close_page()
        try:
            with self._request(
                "/json/new", method="PUT", timeout=5, launch=True
            ) as response:
                tab = json.load(response)
            from websocket import create_connection
            self._socket = create_connection(tab["webSocketDebuggerUrl"], timeout=60)
            self._tab_id = tab["id"]
            self._command("Runtime.enable")
            self._command("Page.navigate", {"url": url})
            if not self._wait_for_load():
                raise TimeoutError("страница не загрузилась за отведённое время")
            return self._evaluate("document.body ? document.body.innerText : ''")
        except Exception as error:
            raise RuntimeError(f"Chrome не открыл страницу {url}: {error}") from error

    def get_text(self, url, params=None):
        return self._open_page(self.build_url(url, params))

    def space_track_json(self, base_url, query, username, password):
        self._open_page(base_url)
        login = json.dumps({"identity": username, "password": password})
        login_result = self._evaluate(
            "(async () => {"
            "const r = await fetch('/ajaxauth/login', {"
            "method: 'POST', headers: {'Content-Type': "
            "'application/x-www-form-urlencoded'}, "
            f"body: new URLSearchParams({login})}});"
            "return await r.text();"
            "})()"
        )
        if "failed" in (login_result or "").lower():
            raise RuntimeError("не удалось войти в Space-Track")
        text = self.get_text(base_url.rstrip("/") + "/" + query)
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("Space-Track вернул некорректный GP")
        return text

    def _stop_chrome(self):
        """Закрывает Chrome, если он был запущен этим клиентом."""
        process = self._chrome_process
        self._chrome_process = None
        self._launch_attempted = False
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                )
            else:
                process.terminate()
        except Exception:
            pass

    def close(self):
        self._close_page()
        self._stop_chrome()
