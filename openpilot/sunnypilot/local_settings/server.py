#!/usr/bin/env python3
"""sunnydash: phone settings page served by the device itself.

Join the device's hotspot (or the same Wi-Fi) and open http://192.168.43.1:8090 to change sunnypilot settings without
sunnylink or any cloud service.

Guardrails:
  - only answers clients on private/local networks, and only when addressed by IP (blocks DNS-rebinding tricks)
  - every API call needs the 6-digit PIN shown on the device; repeated wrong PINs lock the page for a few minutes
  - every write goes through Settings.apply, which enforces the same rules the device UI and sunnylink use
"""
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.local_settings.settings import Settings, SettingError

PORT = 8090
PIN_PARAM = "LocalSettingsPin"
PIN_LENGTH = 6
MAX_BODY_BYTES = 4096
LOCKOUT_FAILURES = 8          # wrong PINs within LOCKOUT_WINDOW_S before the page locks
LOCKOUT_WINDOW_S = 300.
LOCKOUT_DURATION_S = 300.
CAPS_CACHE_S = 5.

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; " +
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")


def new_pin() -> str:
  return f"{secrets.randbelow(10 ** PIN_LENGTH):0{PIN_LENGTH}d}"


def ensure_pin(params) -> str:
  pin = params.get(PIN_PARAM)
  if not pin or len(pin) != PIN_LENGTH or not pin.isdigit():
    pin = new_pin()
    params.put(PIN_PARAM, pin, block=True)
  return pin


def client_allowed(address: str) -> bool:
  try:
    ip = ipaddress.ip_address(address.split("%", maxsplit=1)[0])
  except ValueError:
    return False
  if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
    ip = ip.ipv4_mapped
  return ip.is_private or ip.is_loopback or ip.is_link_local


def host_allowed(host: str | None) -> bool:
  """Only accept requests addressed to an IP (or localhost), so a website can't rebind its own name to the device."""
  if not host:
    return False
  if host.startswith("["):  # [ipv6]:port
    hostname = host[1:host.find("]")] if "]" in host else ""
  elif host.count(":") == 1:  # name:port or ipv4:port
    hostname = host.rsplit(":", 1)[0]
  else:
    hostname = host
  if hostname == "localhost":
    return True
  try:
    ipaddress.ip_address(hostname)
    return True
  except ValueError:
    return False


class PinGuard:
  def __init__(self, clock=time.monotonic):
    self._clock = clock
    self._failures: list[float] = []
    self._locked_until = 0.
    self._lock = threading.Lock()

  def locked(self) -> bool:
    with self._lock:
      return self._clock() < self._locked_until

  def check(self, expected: str | None, given: str | None) -> bool:
    with self._lock:
      now = self._clock()
      if now < self._locked_until:
        return False
      if expected and given and hmac.compare_digest(expected.encode(), given.encode()):
        self._failures.clear()
        return True
      self._failures = [t for t in self._failures if now - t < LOCKOUT_WINDOW_S] + [now]
      if len(self._failures) >= LOCKOUT_FAILURES:
        self._locked_until = now + LOCKOUT_DURATION_S
        self._failures.clear()
        cloudlog.warning("local_settings: too many wrong PINs, locking")
      return False


class DeviceState:
  """Live inputs for the rules: car capabilities (cached), onroad and engaged."""
  def __init__(self, params, caps_fn=None):
    self.params = params
    self._caps_fn = caps_fn
    self._caps: dict = {}
    self._caps_t = -CAPS_CACHE_S

  def caps(self) -> dict:
    now = time.monotonic()
    if now - self._caps_t > CAPS_CACHE_S:
      try:
        if self._caps_fn is None:
          from openpilot.sunnypilot.sunnylink.capabilities import generate_capabilities
          self._caps_fn = generate_capabilities
        self._caps = self._caps_fn(self.params)
      except Exception:
        cloudlog.exception("local_settings: failed to generate capabilities")
      self._caps_t = now
    return self._caps

  def onroad(self) -> bool:
    # IsOffroad is written by the manager; if it's missing, assume onroad so offroad-only settings stay locked
    return not self.params.get_bool("IsOffroad")

  def engaged(self) -> bool:
    return self.params.get_bool("IsEngaged")


def make_handler(settings: Settings, device: DeviceState, guard: PinGuard, page: bytes):
  class Handler(BaseHTTPRequestHandler):
    server_version = "sunnydash"
    sys_version = ""

    def log_message(self, fmt, *args):
      pass

    def _send(self, status: int, body: bytes, content_type: str):
      self.send_response(status)
      self.send_header("Content-Type", content_type)
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.send_header("X-Frame-Options", "DENY")
      self.send_header("X-Content-Type-Options", "nosniff")
      self.send_header("Referrer-Policy", "no-referrer")
      self.send_header("Content-Security-Policy", CSP)
      self.end_headers()
      self.wfile.write(body)

    def _json(self, status: int, payload):
      self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json")

    def _allowed(self) -> bool:
      if not client_allowed(self.client_address[0]) or not host_allowed(self.headers.get("Host")):
        self._json(403, {"error": "Not available from this network"})
        return False
      return True

    def _authorized(self) -> bool:
      if guard.locked():
        self._json(429, {"error": "Too many wrong PINs. Try again in a few minutes."})
        return False
      if not guard.check(settings.params.get(PIN_PARAM), self.headers.get("X-Pin")):
        self._json(401, {"error": "Wrong PIN"})
        return False
      return True

    def _state(self):
      return settings.state(device.caps(), device.onroad(), device.engaged())

    def do_GET(self):
      if not self._allowed():
        return
      path = self.path.split("?", 1)[0]
      if path in ("/", "/index.html"):
        self._send(200, page, "text/html; charset=utf-8")
      elif path == "/api/schema":
        if self._authorized():
          self._json(200, settings.page_schema(device.caps()))
      elif path == "/api/state":
        if self._authorized():
          self._json(200, self._state())
      else:
        self._json(404, {"error": "Not found"})

    def do_POST(self):
      if not self._allowed():
        return
      if self.path.split("?", 1)[0] != "/api/set":
        self._json(404, {"error": "Not found"})
        return
      if not self._authorized():
        return
      if not (self.headers.get("Content-Type") or "").startswith("application/json"):
        self._json(415, {"error": "Expected JSON"})
        return
      try:
        length = int(self.headers.get("Content-Length") or 0)
      except ValueError:
        length = -1
      if length <= 0 or length > MAX_BODY_BYTES:
        self._json(413, {"error": "Bad request size"})
        return
      try:
        body = json.loads(self.rfile.read(length))
        key, value = body["key"], body["value"]
        if not isinstance(key, str):
          raise ValueError
      except (ValueError, KeyError, TypeError):
        self._json(400, {"error": "Bad request"})
        return

      try:
        settings.apply(key, value, device.caps(), device.onroad(), device.engaged())
      except SettingError as e:
        self._json(e.status, {"error": e.message, "state": self._state()})
        return
      except Exception:
        cloudlog.exception(f"local_settings: failed to set {key}")
        self._json(500, {"error": "Couldn't save that setting"})
        return

      cloudlog.info(f"local_settings: {key} changed from phone page")
      self._json(200, {"ok": True, "state": self._state()})

  return Handler


def make_server(params=None, port: int = PORT, host: str = "0.0.0.0", caps_fn=None, schema=None):
  params = params or Params()
  ensure_pin(params)
  with open(PAGE_PATH, "rb") as f:
    page = f.read()
  handler = make_handler(Settings(params, schema), DeviceState(params, caps_fn), PinGuard(), page)
  server = ThreadingHTTPServer((host, port), handler)
  server.daemon_threads = True
  return server


def main():
  server = make_server()
  cloudlog.info(f"local_settings: serving on port {server.server_address[1]}")
  server.serve_forever()


if __name__ == "__main__":
  main()
