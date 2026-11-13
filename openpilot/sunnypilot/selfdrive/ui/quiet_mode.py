"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from opendbc.car.structs import car

from openpilot.cereal import log
from openpilot.common.params import Params

AudibleAlert = car.CarControl.HUDControl.AudibleAlert

ALERTS_ALWAYS_PLAY = {
  AudibleAlert.warningSoft,
  AudibleAlert.warningImmediate,
  AudibleAlert.promptDistracted,
  AudibleAlert.promptRepeat,
}

# sunnydash: with the confidence-based early alert on, the distraction "Pay Attention" chime (preAlert, used only by
# driverDistracted1) must not be muted, or the early alert would be banner-only. preAlert only exists in the cereal enum
# that soundd uses (opendbc's copy stops at promptDistracted); the shared values are identical.
EARLY_ALERT_ALWAYS_PLAY = {
  log.SelfdriveState.AudibleAlert.preAlert,
}


class QuietMode:
  def __init__(self):
    self.params = Params()
    self.enabled: bool = self.params.get_bool("QuietMode")
    self.early_alert_enabled: bool = self.params.get_bool("DriverMonitorEarlyAlert")
    self._frame = 0

  def load_param(self) -> None:
    self._frame += 1
    if self._frame % 50 == 0:  # 2.5 seconds
      self.enabled = self.params.get_bool("QuietMode")
      self.early_alert_enabled = self.params.get_bool("DriverMonitorEarlyAlert")

  def should_play_sound(self, current_alert: int) -> bool:
    """
    Check if a sound should be played based on the Quiet Mode setting
    and the current alert.
    """
    if not self.enabled:
      return bool(current_alert != AudibleAlert.none)

    if self.early_alert_enabled and current_alert in EARLY_ALERT_ALWAYS_PLAY:
      return True

    return current_alert in ALERTS_ALWAYS_PLAY
