from openpilot.cereal import log

from openpilot.system.ui.widgets.scroller import NavScroller
from openpilot.selfdrive.ui.mici.widgets.button import BigButton, BigToggle, BigParamControl, BigMultiParamToggle
from openpilot.system.ui.lib.application import gui_app
from openpilot.selfdrive.ui.layouts.settings.common import restart_needed_callback
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.ui.mici.layouts.settings.initial_cruise_speed import InitialCruiseSpeedButton

PERSONALITY_TO_INT = log.LongitudinalPersonality.schema.enumerants

# sunnydash: confidence threshold for the early distraction alert, matches the big UI stepper
EARLY_ALERT_CONFIDENCE_OPTIONS = list(range(70, 100, 5))
EARLY_ALERT_CONFIDENCE_DEFAULT = 80


class EarlyAlertConfidenceButton(BigButton):
  """Tap to cycle the early alert confidence threshold through 70-95%."""
  def __init__(self):
    super().__init__("early alert below", "",
                     description="Scene confidence below which a distraction chimes right away.\n" +
                                 "Tap to step through 70-95%.")
    self.refresh()

  def _current(self) -> int:
    value = ui_state.params.get("DriverMonitorEarlyAlertConfidence")
    return value if value in EARLY_ALERT_CONFIDENCE_OPTIONS else EARLY_ALERT_CONFIDENCE_DEFAULT

  def refresh(self):
    self.set_value(f"{self._current()}% confidence")

  def _handle_mouse_release(self, mouse_pos):
    super()._handle_mouse_release(mouse_pos)
    idx = EARLY_ALERT_CONFIDENCE_OPTIONS.index(self._current())
    new_value = EARLY_ALERT_CONFIDENCE_OPTIONS[(idx + 1) % len(EARLY_ALERT_CONFIDENCE_OPTIONS)]
    ui_state.params.put("DriverMonitorEarlyAlertConfidence", new_value)
    self.refresh()


class TogglesLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()

    self._personality_toggle = BigMultiParamToggle("following distance", "LongitudinalPersonality", ["aggressive", "standard", "relaxed"],
                                                   description="Standard is recommended.\n" +
                                                               "Aggressive follows closer, with firmer gas and braking.\n" +
                                                               "Relaxed leaves more space.\n" +
                                                               "Use the steering wheel distance button on supported cars.")
    self._initial_cruise_speed = InitialCruiseSpeedButton()
    self._initial_cruise_speed.set_visible(lambda: ui_state.CP is not None and ui_state.has_longitudinal_control and not ui_state.CP.pcmCruise)
    is_metric_toggle = BigToggle("use metric units", initial_state=ui_state.params.get_bool("IsMetric"), toggle_callback=self._on_metric_toggle)
    ldw_toggle = BigParamControl("lane departure warnings", "IsLdwEnabled",
                                 description="Warn when you drift across a detected lane line.\n" +
                                             "Only above 31 mph (50 km/h), with no turn signal.")
    always_on_dm_toggle = BigParamControl("always-on driver monitor", "AlwaysOnDM", description="Monitor the driver even when sunnypilot is not engaged.")
    early_alert_toggle = BigParamControl("early distraction alert", "DriverMonitorEarlyAlert",
                                         description="Chime as soon as you look away when the driving model's\n" +
                                                     "confidence in the scene drops below the threshold.\n" +
                                                     "Above it, chimes come at 30, 45 and 60 seconds.\n" +
                                                     "Plays even in quiet mode.")
    self._early_alert_confidence = EarlyAlertConfidenceButton()
    self._early_alert_confidence.set_visible(lambda: ui_state.params.get_bool("DriverMonitorEarlyAlert"))
    record_front = BigParamControl("record & upload cabin camera", "RecordFront",
                                   description_icon=gui_app.texture("icons_mici/settings/device/cameras.png", 64, 64),
                                   toggle_callback=restart_needed_callback, description="Upload cabin camera data to help improve driver monitoring.")
    record_mic = BigParamControl("record & upload mic audio", "RecordAudio", description_icon=gui_app.texture("icons_mici/microphone.png", 64, 64),
                                 toggle_callback=restart_needed_callback,
                                 description="Record microphone audio while driving.\n" +
                                             "Audio is included in dashcam videos in sunnylink.")
    enable_openpilot = BigParamControl("enable sunnypilot", "OpenpilotEnabledToggle", toggle_callback=restart_needed_callback,
                                       description="Enable to use sunnypilot driver assistance.\n" +
                                                   "Disable to use your car's stock driver assistance.")


    self._scroller.add_widgets([
      self._personality_toggle,
      self._initial_cruise_speed,
      is_metric_toggle,
      ldw_toggle,
      always_on_dm_toggle,
      early_alert_toggle,
      self._early_alert_confidence,
      record_front,
      record_mic,
      enable_openpilot,
    ])

    # Toggle lists
    self._refresh_toggles = (
      ("IsMetric", is_metric_toggle),
      ("IsLdwEnabled", ldw_toggle),
      ("AlwaysOnDM", always_on_dm_toggle),
      ("DriverMonitorEarlyAlert", early_alert_toggle),
      ("RecordFront", record_front),
      ("RecordAudio", record_mic),
      ("OpenpilotEnabledToggle", enable_openpilot),
    )

    enable_openpilot.set_enabled(lambda: not ui_state.engaged)
    record_front.set_enabled(False if ui_state.params.get_bool("RecordFrontLock") else (lambda: not ui_state.engaged))
    record_mic.set_enabled(lambda: not ui_state.engaged)

    if ui_state.params.get_bool("ShowDebugInfo"):
      gui_app.set_show_touches(True)
      gui_app.set_show_fps(True)

    ui_state.add_engaged_transition_callback(self._update_toggles)

  def _update_state(self):
    super()._update_state()

    if ui_state.sm.updated["selfdriveState"]:
      personality = PERSONALITY_TO_INT[ui_state.sm["selfdriveState"].personality]
      if personality != ui_state.personality and ui_state.started:
        self._personality_toggle.set_value(self._personality_toggle._options[personality])
      ui_state.personality = personality

  def show_event(self):
    super().show_event()
    self._update_toggles()

  def _on_metric_toggle(self, state: bool):
    ui_state.params.put_bool("IsMetric", state, block=True)
    self._update_toggles()

  def _update_toggles(self):
    ui_state.update_params()
    self._initial_cruise_speed.refresh()
    self._early_alert_confidence.refresh()

    if ui_state.CP is not None:
      self._personality_toggle.set_visible(ui_state.has_longitudinal_control)

    # Refresh toggles from params to mirror external changes
    for key, item in self._refresh_toggles:
      item.set_checked(ui_state.params.get_bool(key))
