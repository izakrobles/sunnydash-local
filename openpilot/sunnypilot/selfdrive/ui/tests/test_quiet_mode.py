import pytest

from openpilot.cereal import log
from openpilot.sunnypilot.selfdrive.ui import quiet_mode
from openpilot.sunnypilot.selfdrive.ui.quiet_mode import ALERTS_ALWAYS_PLAY, QuietMode

# soundd passes alerts from this enum
AudibleAlert = log.SelfdriveState.AudibleAlert


class FakeParams:
  def __init__(self, values):
    self.values = values

  def get_bool(self, key):
    return bool(self.values.get(key, False))


def make_quiet_mode(monkeypatch, quiet, early_alert):
  monkeypatch.setattr(quiet_mode, "Params", lambda: FakeParams({"QuietMode": quiet, "DriverMonitorEarlyAlert": early_alert}))
  return QuietMode()


@pytest.mark.parametrize("early_alert", [False, True])
def test_quiet_mode_off_plays_everything(monkeypatch, early_alert):
  qm = make_quiet_mode(monkeypatch, quiet=False, early_alert=early_alert)
  assert qm.should_play_sound(AudibleAlert.preAlert)
  assert qm.should_play_sound(AudibleAlert.engage)
  assert not qm.should_play_sound(AudibleAlert.none)


def test_quiet_mode_mutes_pre_alert_when_early_alert_off(monkeypatch):
  qm = make_quiet_mode(monkeypatch, quiet=True, early_alert=False)
  assert not qm.should_play_sound(AudibleAlert.preAlert)


def test_quiet_mode_plays_pre_alert_when_early_alert_on(monkeypatch):
  qm = make_quiet_mode(monkeypatch, quiet=True, early_alert=True)
  assert qm.should_play_sound(AudibleAlert.preAlert)


def test_early_alert_does_not_unmute_other_sounds(monkeypatch):
  qm = make_quiet_mode(monkeypatch, quiet=True, early_alert=True)
  assert not qm.should_play_sound(AudibleAlert.engage)
  assert not qm.should_play_sound(AudibleAlert.disengage)
  assert not qm.should_play_sound(AudibleAlert.none)
  for alert in ALERTS_ALWAYS_PLAY:
    assert qm.should_play_sound(alert)


def test_early_alert_setting_reloads(monkeypatch):
  values = {"QuietMode": True, "DriverMonitorEarlyAlert": False}
  monkeypatch.setattr(quiet_mode, "Params", lambda: FakeParams(values))
  qm = QuietMode()
  assert not qm.should_play_sound(AudibleAlert.preAlert)
  values["DriverMonitorEarlyAlert"] = True
  for _ in range(50):
    qm.load_param()
  assert qm.should_play_sound(AudibleAlert.preAlert)
