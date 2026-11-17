"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from types import SimpleNamespace

from opendbc.car.structs import car

from openpilot.sunnypilot.selfdrive.selfdrived.brake_stop_resume import ARM_TIMEOUT, BrakeStopResume

ButtonType = car.CarState.ButtonEvent.Type
GearShifter = car.CarState.GearShifter
DT = 0.01


def cs(brake=False, speed=0., gas=False, buttons=(), gear=GearShifter.drive, available=True, door=False, belt=False):
  return SimpleNamespace(brakePressed=brake, standstill=speed == 0., gasPressed=gas, gearShifter=gear,
                         cruiseState=SimpleNamespace(available=available), doorOpen=door, seatbeltUnlatched=belt,
                         buttonEvents=[SimpleNamespace(type=b) for b in buttons])


NO_LEAD = SimpleNamespace(present=False, dRel=0., vLead=0.)


def lead(d, v=0.):
  return SimpleNamespace(present=True, dRel=d, vLead=v)


class Sim:
  """Feeds BrakeStopResume like selfdrived does: the brake cancels on the step after it's pressed."""

  def __init__(self, available=True):
    self.bsr = BrakeStopResume(available, DT)
    self.enabled = True

  def step(self, state, lead_=NO_LEAD) -> bool:
    engage = self.bsr.update(state, self.enabled, lead_)
    if engage:
      self.enabled = True
    elif state.brakePressed:
      self.enabled = False
    return engage

  def brake_to_stop(self):
    assert not self.step(cs(brake=True, speed=15.))
    assert not self.step(cs(brake=True, speed=5.))
    assert not self.step(cs(brake=True, speed=0.))
    assert not self.enabled


class TestBrakeStopResume:
  def test_release_at_full_stop_engages_and_holds(self):
    sim = Sim()
    sim.brake_to_stop()
    assert sim.step(cs(brake=False, speed=0.))
    assert sim.enabled and sim.bsr.hold

  def test_release_before_stop_stays_off(self):
    # braking to coast
    sim = Sim()
    sim.step(cs(brake=True, speed=20.))
    sim.step(cs(brake=True, speed=12.))
    assert not sim.step(cs(brake=False, speed=12.))
    assert not sim.step(cs(brake=False, speed=0.))
    # a later brake press at the stop doesn't re-arm it (openpilot wasn't engaged for that press)
    assert not sim.step(cs(brake=True, speed=0.))
    assert not sim.step(cs(brake=False, speed=0.))
    assert not sim.enabled

  def test_release_while_rolling_after_stop_stays_off(self):
    sim = Sim()
    sim.brake_to_stop()
    sim.step(cs(brake=True, speed=1.))
    assert not sim.step(cs(brake=False, speed=1.))
    assert not sim.step(cs(brake=False, speed=0.))

  def test_not_engaged_never_engages(self):
    sim = Sim()
    sim.enabled = False
    sim.step(cs(brake=True, speed=10.))
    sim.step(cs(brake=True, speed=0.))
    assert not sim.step(cs(brake=False, speed=0.))

  def test_unavailable_never_engages(self):
    sim = Sim(available=False)
    sim.brake_to_stop()
    assert not sim.step(cs(brake=False, speed=0.))

  def test_disarm_conditions(self):
    blockers = {
      "gas": {"gas": True},
      "cancel": {"buttons": (ButtonType.cancel,)},
      "main off": {"available": False},
      "door": {"door": True},
      "seatbelt": {"belt": True},
      "park": {"gear": GearShifter.park},
      "neutral": {"gear": GearShifter.neutral},
    }
    for name, kw in blockers.items():
      sim = Sim()
      sim.brake_to_stop()
      sim.step(cs(brake=True, speed=0., **kw))
      assert not sim.step(cs(brake=False, speed=0.)), name

  def test_long_stop_times_out(self):
    sim = Sim()
    sim.brake_to_stop()
    for _ in range(int(ARM_TIMEOUT / DT) + 5):
      sim.step(cs(brake=True, speed=0.))
    assert not sim.step(cs(brake=False, speed=0.))

  def test_hold_released_by_gas_and_buttons(self):
    for go in ({"gas": True}, {"buttons": (ButtonType.resumeCruise,)}, {"buttons": (ButtonType.accelCruise,)}):
      sim = Sim()
      sim.brake_to_stop()
      assert sim.step(cs(speed=0.))
      sim.step(cs(speed=0.))
      assert sim.bsr.hold
      sim.step(cs(speed=0., **go))
      assert not sim.bsr.hold, go

  def test_hold_without_lead_waits_for_driver(self):
    sim = Sim()
    sim.brake_to_stop()
    assert sim.step(cs(speed=0.))
    # red light, nothing ahead; cross traffic shows up moving away and must not count
    for _ in range(500):
      sim.step(cs(speed=0.))
    sim.step(cs(speed=0.), lead(15., v=5.))
    sim.step(cs(speed=0.), lead(20., v=5.))
    assert sim.bsr.hold

  def test_hold_released_when_queued_lead_pulls_away(self):
    sim = Sim()
    sim.step(cs(brake=True, speed=10.), lead(8.))
    sim.step(cs(brake=True, speed=0.), lead(5.))
    assert sim.step(cs(speed=0.), lead(5.))
    sim.step(cs(speed=0.), lead(5.))
    sim.step(cs(speed=0.), lead(5.3, v=0.5))  # inching, not yet
    assert sim.bsr.hold
    sim.step(cs(speed=0.), lead(6.5, v=1.5))
    assert not sim.bsr.hold

  def test_lead_lost_keeps_holding(self):
    sim = Sim()
    sim.step(cs(brake=True, speed=10.), lead(8.))
    sim.step(cs(brake=True, speed=0.), lead(5.))
    assert sim.step(cs(speed=0.), lead(5.))
    for _ in range(10):
      sim.step(cs(speed=0.))
    assert sim.bsr.hold

  def test_hold_dropped_when_disengaged(self):
    sim = Sim()
    sim.brake_to_stop()
    assert sim.step(cs(speed=0.))
    sim.enabled = False  # e.g. no-entry or a panda mismatch
    sim.step(cs(speed=0.))
    sim.step(cs(speed=0.))
    assert not sim.bsr.hold

  def test_brake_again_while_holding_rearms(self):
    sim = Sim()
    sim.brake_to_stop()
    assert sim.step(cs(speed=0.))
    sim.step(cs(brake=True, speed=0.))
    assert not sim.enabled
    assert sim.step(cs(speed=0.))
