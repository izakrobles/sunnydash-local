"""
sunnydash: comma four screen for the on-device phone settings page.

In the car: turn on the page and the hotspot here, scan the first code to join the comma's Wi-Fi,
then scan the second code to open the page. At home: the second code points at the comma's address on your Wi-Fi.

The comma has one Wi-Fi radio, so it's either a hotspot or on your Wi-Fi, never both.
"""
import socket
import time

import pyray as rl

from openpilot.common.qrcode import make_texture
from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigParamControl, BigToggle, GreyBigButton
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.local_settings.server import PIN_PARAM, PORT, ensure_pin, new_pin
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.wifi_manager import DEFAULT_TETHERING_PASSWORD, TETHERING_IP_ADDRESS, WifiManager
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.label import UnifiedLabel
from openpilot.system.ui.widgets.scroller import NavScroller

ADDRESS_REFRESH_S = 3.


def wifi_ip() -> str | None:
  """The device's address on the Wi-Fi it's joined, if any. UDP connect sends nothing; it just picks a route."""
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
      s.connect(("10.255.255.255", 1))
      ip = s.getsockname()[0]
  except OSError:
    return None
  return None if ip.startswith("127.") or ip == TETHERING_IP_ADDRESS else ip


def hotspot_name(params) -> str:
  """Same name wifi_manager gives the hotspot: weedle-<first 4 of the dongle id>."""
  dongle_id = params.get("DongleId")
  return "weedle-" + dongle_id[:4] if dongle_id else "weedle"


def wifi_join_code(ssid: str, password: str) -> str:
  """Standard Wi-Fi QR payload; the iPhone and Android cameras offer to join the network."""
  def esc(s: str) -> str:
    return "".join("\\" + c if c in '\\;,:"' else c for c in s)
  return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


class CaptionedQR(Widget):
  QR_SIZE = 180

  def __init__(self, caption: str):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 400, self.QR_SIZE))
    self._data: str | None = None
    self._texture: rl.Texture | None = None
    self._label = UnifiedLabel(caption, font_size=40, font_weight=FontWeight.BOLD, text_color=rl.Color(255, 255, 255, 230),
                               line_height=1.0)

  def set_caption(self, caption: str):
    self._label.set_text(caption)

  def set_data(self, data: str):
    if data != self._data:
      self._data = data
      self._texture = make_texture(data, inverted=True)

  def _render(self, _):
    if self._texture:
      scale = self.QR_SIZE / self._texture.height
      rl.draw_texture_ex(self._texture, rl.Vector2(round(self._rect.x), round(self._rect.y)), 0.0, scale, rl.WHITE)
    text_x = self._rect.x + self.QR_SIZE + 24
    self._label.render(rl.Rectangle(text_x, self._rect.y, self._rect.width - self.QR_SIZE - 24, self.QR_SIZE))


class PhoneSettingsLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._wifi_manager = WifiManager()
    self._wifi_manager.set_active(False)

    self._enable = BigParamControl("phone settings page", "LocalSettingsEnabled", toggle_callback=self._on_toggle,
                                   description="Change sunnypilot settings from your phone's browser.\n" +
                                               "In the car: turn on the hotspot, scan code 1 to join it,\n" +
                                               "then code 2 to open the page.\n" +
                                               "At home: just scan code 2. No cloud involved.")
    self._hotspot = BigToggle("car hotspot", "", toggle_callback=self._on_hotspot,
                              description="Turns the comma into a Wi-Fi network your phone can join.\n" +
                                          "While it's on, the comma leaves your home Wi-Fi.\n" +
                                          "Change its password under network.")
    self._hotspot_info = GreyBigButton("", "")
    self._join_qr = CaptionedQR("1. scan to\njoin the\ncomma's wi-fi")
    self._page_qr = CaptionedQR("")
    self._pin = GreyBigButton("pin", "")
    self._new_pin = BigButton("new pin", "", description="Make a new PIN. Phones using the old one will be asked again.")
    self._new_pin.set_click_callback(self._on_new_pin)

    def page_on():
      return ui_state.params.get_bool("LocalSettingsEnabled")

    def hotspot_on():
      return page_on() and self._tethering

    for w in (self._hotspot, self._page_qr, self._pin, self._new_pin):
      w.set_visible(page_on)
    for w in (self._hotspot_info, self._join_qr):
      w.set_visible(hotspot_on)

    self._tethering = False
    self._hotspot_switching_until = 0.
    self._wifi_ip: str | None = None
    self._wifi_ip_t = 0.
    self._scroller.add_widgets([self._enable, self._hotspot, self._hotspot_info, self._join_qr, self._page_qr, self._pin,
                                self._new_pin])

  @staticmethod
  def _on_toggle(state: bool):
    if state:
      ensure_pin(ui_state.params)

  def _on_hotspot(self, state: bool):
    self._hotspot.set_enabled(False)
    self._hotspot_switching_until = time.monotonic() + 10.
    self._wifi_manager.set_tethering_active(state)

  @staticmethod
  def _on_new_pin():
    ui_state.params.put(PIN_PARAM, new_pin(), block=True)

  def show_event(self):
    super().show_event()
    self._enable.refresh()
    self._wifi_manager.set_active(True)
    gui_app.add_nav_stack_tick(self._wifi_manager.process_callbacks)

  def hide_event(self):
    super().hide_event()
    self._wifi_manager.set_active(False)
    gui_app.remove_nav_stack_tick(self._wifi_manager.process_callbacks)

  def _update_state(self):
    super()._update_state()
    now = time.monotonic()

    tethering = self._wifi_manager.is_tethering_active()
    if tethering != self._tethering or now > self._hotspot_switching_until:
      self._hotspot.set_enabled(True)
      self._hotspot_switching_until = 0.
    self._tethering = tethering
    self._hotspot.set_checked(tethering)

    if now - self._wifi_ip_t > ADDRESS_REFRESH_S:
      self._wifi_ip = wifi_ip()
      self._wifi_ip_t = now

    ssid = hotspot_name(ui_state.params)
    password = self._wifi_manager.tethering_password or DEFAULT_TETHERING_PASSWORD
    self._hotspot.set_value(ssid if tethering else "off")
    self._hotspot_info.set_text(f"wi-fi {ssid}")
    self._hotspot_info.set_value(f"pw {password}")
    self._join_qr.set_data(wifi_join_code(ssid, password))

    # Point the page code at whichever network the comma is actually on
    host = TETHERING_IP_ADDRESS if tethering or not self._wifi_ip else self._wifi_ip
    pin = ui_state.params.get(PIN_PARAM) or "------"
    self._page_qr.set_data(f"http://{host}:{PORT}/#pin={pin}")
    self._page_qr.set_caption(("2. " if tethering else "") + "scan to\nopen the\nsettings page" +
                              ("" if tethering or self._wifi_ip else "\n(turn on hotspot)"))
    self._pin.set_text(f"pin {pin}")
    self._pin.set_value(f"{host}:{PORT}")
