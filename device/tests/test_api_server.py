"""Tests del servidor del API (aditum_gate.api.ApiServer).

Correr desde la raiz del repo (sin red):
    python3 -m unittest discover -s device/tests -t device -v
"""
import socket
import threading
import unittest
import urllib.request
from unittest import mock

from werkzeug.serving import ThreadedWSGIServer

from aditum_gate.api import ApiServer


def _app_ok(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


class ApiServerTest(unittest.TestCase):
    def test_servidor_estandar_resuelve_dns_entre_bind_y_listen(self):
        # Documenta el problema: HTTPServer.server_bind llama a getfqdn
        # (DNS inverso) antes del listen. Con DNS caido, el puerto queda
        # reservado sin escuchar y el proceso vivo.
        with mock.patch("socket.getfqdn", return_value="x") as getfqdn:
            srv = ThreadedWSGIServer("127.0.0.1", 0, _app_ok)
            srv.server_close()
        getfqdn.assert_called_once()

    def test_api_server_no_consulta_dns_y_atiende(self):
        with mock.patch("socket.getfqdn",
                        side_effect=AssertionError("getfqdn no debe llamarse")):
            srv = ApiServer("127.0.0.1", 0, _app_ok)
        try:
            host, port = srv.server_address[:2]
            self.assertEqual(srv.server_name, "127.0.0.1")
            self.assertEqual(srv.server_port, port)
            # Escucha de verdad: conexion + request completa
            with socket.create_connection((host, port), timeout=3):
                pass
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.read(), b"ok")
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
