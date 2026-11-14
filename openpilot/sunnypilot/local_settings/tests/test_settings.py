from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.sunnypilot.local_settings.settings import (BLOCKED_PARAMS, REASON_CAR, REASON_DEVICE_ONLY, REASON_OFFROAD,
                                                          Context, Settings, SettingError, evaluate, load_schema)

TOYOTA_LONG = {"brand": "toyota", "has_longitudinal_control": True, "device_type": "mici", "torque_allowed": True}
NO_LONG = {"brand": "honda", "has_longitudinal_control": False, "has_icbm": False, "device_type": "mici"}


class TestRules(OpenpilotTestCase):
  def ctx(self, values=None, caps=None, onroad=False, engaged=False):
    return Context(values or {}, caps or {}, onroad, engaged)

  def test_offroad_and_engaged(self):
    assert evaluate({"type": "offroad_only"}, self.ctx(onroad=False))
    assert not evaluate({"type": "offroad_only"}, self.ctx(onroad=True))
    assert evaluate({"type": "not_engaged"}, self.ctx(onroad=True, engaged=False))
    assert not evaluate({"type": "not_engaged"}, self.ctx(engaged=True))

  def test_param_rules(self):
    assert evaluate({"type": "param", "key": "A", "equals": True}, self.ctx({"A": True}))
    assert evaluate({"type": "param", "key": "A", "equals": True}, self.ctx({"A": 1}))  # int-backed toggles
    assert not evaluate({"type": "param", "key": "A", "equals": True}, self.ctx({"A": None}))
    assert evaluate({"type": "param", "key": "B", "equals": 2}, self.ctx({"B": 2}))
    assert evaluate({"type": "param_compare", "key": "B", "op": ">", "value": 0}, self.ctx({"B": 1}))
    assert not evaluate({"type": "param_compare", "key": "B", "op": ">", "value": 0}, self.ctx({"B": None}))

  def test_composites_and_unknown(self):
    cap = {"type": "capability", "field": "brand", "equals": "tesla"}
    assert evaluate({"type": "not", "condition": cap}, self.ctx(caps={"brand": "toyota"}))
    assert evaluate({"type": "any", "conditions": [cap, {"type": "offroad_only"}]}, self.ctx(caps={"brand": "toyota"}))
    assert not evaluate({"type": "all", "conditions": [cap, {"type": "offroad_only"}]}, self.ctx(caps={"brand": "toyota"}))
    assert not evaluate({"type": "something_new"}, self.ctx())  # fail closed


class TestSettings(OpenpilotTestCase):
  def setup_method(self):
    self.params = Params()
    self.settings = Settings(self.params)

  def apply(self, key, value, caps=TOYOTA_LONG, onroad=False, engaged=False):
    self.settings.apply(key, value, caps, onroad, engaged)

  def assert_refused(self, status, key, value, **kw):
    try:
      self.apply(key, value, **kw)
    except SettingError as e:
      assert e.status == status, (e.status, e.message)
      return e.message
    raise AssertionError(f"{key}={value!r} was accepted")

  def test_every_schema_key_is_a_real_param(self):
    for key in self.settings.entries:
      self.params.check_key(key)

  def test_blocked_and_device_only(self):
    for key in ("SshEnabled", "AdbEnabled", "LanguageSetting"):
      assert self.assert_refused(409, key, True) == REASON_DEVICE_ONLY
    for key in BLOCKED_PARAMS & set(self.settings.entries):
      self.assert_refused(409, key, True)

  def test_unknown_key(self):
    self.assert_refused(404, "GithubSshKeys", "ssh-rsa AAAA")
    self.assert_refused(404, "NotARealSetting", True)

  def test_toggle(self):
    self.apply("QuietMode", True)
    assert self.params.get_bool("QuietMode")
    self.apply("QuietMode", False)
    assert not self.params.get_bool("QuietMode")
    self.assert_refused(400, "QuietMode", 1)
    self.assert_refused(400, "QuietMode", "true")

  def test_int_backed_toggle(self):
    self.apply("BlinkerPauseLateralControl", True)
    assert self.params.get("BlinkerPauseLateralControl") == 1

  def test_range(self):
    self.params.put_bool("DriverMonitorEarlyAlert", True, block=True)
    self.apply("DriverMonitorEarlyAlertConfidence", 85)
    assert self.params.get("DriverMonitorEarlyAlertConfidence") == 85
    self.assert_refused(400, "DriverMonitorEarlyAlertConfidence", 83)   # off-step
    self.assert_refused(400, "DriverMonitorEarlyAlertConfidence", 100)  # above max
    self.assert_refused(400, "DriverMonitorEarlyAlertConfidence", True)
    self.assert_refused(400, "DriverMonitorEarlyAlertConfidence", float("nan"))

  def test_float_range(self):
    self.params.put_bool("ShowAdvancedControls", True, block=True)
    self.apply("CameraOffset", 0.05)
    assert abs(self.params.get("CameraOffset") - 0.05) < 1e-9

  def test_options(self):
    self.apply("LongitudinalPersonality", 2)
    assert self.params.get("LongitudinalPersonality") == 2
    self.assert_refused(400, "LongitudinalPersonality", 7)

  def test_default_option_clears_param(self):
    self.params.put_bool("EnforceTorqueControl", True, block=True)
    self.params.put("TorqueControlTune", 1.0, block=True)
    self.apply("TorqueControlTune", "")
    assert self.params.get("TorqueControlTune") is None

  def test_offroad_only_refused_while_driving(self):
    assert self.assert_refused(409, "Mads", False, onroad=True) == REASON_OFFROAD
    self.apply("Mads", False, onroad=False)
    assert not self.params.get_bool("Mads")

  def test_dependency_and_capability(self):
    self.params.put_bool("CustomAccIncrementsEnabled", False, block=True)
    self.assert_refused(409, "CustomAccShortPressIncrement", 2)
    assert self.assert_refused(409, "SmartCruiseControlVision", True, caps=NO_LONG) == REASON_CAR
    self.apply("SmartCruiseControlVision", True, caps=TOYOTA_LONG)

  def test_device_only_reason(self):
    from openpilot.sunnypilot.local_settings.settings import REASON_DEVICE
    assert self.assert_refused(409, "TrueVEgoUI", True, caps=TOYOTA_LONG) == REASON_DEVICE

  def test_vehicle_settings_only_for_own_brand(self):
    key = next(it["key"] for it in load_schema()["vehicle_settings"]["subaru"]["items"])
    assert self.assert_refused(409, key, True, caps=TOYOTA_LONG) == REASON_CAR

  def test_state_shape(self):
    state = self.settings.state(TOYOTA_LONG, onroad=True, engaged=False)
    assert set(state["values"]) == set(state["status"]) == set(self.settings.entries)
    assert state["status"]["Mads"] == {"enabled": False, "reason": REASON_OFFROAD, "options": []}
    assert state["onroad"] and not state["engaged"]

  def test_page_schema_has_only_own_brand(self):
    page = self.settings.page_schema(TOYOTA_LONG)
    assert page["brand"] == "toyota" and page["vehicle"]["items"]
    assert self.settings.page_schema({"brand": "honda"})["vehicle"] is None
