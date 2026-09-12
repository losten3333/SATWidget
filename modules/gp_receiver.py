"""Локальный HTTP-приёмник GP-данных от расширения Chrome.

Расширение SATWidget GP Updater раз в заданный интервал скачивает GP-данные
(JSON-список OMM-записей) и отправляет их POST-запросом сюда. Приёмник
валидирует данные и атомарно записывает их в data/gp.json, откуда их читает
SatelliteManager.
"""
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class GpReceiver:
    """Слушает 127.0.0.1:<port> и принимает GP-данные по POST /gp."""

    def __init__(self, port=9090, cache_path="data/gp.json", host="127.0.0.1"):
        self.port = int(port)
        self.host = host
        self.cache = Path(cache_path)
        self._server = None
        self._thread = None
        self.last_status = None
        self.last_error = None
        # Опциональный колбэк, вызывается в потоке сервера после успешной
        # записи нового gp.json. Принимает количество записей.
        self.on_gp = None

    def set_on_gp(self, callback):
        """Устанавливает колбэк, вызываемый после каждой успешной записи GP."""
        self.on_gp = callback

    def _make_handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type")

            def log_message(self, fmt, *args):
                # Самописные логи ниже, здесь не дублируем.
                pass

            def _send(self, code, body=b"", content_type="application/json"):
                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.wfile.write(body)

            def do_OPTIONS(self):
                self._send(204, b"")

            def do_GET(self):
                """GET /status — служебный эндпоинт для проверки."""
                payload = json.dumps({
                    "status": receiver.last_status,
                    "error": receiver.last_error,
                    "cache": str(receiver.cache),
                }).encode("utf-8")
                self._send(200, payload)

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    length = 0
                try:
                    body = self.rfile.read(length)
                    data = json.loads(body.decode("utf-8"))
                except Exception as error:
                    receiver.last_status = "error"
                    receiver.last_error = f"bad body: {error}"
                    print(f"[gp_receiver] ошибка разбора тела: {error}")
                    self._send(400, json.dumps(
                        {"ok": False, "error": "invalid JSON"}).encode("utf-8"))
                    return

                if not isinstance(data, list) or not data:
                    receiver.last_status = "error"
                    receiver.last_error = "not a non-empty list"
                    print("[gp_receiver] данные не являются непустым списком")
                    self._send(400, json.dumps(
                        {"ok": False, "error": "expect non-empty list"}
                    ).encode("utf-8"))
                    return

                try:
                    receiver._write_cache(data)
                except Exception as error:
                    receiver.last_status = "error"
                    receiver.last_error = str(error)
                    print(f"[gp_receiver] ошибка записи кэша: {error}")
                    self._send(500, json.dumps(
                        {"ok": False, "error": str(error)}).encode("utf-8"))
                    return

                receiver.last_status = f"ok:{len(data)}"
                receiver.last_error = None
                print(f"[gp_receiver] сохранено {len(data)} записей в "
                      f"{receiver.cache}")
                self._send(200, json.dumps({"ok": True, "count": len(data)})
                           .encode("utf-8"))

                # Уведомляем приложение о новых данных (в потоке сервера).
                # Вызывающий код должен обеспечить переход в поток GUI.
                if receiver.on_gp is not None:
                    try:
                        receiver.on_gp(len(data))
                    except Exception as error:
                        print(f"[gp_receiver] ошибка в on_gp: {error}")

        return Handler

    def _write_cache(self, data):
        """Атомарная запись JSON в кэш: временный файл + os.replace."""
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.cache.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def start(self):
        """Запускает HTTP-сервер в фоновом потоке."""
        if self._thread is not None:
            return
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="gp-receiver",
        )
        self._thread.start()
        print(f"[gp_receiver] слушаю {self.host}:{self.port}, "
              f"кэш: {self.cache}")

    def stop(self):
        """Останавливает сервер."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            print("[gp_receiver] остановлен")

    @property
    def address(self):
        return f"http://{self.host}:{self.port}"
