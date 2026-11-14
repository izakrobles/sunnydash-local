"""
sunnydash: comma four screen for the on-device phone settings page.
Turn it on, join the device's hotspot from your phone, scan the code (or type the address and PIN).
"""
import socket
import time

import pyray as rl

from openpilot.common.qrcode import make_texture
from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigParamControl, GreyBigButton
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.local_settings.server import PIN_PARAM, PORT, ensure_pin, new_pin
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.scroller import NavScroller

HOTSPOT_IP = "192.168.43.1"  # matches wifi_manager.TETHERING_IP_ADDRESS
ADDRESS_REFRESH_S = 3.


def wifi_ip() -> str | None:
  """The device's address on the Wi-Fi it's joined, if any. UDP connect sends nothing; it just picks a route."""
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      s.connect(("10.255.255.255", 1))
      ip = s.getsockname()[0]
  except OSError:
    return None
  return None if ip.startswith("127.") or ip == HOTSPOT_IP else ip


class QRCode(Widget):
  SIZE = 180

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, self.SIZE, self.SIZE))
    self._data: str | None = None
    self._texture: rl.Texture | None = None

  def set_data(self, data: str):
    if data != self._data:
      self._data = data
      self._texture = make_texture(data, inverted=True)

  def _render(self, _):
    if self._texture:
      scale = self.SIZE / self._texture.height
      rl.draw_texture_ex(self._texture, rl.Vector2(round(self._rect.x), round(self._rect.y)), 0.0, scale, rl.WHITE)


class PhoneSettingsLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._enable = BigParamControl("phone settings page", "LocalSettingsEnabled", toggle_callback=self._on_toggle,
                                   description="Change sunnypilot settings from your phone's browser.\n" +
                                               "Turn on tethering under network, join the hotspot\n" +
                                               "from your phone, then scan the code.\n" +
                                               "Works on your home Wi-Fi too. No cloud involved.")
    self._qr = QRCode()
    self._hotspot = GreyBigButton("pin", "")
    self._wifi = GreyBigButton("on wi-fi", "")
    self._new_pin = BigButton("new pin", "", description="Make a new PIN. Phones using the old one will be asked again.")
    self._new_pin.set_click_callback(self._on_new_pin)

    for w in (self._qr, self._hotspot, self._wifi, self._new_pin):
      w.set_visible(lambda: ui_state.params.get_bool("LocalSettingsEnabled"))

    self._wifi_ip: str | None = None
    self._wifi_ip_t = 0.
    self._scroller.add_widgets([self._enable, self._qr, self._hotspot, self._wifi, self._new_pin])

  @staticmethod
  def _on_toggle(state: bool):
    if state:
      ensure_pin(ui_state.params)

  @staticmethod
  def _on_new_pin():
    ui_state.params.put(PIN_PARAM, new_pin(), block=True)

  def show_event(self):
    super().show_event()
    self._enable.refresh()

  def _update_state(self):
    super()._update_state()
    pin = ui_state.params.get(PIN_PARAM) or "------"
    self._qr.set_data(f"http://{HOTSPOT_IP}:{PORT}/#pin={pin}")
    self._hotspot.set_text(f"pin {pin}")
    self._hotspot.set_value(f"hotspot {HOTSPOT_IP}:{PORT}")

    now = time.monotonic()
    if now - self._wifi_ip_t > ADDRESS_REFRESH_S:
      self._wifi_ip = wifi_ip()
      self._wifi_ip_t = now
    self._wifi.set_value(f"{self._wifi_ip}:{PORT}" if self._wifi_ip else "not connected")
