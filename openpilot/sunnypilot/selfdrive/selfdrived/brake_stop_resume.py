"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

Resume after a braked stop.

A brake press still cancels openpilot. If that brake is held until the car is fully stopped, releasing it while still
stopped engages again, as if Resume had been pressed, and the car is then held still until one of:
  - the car that was ahead when you stopped pulls away (stop-and-go traffic)
  - you tap the gas, or press Resume / + on the wheel
Letting off before the stop (braking to coast), releasing while rolling, the gas, Cancel, cruise main off, a door,
the seatbelt, leaving Drive, or a stop longer than ARM_TIMEOUT all drop it, so the next start is manual.

The panda enforces the same release-at-a-full-stop rule (volkswagen_mqb.h), so openpilot can't engage on its own.
"""
from opendbc.car.structs import car

ButtonType = car.CarState.ButtonEvent.Type
GearShifter = car.CarState.GearShifter

ARM_TIMEOUT = 300.  # s stopped with the brake held before it gives up
LEAD_MAX_DIST = 30.  # m, a lead farther than this when you stop doesn't count as the car you're queued behind
LEAD_GO_SPEED = 1.0  # m/s, lead speed that counts as pulling away
LEAD_GO_DIST = 1.0  # m the lead must have moved away from where it was

_CANCEL = (ButtonType.cancel,)
_GO_BUTTONS = (ButtonType.resumeCruise, ButtonType.accelCruise)
_NOT_DRIVING = (GearShifter.park, GearShifter.reverse, GearShifter.neutral)


class BrakeStopResume:
  def __init__(self, available: bool, dt: float):
    self.available = available
    self.dt = dt
    self.armed = False  # the brake cancelled an engagement and is still held
    self.stopped = False  # the car came to a full stop while that brake was held
    self.stopped_time = 0.
    self.hold = False  # re-engaged here: keep the car stopped until a go condition
    self.hold_frames = 0
    self.hold_lead_d: float | None = None  # where the car ahead was when we re-engaged

  def _disarm(self) -> None:
    self.armed = False
    self.stopped = False
    self.stopped_time = 0.

  def _blocked(self, CS) -> bool:
    return (CS.gasPressed or not CS.cruiseState.available or CS.doorOpen or CS.seatbeltUnlatched or
            CS.gearShifter in _NOT_DRIVING or any(be.type in _CANCEL for be in CS.buttonEvents))

  def update(self, CS, enabled: bool, lead) -> bool:
    """Call once per selfdrived step, before the state machine. Returns True when openpilot should engage now."""
    if not self.available:
      return False

    self._update_hold(CS, enabled, lead)

    engage = False
    if enabled and CS.brakePressed:
      # engaged and braking: this press is what cancels openpilot
      self.armed = True
      self.stopped = CS.standstill
      self.stopped_time = 0.
    elif self.armed:
      if self._blocked(CS):
        self._disarm()
      elif CS.brakePressed:
        if CS.standstill:
          self.stopped = True
          self.stopped_time += self.dt
          if self.stopped_time > ARM_TIMEOUT:
            self._disarm()
      else:
        # brake released: only a release at a full stop, after a full stop, engages
        engage = self.stopped and CS.standstill
        self._disarm()

    if engage:
      self.hold = True
      self.hold_frames = 0
      self.hold_lead_d = lead.dRel if lead.present and lead.dRel < LEAD_MAX_DIST else None
    return engage

  def _update_hold(self, CS, enabled: bool, lead) -> None:
    if not self.hold:
      return
    self.hold_frames += 1

    # gave way to the driver, or openpilot didn't stay engaged (the first step is still engaging)
    go = CS.gasPressed or any(be.type in _GO_BUTTONS for be in CS.buttonEvents)
    dropped = not enabled and self.hold_frames > 1
    # the car we stopped behind is pulling away; a car that appears later (cross traffic, one turning in) doesn't count
    lead_left = (self.hold_lead_d is not None and lead.present and lead.vLead > LEAD_GO_SPEED and
                 lead.dRel > self.hold_lead_d + LEAD_GO_DIST)
    if go or dropped or lead_left:
      self.hold = False
      self.hold_lead_d = None
