"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.accel_controller.accel_controller import (
  AccelController, BLEND_TAU, START_BOOST_FADE_V, STOP_DISTANCE,
)
from openpilot.sunnypilot.selfdrive.controls.lib.accel_controller.modes import (
  AccelPersonality, ModeKnobs, MODE_KNOBS, IDENTITY,
)


class StubParams:
  def __init__(self, values=None):
    self.values = values or {}

  def get_bool(self, key, *_a, **_kw):
    return bool(self.values.get(key, False))

  def get(self, key, *_a, **_kw):
    return self.values.get(key, None)


class StubLead:
  def __init__(self, status=False, dRel=0.0, vLead=0.0, aLeadK=0.0, aLeadTau=1.5, modelProb=0.9):
    self.status = status
    self.dRel = dRel
    self.vLead = vLead
    self.aLeadK = aLeadK
    self.aLeadTau = aLeadTau
    self.modelProb = modelProb


class StubRadarState:
  def __init__(self, leadOne=None, leadTwo=None):
    self.leadOne = leadOne or StubLead()
    self.leadTwo = leadTwo or StubLead()


class StubLeadV3:
  def __init__(self, a=(0.0, 0.0, 0.0)):
    self.a = list(a)


class StubModelV2:
  def __init__(self, leadsV3=()):
    self.leadsV3 = list(leadsV3)


class StubCarState:
  def __init__(self, vEgo=0.0, aEgo=0.0):
    self.vEgo = vEgo
    self.aEgo = aEgo


def make_sm(lead=None, leadsV3=(), v_ego=0.0, a_ego=0.0):
  return {
    'carState': StubCarState(vEgo=v_ego, aEgo=a_ego),
    'radarState': StubRadarState(leadOne=lead),
    'modelV2': StubModelV2(leadsV3=leadsV3),
  }


def make_ctrl(enabled=True, personality=AccelPersonality.NORMAL):
  values = {
    'AccelControllerEnabled': enabled,
    'AccelPersonality': personality,
  }
  return AccelController(params=StubParams(values))


def step(ctrl, sm, v_ego, n=1):
  for _ in range(n):
    ctrl.update(sm, v_ego, sm['carState'].aEgo)


class TestModeTables:
  def test_all_modes_are_mode_knobs(self):
    for mode_knobs in MODE_KNOBS.values():
      assert isinstance(mode_knobs, ModeKnobs)

  def test_field_set_matches_identity(self):
    expected = set(vars(IDENTITY))
    for mode_knobs in MODE_KNOBS.values():
      assert set(vars(mode_knobs)) == expected

  def test_t_follow_eco_loosest_sport_tightest(self):
    assert MODE_KNOBS[AccelPersonality.ECO].t_follow_delta > \
           MODE_KNOBS[AccelPersonality.NORMAL].t_follow_delta > \
           MODE_KNOBS[AccelPersonality.SPORT].t_follow_delta

  def test_jerk_cost_eco_highest_sport_lowest(self):
    assert MODE_KNOBS[AccelPersonality.ECO].j_cost_mult > \
           MODE_KNOBS[AccelPersonality.NORMAL].j_cost_mult > \
           MODE_KNOBS[AccelPersonality.SPORT].j_cost_mult

  def test_start_boost_eco_lowest_sport_highest(self):
    assert MODE_KNOBS[AccelPersonality.ECO].start_boost < \
           MODE_KNOBS[AccelPersonality.NORMAL].start_boost < \
           MODE_KNOBS[AccelPersonality.SPORT].start_boost

  def test_lead_tau_scale_sport_fastest(self):
    assert MODE_KNOBS[AccelPersonality.ECO].lead_tau_scale > \
           MODE_KNOBS[AccelPersonality.NORMAL].lead_tau_scale > \
           MODE_KNOBS[AccelPersonality.SPORT].lead_tau_scale

  def test_early_brake_tau_sport_smallest(self):
    assert MODE_KNOBS[AccelPersonality.SPORT].early_brake_tau < \
           MODE_KNOBS[AccelPersonality.NORMAL].early_brake_tau < \
           MODE_KNOBS[AccelPersonality.ECO].early_brake_tau


class TestEnableGate:
  def test_disabled_keeps_identity_after_settle(self):
    ctrl = make_ctrl(enabled=False, personality=AccelPersonality.SPORT)
    sm = make_sm(v_ego=10.0)
    step(ctrl, sm, v_ego=10.0, n=int(8.0 / DT_MDL))
    have, want = vars(ctrl.knobs), vars(IDENTITY)
    for name, v in want.items():
      assert abs(have[name] - v) < 1e-3

  def test_enabled_settles_to_mode(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    sm = make_sm(v_ego=10.0)
    step(ctrl, sm, v_ego=10.0, n=int(8.0 / DT_MDL))
    have, want = vars(ctrl.knobs), vars(MODE_KNOBS[AccelPersonality.SPORT])
    for name, v in want.items():
      assert abs(have[name] - v) < 1e-3

  def test_disabled_modulate_a_target_passthrough(self):
    ctrl = make_ctrl(enabled=False)
    ctrl.start_boost = 0.5
    ctrl.early_brake_delta = 0.3
    assert ctrl.modulate_a_target(1.0) == 1.0

  def test_disabled_modulate_should_stop_passthrough(self):
    ctrl = make_ctrl(enabled=False)
    lead = StubLead(status=True, vLead=5.0, dRel=50.0)
    assert ctrl.modulate_should_stop(True, lead) is True

  def test_disabled_personality_param_invalid_falls_back_normal(self):
    values = {'AccelControllerEnabled': True, 'AccelPersonality': 99}
    ctrl = AccelController(params=StubParams(values))
    assert ctrl.personality == AccelPersonality.NORMAL


class TestBlend:
  def test_blend_ramps_within_few_taus(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    sm = make_sm(v_ego=10.0)
    step(ctrl, sm, v_ego=10.0, n=int(BLEND_TAU * 4 / DT_MDL))
    have, want = vars(ctrl.knobs), vars(MODE_KNOBS[AccelPersonality.SPORT])
    for name, v in want.items():
      assert abs(have[name] - v) < 0.02


class TestStartBoost:
  def test_boost_active_at_standstill_with_moving_lead(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.NORMAL)
    lead = StubLead(status=True, vLead=2.0, dRel=STOP_DISTANCE + 2.0)
    sm = make_sm(lead=lead, v_ego=0.0)
    step(ctrl, sm, v_ego=0.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.start_boost > 0.0

  def test_boost_zero_when_lead_stationary(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.NORMAL)
    lead = StubLead(status=True, vLead=0.0, dRel=STOP_DISTANCE + 2.0)
    sm = make_sm(lead=lead, v_ego=0.0)
    step(ctrl, sm, v_ego=0.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.start_boost == 0.0

  def test_boost_zero_when_no_lead(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.NORMAL)
    sm = make_sm(v_ego=0.0)
    step(ctrl, sm, v_ego=0.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.start_boost == 0.0

  def test_boost_fades_above_threshold_speed(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    lead = StubLead(status=True, vLead=5.0, dRel=STOP_DISTANCE + 5.0)
    sm = make_sm(lead=lead, v_ego=START_BOOST_FADE_V + 1.0)
    step(ctrl, sm, v_ego=START_BOOST_FADE_V + 1.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.start_boost == 0.0


class TestEarlyBrake:
  def test_requires_both_signals_below_threshold(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    lead = StubLead(status=True, vLead=10.0, dRel=20.0, aLeadK=-2.0)
    leads_v3 = (StubLeadV3(a=(-2.0, -2.0, -2.0)),)
    sm = make_sm(lead=lead, leadsV3=leads_v3, v_ego=15.0)
    step(ctrl, sm, v_ego=15.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.early_brake_delta > 0.0

  def test_zero_when_only_radar_decel(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    lead = StubLead(status=True, vLead=10.0, dRel=20.0, aLeadK=-2.0)
    leads_v3 = (StubLeadV3(a=(0.0, 0.0, 0.0)),)
    sm = make_sm(lead=lead, leadsV3=leads_v3, v_ego=15.0)
    step(ctrl, sm, v_ego=15.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.early_brake_delta == 0.0

  def test_zero_when_no_lead(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    sm = make_sm(v_ego=15.0)
    step(ctrl, sm, v_ego=15.0, n=int(BLEND_TAU * 4 / DT_MDL))
    assert ctrl.early_brake_delta == 0.0


class TestEarlyBrakeFilter:
  def _settled_brake_sm(self):
    lead = StubLead(status=True, vLead=10.0, dRel=20.0, aLeadK=-2.0)
    leads_v3 = (StubLeadV3(a=(-2.0, -2.0, -2.0)),)
    return make_sm(lead=lead, leadsV3=leads_v3, v_ego=15.0)

  def _settle_blend(self, ctrl, sm):
    step(ctrl, sm, v_ego=15.0, n=int(BLEND_TAU * 4 / DT_MDL))

  def test_filter_no_first_frame_step(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    self._settle_blend(ctrl, make_sm(v_ego=15.0))
    sm = self._settled_brake_sm()
    step(ctrl, sm, v_ego=15.0, n=1)
    assert ctrl.early_brake_delta < ctrl.early_brake_raw
    assert ctrl.early_brake_delta > 0.0

  def test_filter_ramps_in_monotonic(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.NORMAL)
    self._settle_blend(ctrl, make_sm(v_ego=15.0))
    sm = self._settled_brake_sm()
    samples = []
    for _ in range(int(0.5 / DT_MDL)):
      step(ctrl, sm, v_ego=15.0, n=1)
      samples.append(ctrl.early_brake_delta)
    for prev, cur in zip(samples, samples[1:], strict=False):
      assert cur + 1e-9 >= prev

  def test_filter_settles_close_to_raw(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    self._settle_blend(ctrl, make_sm(v_ego=15.0))
    sm = self._settled_brake_sm()
    step(ctrl, sm, v_ego=15.0, n=int(1.0 / DT_MDL))
    assert abs(ctrl.early_brake_delta - ctrl.early_brake_raw) < 0.05

  def test_filter_ramps_out_after_signals_drop(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    self._settle_blend(ctrl, make_sm(v_ego=15.0))
    sm = self._settled_brake_sm()
    step(ctrl, sm, v_ego=15.0, n=int(1.0 / DT_MDL))
    peak = ctrl.early_brake_delta
    assert peak > 0.0

    benign = make_sm(lead=StubLead(status=True, vLead=10.0, dRel=20.0, aLeadK=0.0), v_ego=15.0)
    step(ctrl, benign, v_ego=15.0, n=int(1.0 / DT_MDL))
    assert ctrl.early_brake_delta < peak * 0.1

  def test_sport_tau_faster_than_eco(self):
    sport = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    eco = make_ctrl(enabled=True, personality=AccelPersonality.ECO)
    self._settle_blend(sport, make_sm(v_ego=15.0))
    self._settle_blend(eco, make_sm(v_ego=15.0))
    sm = self._settled_brake_sm()
    n = int(0.15 / DT_MDL)
    step(sport, sm, v_ego=15.0, n=n)
    step(eco, sm, v_ego=15.0, n=n)
    sport_frac = sport.early_brake_delta / max(sport.early_brake_raw, 1e-9)
    eco_frac = eco.early_brake_delta / max(eco.early_brake_raw, 1e-9)
    assert sport_frac > eco_frac


class TestShouldStopOverride:
  def test_release_for_moving_lead_at_safe_distance(self):
    ctrl = make_ctrl(enabled=True)
    lead = StubLead(status=True, vLead=2.0, dRel=STOP_DISTANCE + 2.0)
    assert ctrl.modulate_should_stop(True, lead) is False

  def test_keep_stop_when_lead_stationary(self):
    ctrl = make_ctrl(enabled=True)
    lead = StubLead(status=True, vLead=0.0, dRel=STOP_DISTANCE + 2.0)
    assert ctrl.modulate_should_stop(True, lead) is True

  def test_keep_stop_when_lead_close(self):
    ctrl = make_ctrl(enabled=True)
    lead = StubLead(status=True, vLead=2.0, dRel=2.0)
    assert ctrl.modulate_should_stop(True, lead) is True

  def test_passthrough_when_no_lead(self):
    ctrl = make_ctrl(enabled=True)
    assert ctrl.modulate_should_stop(True, None) is True
    assert ctrl.modulate_should_stop(False, None) is False


class TestObstacleCostBoost:
  def test_boost_active_first_window(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    lead = StubLead(status=True, vLead=10.0, dRel=30.0)
    sm = make_sm(lead=lead, v_ego=15.0)
    step(ctrl, sm, v_ego=15.0, n=1)
    assert ctrl.obstacle_cost_boost > 1.0

  def test_boost_decays_after_window(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    lead = StubLead(status=True, vLead=10.0, dRel=30.0)
    sm = make_sm(lead=lead, v_ego=15.0)
    step(ctrl, sm, v_ego=15.0, n=int(2.0 / DT_MDL))
    assert ctrl.obstacle_cost_boost == 1.0

  def test_boost_unity_when_disabled(self):
    ctrl = make_ctrl(enabled=False, personality=AccelPersonality.SPORT)
    lead = StubLead(status=True, vLead=10.0, dRel=30.0)
    sm = make_sm(lead=lead, v_ego=15.0)
    step(ctrl, sm, v_ego=15.0, n=1)
    assert ctrl.obstacle_cost_boost == 1.0


class TestModulateATarget:
  def test_bias_applies_start_boost(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    ctrl.start_boost = 0.4
    ctrl.early_brake_delta = 0.0
    assert abs(ctrl.modulate_a_target(1.0) - 1.4) < 1e-6

  def test_bias_applies_early_brake(self):
    ctrl = make_ctrl(enabled=True, personality=AccelPersonality.SPORT)
    ctrl.start_boost = 0.0
    ctrl.early_brake_delta = 0.3
    assert abs(ctrl.modulate_a_target(0.0) + 0.3) < 1e-6
