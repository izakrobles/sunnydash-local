import pytest

from openpilot.sunnypilot.private_dashcam import cloud_block
from openpilot.sunnypilot.private_dashcam.cloud_block import CLOUD_BLOCK_PARAM, CLOUD_PROCESSES, CloudBlockedError, cloud_blocked


class FakeParams:
  def __init__(self, values=None, raise_on_get=False):
    self.values = values or {}
    self.raise_on_get = raise_on_get

  def get_bool(self, key):
    if self.raise_on_get:
      raise RuntimeError("params unavailable")
    return bool(self.values.get(key, False))


def test_cloud_blocked_reads_param():
  assert cloud_blocked(FakeParams({CLOUD_BLOCK_PARAM: True}))
  assert not cloud_blocked(FakeParams({CLOUD_BLOCK_PARAM: False}))


def test_cloud_blocked_fails_closed():
  assert cloud_blocked(FakeParams(raise_on_get=True))


def test_blocked_error_is_handled_like_a_network_failure():
  # callers (UI pollers, navd, pigeond) already catch requests' connection errors
  import requests
  assert issubclass(CloudBlockedError, requests.exceptions.ConnectionError)
  assert issubclass(CloudBlockedError, requests.exceptions.RequestException)


def test_cloud_processes_cover_comma_and_sunnylink():
  assert {"manage_athenad", "uploader"} <= CLOUD_PROCESSES
  assert {"manage_sunnylinkd", "sunnylink_registration_manager", "statsd_sp", "backup_manager", "sunnylink_uploader"} <= CLOUD_PROCESSES
  # the private uploader and third-party fetchers must never be gated
  assert not CLOUD_PROCESSES & {"private_dashcam_uploader", "updated", "models_manager", "mapd_manager", "pigeond"}


def test_base_api_refuses_when_blocked(monkeypatch):
  base = pytest.importorskip("openpilot.common.api.base")
  monkeypatch.setattr(cloud_block, "cloud_blocked", lambda params=None: True)

  def no_network(*args, **kwargs):
    raise AssertionError("request was sent while cloud is blocked")
  monkeypatch.setattr(base.requests, "request", no_network)

  api = base.BaseApi.__new__(base.BaseApi)
  api.api_host = "https://api.example.invalid"
  api.user_agent = "test-"
  with pytest.raises(CloudBlockedError):
    api.api_get("v1/me")


def test_base_api_passes_through_when_not_blocked(monkeypatch):
  base = pytest.importorskip("openpilot.common.api.base")
  monkeypatch.setattr(cloud_block, "cloud_blocked", lambda params=None: False)
  monkeypatch.setattr(base, "get_version", lambda: "0.0")

  calls = []
  monkeypatch.setattr(base.requests, "request", lambda method, url, **kw: calls.append((method, url)) or "resp")

  api = base.BaseApi.__new__(base.BaseApi)
  api.api_host = "https://api.example.invalid"
  api.user_agent = "test-"
  assert api.api_get("v1/me") == "resp"
  assert calls == [("GET", "https://api.example.invalid/v1/me")]
