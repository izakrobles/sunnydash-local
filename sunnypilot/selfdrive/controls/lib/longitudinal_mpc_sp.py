"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np
from cereal import log
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import (
  LongitudinalMpc, MPC_SOURCES, N, T_IDXS, T_DIFFS, FCW_IDXS, COST_E_DIM,
  A_CHANGE_COST, J_EGO_COST, X_EGO_OBSTACLE_COST, X_EGO_COST, V_EGO_COST, A_EGO_COST,
  LIMIT_COST, DANGER_ZONE_COST, LEAD_DANGER_FACTOR, CRASH_DISTANCE,
  CRUISE_MIN_ACCEL, CRUISE_MAX_ACCEL, MIN_X_LEAD_FACTOR,
  get_jerk_factor, get_T_FOLLOW, get_safe_obstacle_distance, get_stopped_equivalence_factor,
)
from openpilot.selfdrive.controls.radard import _LEAD_ACCEL_TAU


class LongitudinalMpcSP(LongitudinalMpc):
  accel_ctrl = None

  def attach_accel_controller(self, accel_ctrl):
    self.accel_ctrl = accel_ctrl

  def set_weights(self, prev_accel_constraint=True, personality=log.LongitudinalPersonality.standard):
    ac = self.accel_ctrl
    on = ac is not None and ac.enabled
    jerk_factor = get_jerk_factor(personality) * (ac.knobs.j_cost_mult if on else 1.0)
    a_change_mult = ac.knobs.a_change_cost_mult if on else 1.0
    obstacle_cost = X_EGO_OBSTACLE_COST * (ac.obstacle_cost_boost if ac is not None else 1.0)
    a_change_cost = A_CHANGE_COST * a_change_mult if prev_accel_constraint else 0
    cost_weights = [obstacle_cost, X_EGO_COST, V_EGO_COST, A_EGO_COST,
                    jerk_factor * a_change_cost, jerk_factor * J_EGO_COST]
    constraint_cost_weights = [LIMIT_COST, LIMIT_COST, LIMIT_COST, DANGER_ZONE_COST]
    self.set_cost_weights(cost_weights, constraint_cost_weights)

  def process_lead(self, lead):
    ac = self.accel_ctrl
    lead_tau_scale = ac.knobs.lead_tau_scale if (ac is not None and ac.enabled) else 1.0
    v_ego = self.x0[1]
    if lead is not None and lead.status:
      x_lead = lead.dRel
      v_lead = lead.vLead
      a_lead = lead.aLeadK
      a_lead_tau = lead.aLeadTau * lead_tau_scale
    else:
      x_lead = 50.0
      v_lead = v_ego + 10.0
      a_lead = 0.0
      a_lead_tau = _LEAD_ACCEL_TAU

    min_x_lead = MIN_X_LEAD_FACTOR * (v_ego + v_lead) * (v_ego - v_lead) / (-ACCEL_MIN * 2)
    x_lead = np.clip(x_lead, min_x_lead, 1e8)
    v_lead = np.clip(v_lead, 0.0, 1e8)
    a_lead = np.clip(a_lead, -10., 5.)
    return self.extrapolate_lead(x_lead, v_lead, a_lead, a_lead_tau)

  def update(self, radarstate, v_cruise, personality=log.LongitudinalPersonality.standard):
    ac = self.accel_ctrl
    t_follow_delta = ac.knobs.t_follow_delta if (ac is not None and ac.enabled) else 0.0
    t_follow = max(0.5, get_T_FOLLOW(personality) + t_follow_delta)
    v_ego = self.x0[1]
    self.status = radarstate.leadOne.status or radarstate.leadTwo.status

    lead_xv_0 = self.process_lead(radarstate.leadOne)
    lead_xv_1 = self.process_lead(radarstate.leadTwo)

    lead_0_obstacle = lead_xv_0[:, 0] + get_stopped_equivalence_factor(lead_xv_0[:, 1])
    lead_1_obstacle = lead_xv_1[:, 0] + get_stopped_equivalence_factor(lead_xv_1[:, 1])

    v_lower = v_ego + (T_IDXS * CRUISE_MIN_ACCEL * 1.05)
    v_upper = v_ego + (T_IDXS * CRUISE_MAX_ACCEL * 1.05)
    v_cruise_clipped = np.clip(v_cruise * np.ones(len(T_IDXS)), v_lower, v_upper)
    cruise_obstacle = np.cumsum(T_DIFFS * v_cruise_clipped) + get_safe_obstacle_distance(v_cruise_clipped, t_follow)

    x_obstacles = np.column_stack([lead_0_obstacle, lead_1_obstacle, cruise_obstacle])
    self.source = MPC_SOURCES[np.argmin(x_obstacles[0])]

    self.yref[:, :] = 0.0
    for i in range(N):
      self.solver.set(i, "yref", self.yref[i])
    self.solver.set(N, "yref", self.yref[N][:COST_E_DIM])

    self.params[:, 0] = ACCEL_MIN
    self.params[:, 1] = ACCEL_MAX
    self.params[:, 2] = np.min(x_obstacles, axis=1)
    self.params[:, 3] = np.copy(self.a_prev)
    self.params[:, 4] = t_follow
    self.params[:, 5] = LEAD_DANGER_FACTOR

    self.run()
    if (np.any(lead_xv_0[FCW_IDXS, 0] - self.x_sol[FCW_IDXS, 0] < CRASH_DISTANCE) and
        radarstate.leadOne.modelProb > 0.9):
      self.crash_cnt += 1
    else:
      self.crash_cnt = 0
