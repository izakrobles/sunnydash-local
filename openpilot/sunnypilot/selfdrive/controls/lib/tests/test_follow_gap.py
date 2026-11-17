"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.cereal import log

from openpilot.sunnypilot.selfdrive.controls.lib.follow_gap import (
  FOLLOW_GAP_T, FollowGapSync, follow_time_from_state, gap_label, gap_to_personality, next_gap, personality_to_gap,
  sanitize_gap, step_gap,
)

P = log.LongitudinalPersonality

# get_T_FOLLOW in long_mpc.py (not importable without the compiled MPC solver)
STOCK_T_FOLLOW = {P.aggressive: 1.25, P.standard: 1.45, P.relaxed: 1.75}


class TestFollowGap:
  def test_middle_steps_match_personalities(self):
    # steps 2-4 must be exactly the stock personality gaps so nothing changes for people who never leave them
    for p in (P.aggressive, P.standard, P.relaxed):
      assert FOLLOW_GAP_T[personality_to_gap(p)] == STOCK_T_FOLLOW[p]
      assert gap_to_personality(personality_to_gap(p)) == p

  def test_gaps_increase_and_stay_safe(self):
    times = [FOLLOW_GAP_T[g] for g in range(1, 6)]
    assert times == sorted(times)
    assert min(times) >= 1.0

  def test_personality_buckets(self):
    assert [gap_to_personality(g) for g in range(1, 6)] == [P.aggressive, P.aggressive, P.standard, P.relaxed, P.relaxed]

  def test_button_steps_closer_and_wraps_to_farthest(self):
    assert [next_gap(g) for g in range(1, 6)] == [5, 1, 2, 3, 4]

  def test_sanitize(self):
    assert sanitize_gap(None) == 3
    assert sanitize_gap(0) == 3
    assert sanitize_gap(9) == 3
    assert sanitize_gap("4") == 4

  def test_planner_falls_back_without_a_gap(self):
    assert follow_time_from_state(0) is None
    assert follow_time_from_state(1) == 1.0
    assert follow_time_from_state(5) == 2.1

  def test_step_gap(self):
    assert [step_gap(g, 0) for g in range(1, 6)] == [5, 1, 2, 3, 4]  # toggle cycles
    assert [step_gap(g, -1) for g in range(1, 6)] == [1, 1, 2, 3, 4]  # closer stops at 1
    assert [step_gap(g, 1) for g in range(1, 6)] == [2, 3, 4, 5, 5]  # farther stops at 5

  def test_label(self):
    assert gap_label(2) == "2 of 5 (1.25 s)"


class TestFollowGapSync:
  def test_first_run_takes_gap_from_personality(self):
    # existing installs have no FollowGap yet (default 3) but may have picked Relaxed
    sync = FollowGapSync(3, P.relaxed)
    assert (sync.gap, sync.personality) == (4, P.relaxed)
    assert sync.update(3, P.relaxed)  # params need writing back to 4

  def test_matching_params_kept(self):
    sync = FollowGapSync(1, P.aggressive)
    assert (sync.gap, sync.personality) == (1, P.aggressive)
    assert not sync.update(1, P.aggressive)

  def test_gap_change_from_settings_wins(self):
    sync = FollowGapSync(3, P.standard)
    assert sync.update(5, P.standard)  # personality must be rewritten
    assert (sync.gap, sync.personality) == (5, P.relaxed)
    assert not sync.update(5, P.relaxed)

  def test_personality_change_from_device_moves_gap(self):
    sync = FollowGapSync(3, P.standard)
    assert sync.update(3, P.aggressive)
    assert (sync.gap, sync.personality) == (2, P.aggressive)
    assert not sync.update(2, P.aggressive)

  def test_personality_matching_current_bucket_keeps_gap(self):
    sync = FollowGapSync(5, P.relaxed)
    # picking Relaxed while already on step 5 shouldn't snap back to 4
    assert not sync.update(5, P.relaxed)
    assert sync.gap == 5

  def test_button_press_then_param_readback(self):
    sync = FollowGapSync(2, P.aggressive)
    sync.set_gap(next_gap(sync.gap))
    assert (sync.gap, sync.personality) == (1, P.aggressive)
    assert not sync.update(1, P.aggressive)
    sync.set_gap(next_gap(sync.gap))
    assert (sync.gap, sync.personality) == (5, P.relaxed)
    assert not sync.update(5, P.relaxed)

  def test_invalid_gap_param_repaired(self):
    sync = FollowGapSync(3, P.standard)
    assert sync.update(9, P.standard)
    assert sync.gap == 3


class TestGapPageSetSpeed:
  """While the distance page is open, card hands + / - presses to the gap and the set speed must not move."""

  def _helper(self):
    from types import SimpleNamespace
    from opendbc.car import structs
    from openpilot.selfdrive.car.cruise import VCruiseHelper
    h = VCruiseHelper(SimpleNamespace(pcmCruise=False), structs.CarParamsSP())
    h.v_cruise_kph = 72.
    return h

  def _cs(self, events):
    from types import SimpleNamespace
    return SimpleNamespace(buttonEvents=events, cruiseState=SimpleNamespace(available=True, standstill=False, speed=0.,
                           speedCluster=0.), vEgo=20., gasPressed=False)

  def _tap(self, h, fgb, t):
    from opendbc.car import structs
    for pressed in (True, False):
      events = [structs.CarState.ButtonEvent(type=t, pressed=pressed)]
      fgb.update(events)
      h.update_v_cruise(self._cs(events), True, True, frozenset(fgb.page_presses))

  def test_plus_changes_speed_only_with_page_closed(self):
    from opendbc.car import structs
    from opendbc.sunnypilot.car.follow_gap_buttons import FollowGapButtons
    ButtonType = structs.CarState.ButtonEvent.Type
    h, fgb = self._helper(), FollowGapButtons()
    for _ in range(3):  # settle the just-engaged state
      h.update_v_cruise(self._cs([]), True, True)
    self._tap(h, fgb, ButtonType.accelCruise)
    speed = h.v_cruise_kph
    assert speed > 72.
    self._tap(h, fgb, ButtonType.gapAdjustCruise)
    self._tap(h, fgb, ButtonType.accelCruise)
    self._tap(h, fgb, ButtonType.decelCruise)
    assert h.v_cruise_kph == speed
