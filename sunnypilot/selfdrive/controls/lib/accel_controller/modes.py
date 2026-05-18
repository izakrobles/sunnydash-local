"""
Copyright (c) 2021-, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from dataclasses import dataclass


class AccelPersonality:
  ECO = 0
  NORMAL = 1
  SPORT = 2


@dataclass
class ModeKnobs:
  t_follow_delta: float = 0.0
  j_cost_mult: float = 1.0
  a_change_cost_mult: float = 1.0
  obstacle_cost_boost: float = 1.0
  lead_tau_scale: float = 1.0
  start_boost: float = 0.0
  early_brake_gain: float = 0.0
  early_brake_tau: float = 0.15


IDENTITY = ModeKnobs()

MODE_KNOBS = {
  AccelPersonality.ECO: ModeKnobs(
    t_follow_delta=0.25,
    j_cost_mult=1.60,
    a_change_cost_mult=1.40,
    obstacle_cost_boost=1.30,
    lead_tau_scale=0.85,
    start_boost=0.10,
    early_brake_gain=0.60,
    early_brake_tau=0.20,
  ),
  AccelPersonality.NORMAL: ModeKnobs(
    t_follow_delta=0.0,
    j_cost_mult=1.0,
    a_change_cost_mult=1.0,
    obstacle_cost_boost=1.50,
    lead_tau_scale=0.75,
    start_boost=0.20,
    early_brake_gain=0.80,
    early_brake_tau=0.15,
  ),
  AccelPersonality.SPORT: ModeKnobs(
    t_follow_delta=-0.15,
    j_cost_mult=0.60,
    a_change_cost_mult=0.70,
    obstacle_cost_boost=1.70,
    lead_tau_scale=0.65,
    start_boost=0.35,
    early_brake_gain=1.00,
    early_brake_tau=0.08,
  ),
}
