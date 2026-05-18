"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from dataclasses import replace
import numpy as np

from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.sunnypilot.selfdrive.controls.lib.accel_controller.modes import (
  AccelPersonality, ModeKnobs, MODE_KNOBS, IDENTITY,
)

STOP_DISTANCE = 6.0
BLEND_TAU = 0.8
NEW_LEAD_BOOST_WINDOW = 0.3
EARLY_BRAKE_HOLD = 0.2
EARLY_BRAKE_THRESHOLD = -0.5
EARLY_BRAKE_MAX = 0.8
START_BOOST_FADE_V = 2.0
PARAM_READ_FRAMES = 50


class AccelController:
  def __init__(self, CP=None, CP_SP=None, params=None):
    self.CP = CP
    self.CP_SP = CP_SP
    self.params = params or Params()
    self.frame = 0

    self.enabled = self.params.get_bool("AccelControllerEnabled")
    self.personality = self._read_personality()

    self.knobs = ModeKnobs()
    self.target_knobs = ModeKnobs()

    self.lead_seen_t = 0.0
    self.last_lead_status = False
    self.early_brake_hold = 0.0

    self.early_brake_raw = 0.0
    self.early_brake_delta = 0.0
    self.start_boost = 0.0
    self.obstacle_cost_boost = 1.0

  def _read_personality(self):
    val = self.params.get("AccelPersonality", return_default=True)
    if val is None or val not in MODE_KNOBS:
      return AccelPersonality.NORMAL
    return val

  def _read_params(self):
    if self.frame % PARAM_READ_FRAMES == 0:
      self.enabled = self.params.get_bool("AccelControllerEnabled")
      self.personality = self._read_personality()

  def _refresh_target(self):
    self.target_knobs = replace(IDENTITY if not self.enabled else MODE_KNOBS[self.personality])

  def _blend(self, dt):
    a = 1 - np.exp(-dt / BLEND_TAU) if BLEND_TAU > 0 else 1.0
    cur, tgt = vars(self.knobs), vars(self.target_knobs)
    for name in cur:
      cur[name] += (tgt[name] - cur[name]) * a

  def _update_lead_seen(self, lead_status):
    if lead_status and not self.last_lead_status:
      self.lead_seen_t = 0.0
    elif lead_status:
      self.lead_seen_t += DT_MDL
    else:
      self.lead_seen_t = 0.0
    self.last_lead_status = lead_status

  def _compute_early_brake(self, lead, modelV2):
    if not self.enabled or lead is None or not lead.status:
      self.early_brake_hold = 0.0
      return 0.0

    a_radar = float(lead.aLeadK)
    a_model = a_radar
    leads_v3 = modelV2.leadsV3 if modelV2 is not None else []
    if len(leads_v3) > 0:
      a_arr = list(leads_v3[0].a)[:3]
      if a_arr:
        a_model = float(np.mean(a_arr))

    if a_radar < EARLY_BRAKE_THRESHOLD and a_model < EARLY_BRAKE_THRESHOLD:
      self.early_brake_hold = EARLY_BRAKE_HOLD
    else:
      self.early_brake_hold = max(0.0, self.early_brake_hold - DT_MDL)

    if self.early_brake_hold <= 0:
      return 0.0

    fused = max(a_radar, a_model)
    raw = float(np.clip(-fused + EARLY_BRAKE_THRESHOLD, 0.0, EARLY_BRAKE_MAX))
    return raw * self.knobs.early_brake_gain

  def _compute_start_boost(self, v_ego, lead):
    if not self.enabled or v_ego >= START_BOOST_FADE_V:
      return 0.0
    if lead is None or not lead.status:
      return 0.0
    if lead.vLead <= 1.0 or lead.dRel <= STOP_DISTANCE + 1.0:
      return 0.0
    fade = max(0.0, 1.0 - v_ego / START_BOOST_FADE_V)
    return self.knobs.start_boost * fade

  def update(self, sm, v_ego, a_ego):
    self._read_params()
    self._refresh_target()
    self._blend(DT_MDL)

    lead = sm['radarState'].leadOne
    md = sm['modelV2']

    self._update_lead_seen(bool(lead.status))
    self.early_brake_raw = self._compute_early_brake(lead, md)

    tau = max(self.knobs.early_brake_tau, DT_MDL)
    a = 1 - np.exp(-DT_MDL / tau)
    self.early_brake_delta += (self.early_brake_raw - self.early_brake_delta) * a

    self.start_boost = self._compute_start_boost(v_ego, lead)
    new_lead = self.last_lead_status and self.lead_seen_t < NEW_LEAD_BOOST_WINDOW
    self.obstacle_cost_boost = self.knobs.obstacle_cost_boost if (self.enabled and new_lead) else 1.0
    self.frame += 1

  def modulate_a_target(self, a_target):
    if not self.enabled:
      return a_target
    return a_target + self.start_boost - self.early_brake_delta

  def modulate_should_stop(self, should_stop, lead):
    if not self.enabled or not should_stop or lead is None or not lead.status:
      return should_stop
    if lead.vLead > 1.0 and lead.dRel > STOP_DISTANCE + 1.0:
      return False
    return should_stop
