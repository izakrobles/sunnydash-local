"""
sunnydash: small eye badge next to the driver monitoring icon showing which distraction-alert timing is active.
  orange eye: quick, scene confidence is below the threshold, a look away chimes right away
  white eye:  relaxed, scene confidence is at or above the threshold, chimes come at 30, 45 and 60 seconds
Hidden when the early alert setting is off (stock timing).
"""
import pyray as rl

from openpilot.cereal import custom
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets import Widget

EarlyAlertMode = custom.DriverMonitoringStateSP.AlertMode

QUICK_COLOR = (255, 115, 0)     # same orange as the driver monitoring cone
RELAXED_COLOR = (255, 255, 255)


class EarlyAlertBadge(Widget):
  SIZE = 44
  EYE_SIZE = 26

  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, self.SIZE, self.SIZE))
    self._eye = gui_app.texture("icons_mici/onroad/eye_fill.png", self.EYE_SIZE, self.EYE_SIZE)
    self._mode = EarlyAlertMode.off
    self._show = False
    self._fade_filter = FirstOrderFilter(0.0, 0.05, 1 / gui_app.target_fps)
    self._quick_filter = FirstOrderFilter(0.0, 0.05, 1 / gui_app.target_fps)  # 1.0 = full orange

  def set_show(self, show: bool):
    self._show = show

  @property
  def mode(self):
    return self._mode

  def _update_state(self):
    sm = ui_state.sm
    if sm.recv_frame["driverMonitoringStateSP"] > ui_state.started_frame:
      self._mode = sm["driverMonitoringStateSP"].alertMode
    else:
      self._mode = EarlyAlertMode.off

    visible = self._show and self._mode != EarlyAlertMode.off
    self._fade_filter.update(1.0 if visible else 0.0)
    self._quick_filter.update(1.0 if self._mode == EarlyAlertMode.quick else 0.0)

  def _render(self, _):
    alpha = self._fade_filter.x
    if alpha < 0.01:
      return

    center = rl.Vector2(self._rect.x + self.SIZE / 2, self._rect.y + self.SIZE / 2)
    rl.draw_circle_v(center, self.SIZE / 2, rl.Color(0, 0, 0, int(255 * 0.45 * alpha)))

    t = self._quick_filter.x
    r, g, b = (round(RELAXED_COLOR[i] + (QUICK_COLOR[i] - RELAXED_COLOR[i]) * t) for i in range(3))
    eye_alpha = 0.6 + 0.35 * t  # relaxed is a calm, dimmer white; quick is a strong orange
    pos = rl.Vector2(center.x - self.EYE_SIZE / 2, center.y - self.EYE_SIZE / 2)
    rl.draw_texture_v(self._eye, pos, rl.Color(r, g, b, int(255 * eye_alpha * alpha)))
