import http.client
import json
import threading

from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.local_settings import server
from openpilot.sunnypilot.local_settings.server import (LOCKOUT_DURATION_S, LOCKOUT_FAILURES, PIN_PARAM, PinGuard,
                                                        client_allowed, ensure_pin, host_allowed, make_server)

CAPS = {"brand": "toyota", "has_longitudinal_control": True, "device_type": "mici"}


class TestGuards(OpenpilotTestCase):
  def test_client_allowed(self):
    for ip in ("192.168.43.20", "10.0.0.5", "172.16.3.4", "127.0.0.1", "169.254.1.1", "fe80::1", "::ffff:192.168.1.2"):
      assert client_allowed(ip), ip
    for ip in ("8.8.8.8", "100.64.0.1", "2001:4860::1", "garbage"):
      assert not client_allowed(ip), ip

  def test_host_allowed(self):
    for host in ("192.168.43.1:8090", "192.168.43.1", "localhost:8090", "[fe80::1]:8090"):
      assert host_allowed(host), host
    for host in (None, "", "evil.example.com", "evil.example.com:8090", "192.168.43.1.nip.io:8090"):
      assert not host_allowed(host), host

  def test_pin_lockout(self):
    now = [0.]
    guard = PinGuard(clock=lambda: now[0])
    assert guard.check("123456", "123456")
    for _ in range(LOCKOUT_FAILURES):
      assert not guard.check("123456", "000000")
    assert guard.locked()
    assert not guard.check("123456", "123456")  # even the right PIN is refused while locked
    now[0] += LOCKOUT_DURATION_S + 1
    assert guard.check("123456", "123456")

  def test_ensure_pin(self):
    params = Params()
    pin = ensure_pin(params)
    assert len(pin) == 6 and pin.isdigit()
    assert ensure_pin(params) == pin
    params.put(PIN_PARAM, "abc", block=True)
    assert ensure_pin(params) != "abc"


class TestServer(OpenpilotTestCase):
  def setup_method(self):
    self.params = Params()
    self.params.put_bool("IsOffroad", True, block=True)
    self.srv = make_server(self.params, port=0, host="127.0.0.1", caps_fn=lambda _p: CAPS)
    self.port = self.srv.server_address[1]
    self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
    self.thread.start()
    self.pin = self.params.get(PIN_PARAM)

  def teardown_method(self):
    self.srv.shutdown()
    self.srv.server_close()

  def request(self, method, path, body=None, pin="default", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
    h = {"X-Pin": self.pin if pin == "default" else pin} if pin else {}
    if body is not None:
      h["Content-Type"] = "application/json"
    h.update(headers or {})
    conn.request(method, path, json.dumps(body) if body is not None else None, {k: v for k, v in h.items() if v is not None})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if resp.getheader("Content-Type", "").startswith("application/json") else data

  def test_page_is_public_but_api_needs_pin(self):
    status, body = self.request("GET", "/", pin=None)
    assert status == 200 and b"sunnypilot settings" in body
    assert self.request("GET", "/api/state", pin=None)[0] == 401
    assert self.request("GET", "/api/state", pin="000000" if self.pin != "000000" else "111111")[0] == 401
    assert self.request("GET", "/api/state")[0] == 200

  def test_rejects_named_host(self):
    assert self.request("GET", "/api/state", headers={"Host": "evil.example.com"})[0] == 403

  def test_set_and_refuse(self):
    status, body = self.request("POST", "/api/set", {"key": "QuietMode", "value": True})
    assert status == 200 and body["state"]["values"]["QuietMode"] is True
    assert self.params.get_bool("QuietMode")

    self.params.put_bool("IsOffroad", False, block=True)
    status, body = self.request("POST", "/api/set", {"key": "Mads", "value": False})
    assert status == 409 and "Park" in body["error"]

  def test_missing_offroad_flag_counts_as_driving(self):
    self.params.remove("IsOffroad")
    assert self.request("POST", "/api/set", {"key": "Mads", "value": False})[0] == 409

  def test_bad_requests(self):
    assert self.request("POST", "/api/set", {"value": True})[0] == 400
    assert self.request("POST", "/api/set", {"key": "QuietMode", "value": True}, headers={"Content-Type": "text/plain"})[0] == 415
    big = {"key": "QuietMode", "value": "x" * (server.MAX_BODY_BYTES + 10)}
    assert self.request("POST", "/api/set", big)[0] == 413
    assert self.request("GET", "/nope")[0] == 404

  def test_security_headers(self):
    conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in resp.getheader("Content-Security-Policy")
    assert resp.getheader("Access-Control-Allow-Origin") is None
