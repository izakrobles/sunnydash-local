#!/usr/bin/env python3
import os
import argparse
import threading
import numpy as np
from inputs import UnpluggedError, get_gamepad

from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.hardware import HARDWARE
from openpilot.tools.lib.kbhit import KBHit

EXPO = 0.4
JOYSTICK_ACCEL_MAX = 4.0  # joystickd multiplies the accel axis by this before the car clips it
XBOX_DEFAULTS = {'gas_max': 1.2, 'gas_curve': 1.5, 'brake_max': 2.5, 'brake_curve': 1.5}


class Keyboard:
  def __init__(self):
    self.kb = KBHit()
    self.axis_increment = 0.05  # 5% of full actuation each key press
    self.axes_map = {'w': 'gb', 's': 'gb',
                     'a': 'steer', 'd': 'steer'}
    self.axes_values = {'gb': 0., 'steer': 0.}
    self.axes_order = ['gb', 'steer']
    self.cancel = False

  def update(self):
    key = self.kb.getch().lower()
    self.cancel = False
    if key == 'r':
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
    elif key == 'c':
      self.cancel = True
    elif key in self.axes_map:
      axis = self.axes_map[key]
      incr = self.axis_increment if key in ['w', 'a'] else -self.axis_increment
      self.axes_values[axis] = float(np.clip(self.axes_values[axis] + incr, -1, 1))
    else:
      return False
    return True


class Joystick:
  def __init__(self):
    # This class supports a PlayStation 5 DualSense controller on the comma 3X
    # TODO: find a way to get this from API or detect gamepad/PC, perhaps "inputs" doesn't support it
    self.cancel_button = 'BTN_NORTH'  # BTN_NORTH=X/triangle
    if HARDWARE.get_device_type() == 'pc':
      accel_axis = 'ABS_Z'
      steer_axis = 'ABS_RX'
      # TODO: once the longcontrol API is finalized, we can replace this with outputting gas/brake and steering
      self.flip_map = {'ABS_RZ': accel_axis}
    else:
      accel_axis = 'ABS_RX'
      steer_axis = 'ABS_Z'
      self.flip_map = {'ABS_RY': accel_axis}

    self.min_axis_value = {accel_axis: 0., steer_axis: 0.}
    self.max_axis_value = {accel_axis: 255., steer_axis: 255.}
    self.axes_values = {accel_axis: 0., steer_axis: 0.}
    self.axes_order = [accel_axis, steer_axis]
    self.cancel = False

  def update(self):
    try:
      joystick_event = get_gamepad()[0]
    except (OSError, UnpluggedError):
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
      return False

    event = (joystick_event.code, joystick_event.state)

    # flip left trigger to negative accel
    if event[0] in self.flip_map:
      event = (self.flip_map[event[0]], -event[1])

    if event[0] == self.cancel_button:
      if event[1] == 1:
        self.cancel = True
      elif event[1] == 0:   # state 0 is falling edge
        self.cancel = False
    elif event[0] in self.axes_values:
      self.max_axis_value[event[0]] = max(event[1], self.max_axis_value[event[0]])
      self.min_axis_value[event[0]] = min(event[1], self.min_axis_value[event[0]])

      norm = -float(np.interp(event[1], [self.min_axis_value[event[0]], self.max_axis_value[event[0]]], [-1., 1.]))
      norm = norm if abs(norm) > 0.03 else 0.  # center can be noisy, deadzone of 3%
      self.axes_values[event[0]] = EXPO * norm ** 3 + (1 - EXPO) * norm  # less action near center for fine control
    else:
      return False
    return True


class Xbox:
  """
  Xbox controller over the xpad driver.

  Right stick steers, right trigger is gas, left trigger is brake, RB is the
  throttle deadman (hold it to accelerate) and B cancels cruise.

  Uses fixed axis ranges instead of the adaptive min/max in Joystick: triggers
  only travel one direction, so a learned range would turn a light tap into
  full travel.

  Analog triggers map linearly onto the accel request, so a light press is light
  braking. gas_max/brake_max are in m/s^2 and are what a fully pressed trigger
  asks for; joystickd multiplies the axis by JOYSTICK_ACCEL_MAX and the car
  clips to its own limits, so asking for more than the car accepts just burns
  trigger travel.

  Values come from the JoystickGasMax/JoystickGasCurve/JoystickBrakeMax/
  JoystickBrakeCurve params so they can be changed from the phone settings page
  while this is running. A CLI flag pins that one value for the session.
  """

  def __init__(self, params=None, gas_max=None, gas_curve=None, brake_max=None, brake_curve=None):
    self.params = params if params is not None else Params()
    self._cli = {'gas_max': gas_max, 'gas_curve': gas_curve, 'brake_max': brake_max, 'brake_curve': brake_curve}
    self.cancel_button = 'BTN_EAST'   # B
    self.deadman_button = 'BTN_TR'    # RB
    self.deadman = False
    self.cancel = False
    self.axes_values = {'accel': 0., 'steer': 0.}
    self.axes_order = ['accel', 'steer']
    self.refresh()

  def _read(self, name, key, lo, hi):
    value = self._cli[name]
    if value is None:
      try:
        value = self.params.get(key, return_default=True)
      except (KeyError, TypeError, ValueError):
        value = None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      value = XBOX_DEFAULTS[name]
    return float(np.clip(value, lo, hi))

  def refresh(self):
    """Re-read the tuning, called once a second so the phone page can change it mid-session."""
    self.gas_max = self._read('gas_max', 'JoystickGasMax', 0., JOYSTICK_ACCEL_MAX)
    self.gas_curve = self._read('gas_curve', 'JoystickGasCurve', 0.2, 4.)
    self.brake_max = self._read('brake_max', 'JoystickBrakeMax', 0., JOYSTICK_ACCEL_MAX)
    self.brake_curve = self._read('brake_curve', 'JoystickBrakeCurve', 0.2, 4.)

  def update(self):
    try:
      joystick_event = get_gamepad()[0]
    except (OSError, UnpluggedError):
      self.axes_values = dict.fromkeys(self.axes_values, 0.)
      self.deadman = False
      return False

    code, state = joystick_event.code, joystick_event.state

    if code == self.deadman_button:
      self.deadman = state == 1
    elif code == self.cancel_button:
      self.cancel = state == 1
    elif code == 'ABS_RX':
      # positive xpad state is stick right, openpilot wants negative torque for a right turn
      steer = -state / 32767.
      steer = 0. if abs(steer) < 0.03 else steer
      self.axes_values['steer'] = EXPO * steer ** 3 + (1 - EXPO) * steer
    elif code == 'ABS_RZ':
      gas = (state / 255.) ** self.gas_curve
      self.axes_values['accel'] = gas * self.gas_max / JOYSTICK_ACCEL_MAX
    elif code == 'ABS_Z':
      brake = (state / 255.) ** self.brake_curve
      self.axes_values['accel'] = -brake * self.brake_max / JOYSTICK_ACCEL_MAX
    else:
      return False

    # Only throttle is deadman gated, brakes stay live so letting go never takes away stopping power
    if not self.deadman and self.axes_values['accel'] > 0.:
      self.axes_values['accel'] = 0.

    return True


def send_thread(joystick):
  pm = messaging.PubMaster(['testJoystick'])

  rk = Ratekeeper(100, print_delay_threshold=None)

  while True:
    if rk.frame % 20 == 0:
      print('\n' + ', '.join(f'{name}: {round(v, 3)}' for name, v in joystick.axes_values.items()))

    refresh = getattr(joystick, 'refresh', None)
    if refresh is not None and rk.frame % 100 == 0:
      refresh()

    joystick_msg = messaging.new_message('testJoystick')
    joystick_msg.valid = True
    joystick_msg.testJoystick.axes = [joystick.axes_values[ax] for ax in joystick.axes_order]

    pm.send('testJoystick', joystick_msg)

    rk.keep_time()


def joystick_control_thread(joystick):
  Params().put_bool('JoystickDebugMode', True, block=True)
  threading.Thread(target=send_thread, args=(joystick,), daemon=True).start()
  while True:
    joystick.update()


def main():
  joystick_control_thread(Joystick())


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Publishes events from your joystick to control your car.\n' +
                                               'openpilot must be offroad before starting joystick_control. This tool supports ' +
                                               'a PlayStation 5 DualSense controller on the comma 3X.',
                                   formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--keyboard', action='store_true', help='Use your keyboard instead of a joystick')
  parser.add_argument('--xbox', action='store_true', help='Use an Xbox controller (xpad): right stick steers, triggers are gas/brake')
  parser.add_argument('--gas-max', type=float, default=None, help='Right trigger ceiling in m/s^2 (default: JoystickGasMax)')
  parser.add_argument('--gas-curve', type=float, default=None, help='Right trigger exponent, >1 is gentler early (default: JoystickGasCurve)')
  parser.add_argument('--brake-max', type=float, default=None, help='Left trigger ceiling in m/s^2 (default: JoystickBrakeMax)')
  parser.add_argument('--brake-curve', type=float, default=None, help='Left trigger exponent, >1 is gentler early (default: JoystickBrakeCurve)')
  parser.add_argument('--list', action='store_true', help='Just print raw gamepad events and exit, useful for finding axis names')
  args = parser.parse_args()

  if args.list:
    print('Printing raw events, ctrl-c to stop. Press a stick/trigger/button to see its code.')
    while True:
      try:
        joystick_event = get_gamepad()[0]
      except (OSError, UnpluggedError):
        print('No gamepad found, plug it in and restart')
        break
      print(f'{joystick_event.code}: {joystick_event.state}')
    exit()

  if not Params().get_bool("IsOffroad") and "ZMQ" not in os.environ:
    print("The car must be off before running joystick_control.")
    exit()

  print()
  if args.keyboard:
    print('Gas/brake control: `W` and `S` keys')
    print('Steering control: `A` and `D` keys')
    print('Buttons')
    print('- `R`: Resets axes')
    print('- `C`: Cancel cruise control')
  elif args.xbox:
    pinned = ', '.join(f'{k}={v:g}' for k, v in (('gas max', args.gas_max), ('gas curve', args.gas_curve),
                                                  ('brake max', args.brake_max), ('brake curve', args.brake_curve))
                       if v is not None)
    print('Xbox mapping')
    print('- Right stick left/right: steering')
    print('- Right trigger: gas (hold `RB` to arm throttle)')
    print('- Left trigger: brake (always live)')
    print('- `B`: Cancel cruise control')
    print(f'Tuning: {pinned}' if pinned else 'Tuning: read live from the Joystick* params (phone settings page)')
  else:
    print('Using joystick, make sure to run openpilot/cereal/messaging/bridge on your device if running over the network!')
    print('If not running on a comma device, the mapping may need to be adjusted.')

  joystick = Keyboard() if args.keyboard else Xbox(gas_max=args.gas_max, gas_curve=args.gas_curve,
                                                   brake_max=args.brake_max, brake_curve=args.brake_curve) if args.xbox else Joystick()
  joystick_control_thread(joystick)
