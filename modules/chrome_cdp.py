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

    def _origin(self):
        """Возвращает Origin для запросов к debug-порту.

        Chrome DevTools отклоняет запросы с 403, если Origin не разрешён
        флагом --remote-allow-origins. Поэтому всегда передаём явный Origin.
        """
        parsed = urlparse(self.debug_url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 9222
        return "http://%s:%s" % (host, port)

    @staticmethod
    def _port_is_ready(port):
        """True, если на порту уже отвечает рабочий debug-порт Chrome (200)."""
        try:
            req = Request(
                "http://127.0.0.1:%s/json/version" % port, method="GET",
            )
            req.add_header("Origin", "http://127.0.0.1:%s" % port)
            with urlopen(req, timeout=1) as response:
                return getattr(response, "status", 200) == 200
        except Exception:
            return False

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

        Пытается занять настроенный порт. Если тот занят посторонним
        слушателем (антивирус/прокси/другой Chrome), который отвечает 403,
        или Chrome не становится готов — перескакивает на следующие
        свободные порты, пока один не заработает.
        """
        print("[chrome_cdp] _ensure_chrome_running: auto_launch=%s, "
              "_launch_attempted=%s" % (self.auto_launch, self._launch_attempted))
        if not self.auto_launch or self._launch_attempted:
            print("[chrome_cdp] _ensure_chrome_running: пропуск запуска")
            return False
        self._launch_attempted = True
        chrome = self._find_chrome()
        print("[chrome_cdp] _ensure_chrome_running: найден Chrome: %s" % chrome)
        if not chrome:
            print("[chrome_cdp] _ensure_chrome_running: Chrome не найден")
            return False
        profile_base = Path.home() / ".satwidget" / "chrome-debug-profile"
        profile_base.mkdir(parents=True, exist_ok=True)
        print("[chrome_cdp] _ensure_chrome_running: базовый профиль: %s"
              % profile_base)

        # Если на настроенном порту уже отвечает рабочий debug-порт Chrome,
        # используем его как есть (не запускаем свой экземпляр).
        configured_port = self._debug_port()
        if self._port_is_ready(configured_port):
            print("[chrome_cdp] _ensure_chrome_running: порт %s уже отвечает "
                  "рабочий debug-порт Chrome — используем его"
                  % configured_port)
            return True

        # Пробуем настроенный порт, затем следующие свободные.
        for port in list(range(configured_port, configured_port + 21)):
            if self._port_is_ready(port):
                print("[chrome_cdp] _ensure_chrome_running: порт %s уже "
                      "рабочий, используем его" % port)
                self._set_debug_port(port)
                return True
            # Свой профиль на порт — чтобы одичавший процесс, держащий
            # профиль-лок на одном порту, не мешал другим попыткам.
            profile = Path(
                str(profile_base) + "-%s" % port
            )
            profile.mkdir(parents=True, exist_ok=True)
            print("[chrome_cdp] _ensure_chrome_running: пытаюсь запустить "
                  "Chrome на порту %s (профиль %s)" % (port, profile))
            if self._launch_on_port(chrome, profile, port):
                self._set_debug_port(port)
                print("[chrome_cdp] _ensure_chrome_running: Chrome запущен и "
                      "работает на порту %s" % port)
                return True
            print("[chrome_cdp] _ensure_chrome_running: порт %s не "
                  "заработал — пробую следующий" % port)
        print("[chrome_cdp] _ensure_chrome_running: не удалось поднять "
              "Chrome ни на одном порту")
        return False

    def _set_debug_port(self, port):
        new = "http://127.0.0.1:%s" % port
        if new != self.debug_url:
            print("[chrome_cdp] _set_debug_port: %s -> %s"
                  % (self.debug_url, new))
            self.debug_url = new

    def _launch_on_port(self, chrome, profile, port) -> bool:
        """Запускает Chrome с отдельным профилем на заданном порту и ждёт,
        пока debug-порт станет готов. Возвращает True при успехе."""
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-web-security",
            "about:blank",
        ]
        print("[chrome_cdp] _launch_on_port: команда: %s" % " ".join(args))
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as error:
            print("[chrome_cdp] _launch_on_port: ОШИБКА Popen: %r" % error)
            return False
        self._chrome_process = proc
        print("[chrome_cdp] _launch_on_port: PID=%s" % proc.pid)
        deadline = time.monotonic() + 15
        last_err = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print("[chrome_cdp] _launch_on_port: процесс завершился, "
                      "код=%s" % proc.returncode)
            url = "http://127.0.0.1:%s/json/version" % port
            try:
                req = Request(url, method="GET")
                req.add_header("Origin", "http://127.0.0.1:%s" % port)
                with urlopen(req, timeout=1) as response:
                    if getattr(response, "status", 200) == 200:
                        print("[chrome_cdp] _launch_on_port: порт %s отвечает "
                              "OK" % port)
                        return True
            except Exception as error:
                last_err = error
                time.sleep(0.2)
        if last_err is not None:
            print("[chrome_cdp] _launch_on_port: таймаут 15с на порту %s, "
                  "последняя ошибка=%r" % (port, last_err))
        try:
            if proc.stderr is not None:
                err = proc.stderr.read()
                if err:
                    print("[chrome_cdp] _launch_on_port: stderr:\n%r" % err)
        except Exception as error:
            print("[chrome_cdp] _launch_on_port: чтение stderr ошибка %r"
                  % error)
        # Порт не заработал — гасим процесс, чтобы не накапливать
        # осиротевшие Chrome и не держать лок на профиле.
        try:
            if proc.poll() is None:
                proc.terminate()
                print("[chrome_cdp] _launch_on_port: процесс %s завершён "
                      "(порт не поднялся)" % proc.pid)
        except Exception:
            pass
        return False

    def _request(self, path, method="GET", timeout=3, launch=False):
        url = self.debug_url + path
        req = Request(url, method=method)
        # Chrome DevTools возвращает 403 без разрешённого Origin.
        req.add_header("Origin", self._origin())
        try:
            response = urlopen(req, timeout=timeout)
            status = getattr(response, "status", None)
            print("[chrome_cdp] _request: %s %s -> HTTP %s"
                  % (method, path, status))
            return response
        except Exception as error:
            print("[chrome_cdp] _request: %s %s -> ОШИБКА %r"
                  % (method, path, error))
            if hasattr(error, "code"):
                print("[chrome_cdp] _request: HTTP-код ошибки = %s" % error.code)
            if not launch or not self._ensure_chrome_running():
                raise
            print("[chrome_cdp] _request: повтор запроса после запуска Chrome")
            # debug_url мог смениться (fallback на свободный порт) —
            # пересобираем запрос на актуальный порт.
            url = self.debug_url + path
            req = Request(url, method=method)
            req.add_header("Origin", self._origin())
            return urlopen(req, timeout=timeout)

    def _close_page(self):
        if self._socket is not None:
            print("[chrome_cdp] _close_page: закрываю websocket")
            try:
                self._socket.close()
            except Exception as error:
                print("[chrome_cdp] _close_page: websocket.close() ошибка %r" % error)
            self._socket = None
        if self._tab_id is not None:
            try:
                with self._request("/json/close/" + self._tab_id, timeout=3):
                    pass
            except Exception as error:
                print("[chrome_cdp] _close_page: закрытие вкладки ошибка %r" % error)
            self._tab_id = None

    def _open_socket(self):
        if self._socket is not None:
            return
        try:
            from websocket import create_connection
            with self._request("/json/version", launch=True) as response:
                browser = json.load(response)
            ws_url = browser["webSocketDebuggerUrl"]
            print("[chrome_cdp] _open_socket: webSocketDebuggerUrl = %s" % ws_url)
            print("[chrome_cdp] _open_socket: версия Chrome: %s"
                  % browser.get("Browser"))
            self._socket = create_connection(ws_url, timeout=30)
            print("[chrome_cdp] _open_socket: websocket подключён OK")
        except Exception as error:
            print("[chrome_cdp] _open_socket: ОШИБКА %r" % error)
            raise RuntimeError(
                "Chrome DevTools недоступен. Запустите Chrome с параметром "
                "--remote-debugging-port=9222 и повторите обновление."
            ) from error

    def _command(self, method, params=None):
        self._open_socket()
        command_id = next(self._ids)
        payload = {
            "id": command_id,
            "method": method,
            "params": params or {},
        }
        print("[chrome_cdp] _command: #%s %s %s"
              % (command_id, method, json.dumps(params or {})))
        self._socket.send(json.dumps(payload))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                print("[chrome_cdp] _command: #%s %s -> ОШИБКА %r"
                      % (command_id, method, message["error"]))
                raise RuntimeError(message["error"].get("message", method))
            return message.get("result", {})

    def _evaluate(self, expression):
        display = expression if len(expression) < 200 else expression[:200] + "..."
        print("[chrome_cdp] _evaluate: %s" % display)
        result = self._command("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        details = result.get("exceptionDetails")
        if details:
            print("[chrome_cdp] _evaluate: исключение JS: %r"
                  % details.get("text"))
            raise RuntimeError(details.get("text", "Chrome JavaScript error"))
        value = result.get("result", {}).get("value")
        print("[chrome_cdp] _evaluate: результат (len=%s): %r"
              % (len(str(value)) if value is not None else 0,
                 (str(value)[:200] if value is not None else value)))
        return value

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
        print("[chrome_cdp] _open_page: открываю URL: %s" % url)
        self._close_page()
        try:
            with self._request(
                "/json/new", method="PUT", timeout=5, launch=True
            ) as response:
                tab = json.load(response)
            print("[chrome_cdp] _open_page: создана вкладка id=%s, url=%s"
                  % (tab.get("id"), tab.get("url")))
            from websocket import create_connection
            self._socket = create_connection(tab["webSocketDebuggerUrl"], timeout=60)
            self._tab_id = tab["id"]
            self._command("Runtime.enable")
            self._command("Page.enable")
            self._command("Network.enable")
            main_status = None
            main_req_id = None
            nav_error = None
            print("[chrome_cdp] _open_page: отправляю Page.navigate")
            self._socket.send(json.dumps({
                "id": next(self._ids),
                "method": "Page.navigate",
                "params": {"url": url},
            }))
            deadline = time.monotonic() + 30
            navigate_ok = False
            ready = None
            while time.monotonic() < deadline:
                self._socket.settimeout(0.5)
                try:
                    line = self._socket.recv()
                except Exception:
                    line = None
                if line:
                    try:
                        msg = json.loads(line)
                    except Exception:
                        msg = None
                    if msg is not None:
                        method = msg.get("method")
                        if method == "Network.responseReceived":
                            params = msg.get("params", {})
                            if params.get("type") == "Document":
                                main_req_id = params.get("requestId")
                                resp = params.get("response", {})
                                main_status = resp.get("status")
                                print("[chrome_cdp] _open_page: главный "
                                      "документ: HTTP %s, mime=%s, "
                                      "statusText=%s"
                                      % (main_status, resp.get("mimeType"),
                                         resp.get("statusText")))
                        elif method == "Network.loadingFailed":
                            params = msg.get("params", {})
                            if params.get("requestId") == main_req_id:
                                nav_error = params.get("errorText")
                                print("[chrome_cdp] _open_page: загрузка "
                                      "прервана: errorText=%s, blockReason=%s, "
                                      "canceled=%s"
                                      % (nav_error,
                                         params.get("blockedReason"),
                                         params.get("canceled")))
                        # ответ на нашу команду Page.navigate
                        if isinstance(msg.get("id"), int):
                            navigate_ok = True
                            if msg.get("error"):
                                print("[chrome_cdp] _open_page: Page.navigate "
                                      "ОШИБКА %r" % msg["error"])
                # Опрашиваем готовность страницы отдельной командой
                if navigate_ok:
                    try:
                        eval_id = next(self._ids)
                        self._socket.settimeout(1.0)
                        self._socket.send(json.dumps({
                            "id": eval_id,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": "document.readyState",
                                "returnByValue": True,
                            },
                        }))
                        while time.monotonic() < deadline:
                            ev = self._socket.recv()
                            em = json.loads(ev)
                            if em.get("id") == eval_id:
                                ready = (em.get("result", {})
                                        .get("result", {})
                                        .get("value"))
                                break
                    except Exception:
                        pass
                    if ready == "complete":
                        break
            print("[chrome_cdp] _open_page: navigate_ok=%s, readyState=%s"
                  % (navigate_ok, ready))
            if ready != "complete":
                raise TimeoutError("страница не загрузилась за отведённое время")
            text = self._evaluate(
                "document.body ? document.body.innerText : ''"
            )
            print("[chrome_cdp] _open_page: body длиной %s символов"
                  % len(text))
            print("[chrome_cdp] _open_page: начало body: %r"
                  % text[:300])
            # Резюме по главному ответу
            print("[chrome_cdp] _open_page: ИТОГО главный документ: "
                  "status=%s, request_id=%s, nav_error=%s"
                  % (main_status, main_req_id, nav_error))
            return text
        except Exception as error:
            print("[chrome_cdp] _open_page: ОШИБКА %r" % error)
            raise RuntimeError(f"Chrome не открыл страницу {url}: {error}") from error

    def get_text(self, url, params=None):
        return self._open_page(self.build_url(url, params))

    def space_track_json(self, base_url, query, username, password):
        print("[chrome_cdp] space_track_json: base=%s" % base_url)
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
        print("[chrome_cdp] space_track_json: результат логина: %r"
              % (login_result[:200] if login_result else login_result))
        if "failed" in (login_result or "").lower():
            raise RuntimeError("не удалось войти в Space-Track")
        data_url = base_url.rstrip("/") + "/" + query
        print("[chrome_cdp] space_track_json: запрашиваю GP: %s" % data_url)
        text = self.get_text(data_url)
        print("[chrome_cdp] space_track_json: GP длина=%s, начало=%r"
              % (len(text), text[:200]))
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
