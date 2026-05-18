#!/usr/bin/env python3
"""
Offline scenario simulator + plotter for the sunnypilot AccelController.

Runs each scenario through 4 controller settings (off / eco / normal / sport),
simulates a simplified planner + actuator + car kinematics, and produces:
  - <out>/<scenario>.png   — 4-panel time-series plot overlaying all 4 modes
  - <out>/<scenario>.gif   — short top-down animation of car-vs-lead

Usage: python tools/scripts/accel_controller_sim.py --out tools/scripts/accel_controller_sim_out
"""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path


def _install_stubs() -> None:
  pp = types.ModuleType('openpilot.common.params_pyx')

  class _StubParams:
    enabled = True
    mode = 1

    def __init__(self, *a, **k):
      pass

    def get_bool(self, k, *a, **kw):
      return _StubParams.enabled if k == 'AccelControllerEnabled' else False

    def get(self, k, *a, **kw):
      return _StubParams.mode if k == 'AccelPersonality' else None

    def put(self, k, v):
      pass

    def put_bool(self, k, v):
      pass

    def remove(self, k):
      pass

  pp.Params = _StubParams
  pp.ParamKeyFlag = type('ParamKeyFlag', (), {})
  pp.ParamKeyType = type('ParamKeyType', (), {})
  pp.UnknownKeyName = type('UnknownKeyName', (Exception,), {})
  sys.modules['openpilot.common.params_pyx'] = pp
  return _StubParams


_StubParams = _install_stubs()

import numpy as np  # noqa: E402
import matplotlib  # noqa: E402
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FuncAnimation, PillowWriter  # noqa: E402

from openpilot.sunnypilot.selfdrive.controls.lib.accel_controller.accel_controller import AccelController  # noqa: E402
from openpilot.sunnypilot.selfdrive.controls.lib.accel_controller.modes import AccelPersonality, MODE_KNOBS, IDENTITY  # noqa: E402


DT = 0.05            # 20 Hz model clock
ACTUATOR_TAU = 0.15  # s
ACCEL_MIN = -3.5
ACCEL_MAX = 1.8
STOP_DISTANCE = 6.0
T_FOLLOW_BASE = 1.45
COMFORT_BRAKE = 2.5
KP_GAP = 0.20
KV_REL = 0.80
KC_CRUISE = 0.35
JERK_BASE = 2.5

MODE_NAMES = {
  'off':    (False, AccelPersonality.NORMAL),
  'eco':    (True,  AccelPersonality.ECO),
  'normal': (True,  AccelPersonality.NORMAL),
  'sport':  (True,  AccelPersonality.SPORT),
}
MODE_COLORS = {'off': '#555555', 'eco': '#1f77b4', 'normal': '#2ca02c', 'sport': '#d62728'}


class StubLead:
  __slots__ = ('status', 'dRel', 'vLead', 'aLeadK', 'aLeadTau', 'modelProb')

  def __init__(self, status, dRel, vLead, aLeadK):
    self.status = status
    self.dRel = dRel
    self.vLead = vLead
    self.aLeadK = aLeadK
    self.aLeadTau = 1.5
    self.modelProb = 0.9


class StubLeadV3:
  __slots__ = ('a',)

  def __init__(self, a):
    self.a = list(a)


class StubModelV2:
  __slots__ = ('leadsV3',)

  def __init__(self, leads_a=None):
    self.leadsV3 = [StubLeadV3(leads_a)] if leads_a is not None else []


class StubRadar:
  __slots__ = ('leadOne', 'leadTwo')

  def __init__(self, lead):
    self.leadOne = lead
    self.leadTwo = StubLead(False, 0.0, 0.0, 0.0)


def make_sm(lead, leads_a, v_ego, a_ego):
  cs = types.SimpleNamespace(vEgo=v_ego, aEgo=a_ego)
  return {
    'carState': cs,
    'radarState': StubRadar(lead),
    'modelV2': StubModelV2(leads_a),
  }


def planner_a(v_ego, lead_status, lead_d, lead_v, v_set, knobs, prev_a):
  t_follow = T_FOLLOW_BASE + knobs.t_follow_delta
  safe_gap = STOP_DISTANCE + t_follow * v_ego + v_ego * v_ego / (2 * COMFORT_BRAKE)
  gain_scale = 1.0 / max(knobs.j_cost_mult, 0.3)
  if lead_status:
    a_lead = KP_GAP * gain_scale * (lead_d - safe_gap) + KV_REL * gain_scale * (lead_v - v_ego)
  else:
    a_lead = ACCEL_MAX
  a_cruise = KC_CRUISE * gain_scale * (v_set - v_ego)
  a_raw = min(a_lead, a_cruise)
  jerk_max = JERK_BASE / max(knobs.a_change_cost_mult, 0.3)
  da_max = jerk_max * DT
  a_clipped = float(np.clip(a_raw, prev_a - da_max, prev_a + da_max))
  return float(np.clip(a_clipped, ACCEL_MIN, ACCEL_MAX))


def run_one(scenario, mode_name, v_set=20.0, x_ego0=0.0):
  enabled, personality = MODE_NAMES[mode_name]
  _StubParams.enabled = enabled
  _StubParams.mode = personality
  ctrl = AccelController()

  ts = scenario['t']
  lead_d_arr = scenario['lead_d']
  lead_v_arr = scenario['lead_v']
  lead_a_arr = scenario['lead_a']
  lead_status_arr = scenario['lead_status']

  v_ego = scenario.get('v_ego0', 0.0)
  a_ego = 0.0
  a_actuator = 0.0
  a_planner = 0.0
  x_ego = x_ego0

  out = {k: np.zeros_like(ts) for k in ('v_ego', 'a_ego', 'a_target', 'a_planner',
                                         'gap', 'start_boost', 'early_brake', 'obstacle_boost',
                                         'x_ego', 'x_lead')}

  for i, _t in enumerate(ts):
    x_lead = x_ego + lead_d_arr[i]
    lead = StubLead(bool(lead_status_arr[i]), float(lead_d_arr[i]),
                    float(lead_v_arr[i]), float(lead_a_arr[i]))
    leads_a = [lead_a_arr[i], lead_a_arr[i], lead_a_arr[i]] if lead_status_arr[i] else None
    sm = make_sm(lead, leads_a, v_ego, a_ego)

    ctrl.update(sm, v_ego, a_ego)
    a_planner = planner_a(v_ego, bool(lead_status_arr[i]), float(lead_d_arr[i]),
                          float(lead_v_arr[i]), v_set, ctrl.knobs, a_planner)
    a_target = ctrl.modulate_a_target(a_planner)
    a_target = float(np.clip(a_target, ACCEL_MIN, ACCEL_MAX))

    alpha = 1 - np.exp(-DT / ACTUATOR_TAU)
    a_actuator += (a_target - a_actuator) * alpha
    a_ego = a_actuator
    v_ego = max(0.0, v_ego + a_actuator * DT)
    x_ego += v_ego * DT

    out['v_ego'][i] = v_ego
    out['a_ego'][i] = a_ego
    out['a_planner'][i] = a_planner
    out['a_target'][i] = a_target
    out['gap'][i] = lead_d_arr[i]
    out['start_boost'][i] = ctrl.start_boost
    out['early_brake'][i] = ctrl.early_brake_delta
    out['obstacle_boost'][i] = ctrl.obstacle_cost_boost
    out['x_ego'][i] = x_ego
    out['x_lead'][i] = x_lead

  return out


# ------------- scenarios ------------------------------------------------------

def scn_sng_launch(T=8.0):
  t = np.arange(0, T, DT)
  v = np.zeros_like(t)
  d = np.zeros_like(t)
  a = np.zeros_like(t)
  s = np.ones_like(t, dtype=bool)
  lead_x = 5.0
  for i, ti in enumerate(t):
    if ti < 1.0:
      v[i] = 0.0
      a[i] = 0.0
    elif ti < 3.5:
      a[i] = 1.4
      v[i] = min(5.0, (ti - 1.0) * 1.4)
    else:
      v[i] = 5.0
      a[i] = 0.0
    if i > 0:
      lead_x += v[i] * DT
    d[i] = lead_x
  return {'t': t, 'lead_d': d, 'lead_v': v, 'lead_a': a, 'lead_status': s, 'v_ego0': 0.0}


def scn_hard_brake(T=8.0):
  t = np.arange(0, T, DT)
  v = np.full_like(t, 25.0)
  a = np.zeros_like(t)
  s = np.ones_like(t, dtype=bool)
  for i, ti in enumerate(t):
    if 2.0 <= ti < 5.0:
      a[i] = -3.0
      v[i] = max(5.0, 25.0 - 3.0 * (ti - 2.0))
    elif ti >= 5.0:
      v[i] = 7.0
      a[i] = 0.0
  d = np.zeros_like(t)
  lead_x = 50.0
  for i, vi in enumerate(v):
    if i > 0:
      lead_x += vi * DT
    d[i] = max(5.0, lead_x - 0)
  d[0] = 50.0
  d_acc = np.cumsum(v * DT) + 50.0 - np.cumsum(np.full_like(v, 20.0 * DT))
  return {'t': t, 'lead_d': d_acc, 'lead_v': v, 'lead_a': a, 'lead_status': s, 'v_ego0': 20.0}


def scn_cutin(T=8.0):
  t = np.arange(0, T, DT)
  s = (t >= 2.0)
  v = np.where(s, 16.0, 0.0)
  a = np.where((t >= 2.0) & (t < 4.0), -1.5, 0.0)
  for i in range(len(t)):
    if 2.0 <= t[i] < 4.0:
      v[i] = max(10.0, 16.0 - 1.5 * (t[i] - 2.0))
    elif t[i] >= 4.0:
      v[i] = 13.0
      a[i] = 0.0
  d = np.full_like(t, 100.0)
  for i in range(len(t)):
    if not s[i]:
      d[i] = 100.0
    else:
      if i == 0 or not s[i - 1]:
        d[i] = 22.0
      else:
        d[i] = d[i - 1] + (v[i] - 20.0) * DT
  return {'t': t, 'lead_d': d, 'lead_v': v, 'lead_a': a, 'lead_status': s, 'v_ego0': 20.0}


def scn_traffic(T=30.0):
  t = np.arange(0, T, DT)
  cycle = 7.0
  v = 2.5 * (1 - np.cos(2 * np.pi * t / cycle))
  a = (2.5 * 2 * np.pi / cycle) * np.sin(2 * np.pi * t / cycle)
  s = np.ones_like(t, dtype=bool)
  d = np.zeros_like(t)
  lead_x = 12.0
  for i, _ti in enumerate(t):
    if i > 0:
      lead_x += v[i] * DT
    d[i] = lead_x
  return {'t': t, 'lead_d': d, 'lead_v': v, 'lead_a': a, 'lead_status': s, 'v_ego0': 0.0}


SCENARIOS = {
  'sng_launch': ('Stop-and-go launch (lead 5 m, departs after 1 s)', scn_sng_launch),
  'hard_brake': ('Highway hard brake (lead −3.0 m/s² for 3 s)', scn_hard_brake),
  'cutin':      ('Cut-in at speed (lead appears at 22 m, decelerating)', scn_cutin),
  'traffic':    ('Dense traffic creep (lead oscillates 0–5 m/s)', scn_traffic),
}


# ------------- plotting -------------------------------------------------------

def plot_scenario(name, title, scn, runs, out_dir):
  fig, axes = plt.subplots(4, 1, figsize=(11, 11), sharex=True)
  t = scn['t']

  ax = axes[0]
  ax.plot(t, scn['lead_v'], 'k--', label='lead vLead', linewidth=1)
  for mode, data in runs.items():
    ax.plot(t, data['v_ego'], color=MODE_COLORS[mode], label=f'{mode}', linewidth=1.5)
  ax.set_ylabel('velocity (m/s)')
  ax.legend(loc='upper right', ncol=5, fontsize=9)
  ax.grid(True, alpha=0.3)
  ax.set_title(title)

  ax = axes[1]
  for mode, data in runs.items():
    ax.plot(t, data['a_target'], color=MODE_COLORS[mode], label=mode, linewidth=1.5)
  ax.axhline(0, color='gray', linewidth=0.5)
  ax.set_ylabel('aTarget (m/s²)')
  ax.grid(True, alpha=0.3)

  ax = axes[2]
  for mode, data in runs.items():
    ax.plot(t, data['gap'], color=MODE_COLORS[mode], label=mode, linewidth=1.5)
  ax.axhline(STOP_DISTANCE, color='red', linestyle=':', linewidth=1, label='stop dist')
  ax.set_ylabel('gap (m)')
  ax.legend(loc='upper right', fontsize=9, ncol=5)
  ax.grid(True, alpha=0.3)

  ax = axes[3]
  for mode, data in runs.items():
    ax.plot(t, data['start_boost'], color=MODE_COLORS[mode], linestyle='-', linewidth=1.5,
            label=f'{mode} start_boost')
    ax.plot(t, -data['early_brake'], color=MODE_COLORS[mode], linestyle='--', linewidth=1.2,
            label=f'{mode} -early_brake')
  ax.axhline(0, color='gray', linewidth=0.5)
  ax.set_ylabel('bias components (m/s²)')
  ax.set_xlabel('t (s)')
  ax.legend(loc='upper right', fontsize=7, ncol=4)
  ax.grid(True, alpha=0.3)

  fig.tight_layout()
  png = out_dir / f'{name}.png'
  fig.savefig(png, dpi=110)
  plt.close(fig)
  return png


def animate_scenario(name, title, scn, runs, out_dir, fps=20):
  t = scn['t']
  n = len(t)

  fig, (ax_top, ax_a) = plt.subplots(2, 1, figsize=(11, 5),
                                     gridspec_kw={'height_ratios': [1, 1]})
  ax_top.set_xlim(-5, 80)
  ax_top.set_ylim(-1, len(runs) + 1)
  ax_top.set_yticks(range(1, len(runs) + 1))
  ax_top.set_yticklabels(list(runs.keys()))
  ax_top.set_xlabel('x (m)')
  ax_top.set_title(title)
  ax_top.grid(True, alpha=0.3)

  car_artists = {}
  lead_artists = {}
  for idx, mode in enumerate(runs.keys(), start=1):
    car, = ax_top.plot([0], [idx], 's', color=MODE_COLORS[mode], markersize=14)
    lead, = ax_top.plot([0], [idx], 'D', color='black', markersize=10, alpha=0.5)
    car_artists[mode] = car
    lead_artists[mode] = lead

  ax_a.set_xlim(t[0], t[-1])
  amin = min(r['a_target'].min() for r in runs.values()) - 0.2
  amax = max(r['a_target'].max() for r in runs.values()) + 0.2
  ax_a.set_ylim(amin, amax)
  ax_a.set_xlabel('t (s)')
  ax_a.set_ylabel('aTarget (m/s²)')
  ax_a.grid(True, alpha=0.3)
  a_lines = {}
  for mode in runs:
    line, = ax_a.plot([], [], color=MODE_COLORS[mode], label=mode, linewidth=1.6)
    a_lines[mode] = line
  ax_a.legend(loc='upper right', fontsize=9, ncol=4)

  time_text = ax_top.text(0.02, 0.95, '', transform=ax_top.transAxes, fontsize=10,
                          verticalalignment='top')

  x_ref = np.zeros(n)

  def init():
    for mode in runs:
      a_lines[mode].set_data([], [])
    time_text.set_text('')
    return list(car_artists.values()) + list(lead_artists.values()) + list(a_lines.values()) + [time_text]

  def frame(i):
    x_ref_i = runs['normal']['x_ego'][i]
    for idx, mode in enumerate(runs.keys(), start=1):
      ego_x = runs[mode]['x_ego'][i] - x_ref_i
      lead_x = runs[mode]['x_lead'][i] - x_ref_i
      car_artists[mode].set_data([ego_x], [idx])
      lead_artists[mode].set_data([lead_x], [idx])
      a_lines[mode].set_data(t[:i + 1], runs[mode]['a_target'][:i + 1])
    time_text.set_text(f't = {t[i]:5.2f} s')
    return list(car_artists.values()) + list(lead_artists.values()) + list(a_lines.values()) + [time_text]

  step = max(1, int(round(1 / (fps * DT))))
  frames = range(0, n, step)
  anim = FuncAnimation(fig, frame, frames=frames, init_func=init, blit=False)
  gif = out_dir / f'{name}.gif'
  anim.save(gif, writer=PillowWriter(fps=fps))
  plt.close(fig)
  return gif


def print_mode_summary():
  rows = []
  for name in ('eco', 'normal', 'sport'):
    pers = MODE_NAMES[name][1]
    k = MODE_KNOBS[pers]
    rows.append((name, k.t_follow_delta, k.j_cost_mult, k.a_change_cost_mult,
                 k.start_boost, k.early_brake_gain, k.early_brake_tau, k.lead_tau_scale))
  print(f"{'mode':<7} {'tF_d':>6} {'jMul':>6} {'aChg':>6} {'sBst':>6} {'ebGn':>6} {'ebTau':>6} {'lTau':>6}")
  for r in rows:
    print(f"{r[0]:<7} {r[1]:>6.2f} {r[2]:>6.2f} {r[3]:>6.2f} {r[4]:>6.2f} {r[5]:>6.2f} {r[6]:>6.2f} {r[7]:>6.2f}")


def main():
  p = argparse.ArgumentParser()
  p.add_argument('--out', type=Path, default=Path('tools/scripts/accel_controller_sim_out'))
  p.add_argument('--no-anim', action='store_true', help='skip GIFs (faster)')
  p.add_argument('--only', help='comma-separated scenario subset')
  args = p.parse_args()

  args.out.mkdir(parents=True, exist_ok=True)

  print('IDENTITY:', vars(IDENTITY))
  print_mode_summary()

  wanted = set(args.only.split(',')) if args.only else set(SCENARIOS.keys())

  for name, (title, builder) in SCENARIOS.items():
    if name not in wanted:
      continue
    print(f'\n== scenario: {name} ({title}) ==')
    scn = builder()
    runs = {mode: run_one(scn, mode) for mode in MODE_NAMES}
    png = plot_scenario(name, title, scn, runs, args.out)
    print(f'  png: {png}')
    if not args.no_anim:
      gif = animate_scenario(name, title, scn, runs, args.out)
      print(f'  gif: {gif}')

  print(f'\noutputs in: {args.out}')
  return 0


if __name__ == '__main__':
  sys.exit(main())
