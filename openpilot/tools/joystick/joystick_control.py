#!/usr/bin/env python3
import os
import re
import glob
import time
import fcntl
import struct
import argparse
import threading
import subprocess
import numpy as np
import inputs
from inputs import UnpluggedError

from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.hardware import HARDWARE
from openpilot.tools.lib.kbhit import KBHit

EXPO = 0.4
JOYSTICK_ACCEL_MAX = 4.0  # joystickd multiplies the accel axis by this before the car clips it
XBOX_DEFAULTS = {'gas_max': 1.2, 'gas_curve': 1.5, 'brake_max': 2.5, 'brake_curve': 1.5}

XPAD_NEW_ID = '/sys/bus/usb/drivers/xpad/new_id'
XINPUT_INTERFACE = ('ff', '5d', '01')    # USB class/subclass/protocol of a wired Xbox 360 pad, what 2.4G dongles present
KNOWN_XPAD_IDS = (('2dc8', '3106'),)     # 8BitDo Ultimate 2.4G dongle
PLAYSTATION_NAMES = ('sony', 'dualsense', 'dualshock', 'playstation', 'wireless controller')
ABS_X, ABS_Z, ABS_RZ = 0, 2, 5           # evdev axis codes
GAMEPAD_RESCAN_S = 1.0

_read_failed = False


def get_gamepad():
  """inputs.get_gamepad, but remembers a failed read so callers can tell "unplugged" from "an event we don't use"."""
  global _read_failed
  try:
    events = inputs.get_gamepad()
  except (OSError, UnpluggedError):
    _read_failed = True
    raise
  _read_failed = False
  return events


def _sysfs(path):
  try:
    with open(path) as f:
      return f.read().strip().lower()
  except OSError:
    return None


def bind_unknown_xbox_pads(tried=None, usb_root='/sys/bus/usb/devices', new_id_path=XPAD_NEW_ID, run=subprocess.run):
  """
  AGNOS runs a 4.9 kernel. It has the xpad driver, but xpad's ID list predates many pads (the 8BitDo 2.4G dongle,
  2dc8:3106, among them), so they sit on USB with no driver and never become an input device.
  Hand xpad the ID of every unbound XInput interface, plus the known ones. Lasts until reboot, so it's redone here.
  Returns the IDs it registered.
  """
  tried = set() if tried is None else tried
  if not os.path.exists(new_id_path):
    return []

  ids = list(KNOWN_XPAD_IDS)
  for intf in glob.glob(os.path.join(usb_root, '*:*')):
    triple = tuple(_sysfs(os.path.join(intf, f)) for f in ('bInterfaceClass', 'bInterfaceSubClass', 'bInterfaceProtocol'))
    if triple != XINPUT_INTERFACE or os.path.exists(os.path.join(intf, 'driver')):
      continue
    dev = os.path.join(os.path.dirname(intf), os.path.basename(intf).split(':')[0])
    ids.append((_sysfs(os.path.join(dev, 'idVendor')), _sysfs(os.path.join(dev, 'idProduct'))))

  added = []
  for vid, pid in ids:
    if (vid, pid) in tried or not (vid and pid and re.fullmatch(r'[0-9a-f]{4}', vid) and re.fullmatch(r'[0-9a-f]{4}', pid)):
      continue
    tried.add((vid, pid))
    run(['sudo', 'sh', '-c', f'echo {vid} {pid} > {new_id_path}'], check=False)
    added.append((vid, pid))
  return added


class _Devices(inputs.DeviceManager):
  def _find_leds(self):
    # inputs pairs each xpad LED with a gamepad and crashes when the LED exists before the pad's /dev/input symlink
    # does, i.e. right after the driver binds. We never use the LEDs.
    pass


def rescan_gamepads():
  """inputs only looks for devices once, when it's imported. Look again so a pad plugged in later is found."""
  try:
    inputs.devices = _Devices()
  except Exception as e:  # a half-enumerated device; it'll be there on the next rescan
    print(f'gamepad rescan failed, retrying: {e!r}')
    return []
  return list(inputs.devices.gamepads)


def rescan_gamepad():
  pads = rescan_gamepads()
  return pads[0] if pads else None


def use_gamepad(pad):
  """get_gamepad reads the first pad in the list, so put the one we picked there."""
  inputs.devices.gamepads.remove(pad)
  inputs.devices.gamepads.insert(0, pad)


def gamepad_event_path(pad):
  return getattr(pad, '_character_device_path', None) if pad is not None else None


def gamepad_driver(pad):
  """Kernel driver behind a pad, e.g. 'xpad'. The axis layout depends on it, not on the pad's name."""
  path = gamepad_event_path(pad)
  if not path:
    return None
  driver = os.path.realpath(f'/sys/class/input/{os.path.basename(path)}/device/device/driver')
  return os.path.basename(driver) if os.path.exists(driver) else None


def mapping_for(pad):
  """
  'xbox', 'playstation', or None for a pad we don't have a mapping for.
  Guessing is not OK here: a 2.4G dongle can briefly show up as a Switch Pro Controller while it hunts for a host
  mode, and on that layout the Xbox trigger axes are a stick.
  """
  if gamepad_driver(pad) == 'xpad':
    return 'xbox'
  if any(n in pad.name.lower() for n in PLAYSTATION_NAMES):
    return 'playstation'
  return None


def abs_info(event_path, code):
  """(current value, max) of an axis as the driver reports it, or None."""
  if not event_path:
    return None
  try:
    fd = os.open(event_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
      absinfo = fcntl.ioctl(fd, 0x80184540 + code, bytes(24))  # EVIOCGABS(code)
    finally:
      os.close(fd)
  except OSError:
    return None
  value, _minimum, maximum = struct.unpack('6i', absinfo)[:3]
  return value, maximum


def abs_max(event_path, code, default):
  """The top of an axis' range: triggers are 255 on 360-style pads but 1023 on Xbox One ones."""
  info = abs_info(event_path, code)
  return float(info[1]) if info and info[1] > 0 else default


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

  Left stick steers, right trigger is gas, left trigger is brake, RB is the
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

  def __init__(self, params=None, gas_max=None, gas_curve=None, brake_max=None, brake_curve=None, event_path=None):
    self.params = params if params is not None else Params()
    self._cli = {'gas_max': gas_max, 'gas_curve': gas_curve, 'brake_max': brake_max, 'brake_curve': brake_curve}
    self.trigger_max = abs_max(event_path, ABS_RZ, 255.)
    self.stick_max = abs_max(event_path, ABS_X, 32767.)
    self.cancel_button = 'BTN_EAST'   # B
    self.deadman_button = 'BTN_TR'    # RB
    self.deadman = False
    self.cancel = False
    self.axes_values = {'accel': 0., 'steer': 0.}
    self.axes_order = ['accel', 'steer']
    self.refresh()

    # Start from where the pad is right now, not from zero: events only arrive when something moves, so a stick or
    # brake trigger that's already off center would otherwise be invisible until it's touched.
    # Gas stays at zero until RB is seen pressed.
    for name, code in (('ABS_X', ABS_X), ('ABS_Z', ABS_Z)):
      info = abs_info(event_path, code)
      if info is not None:
        self.handle(name, info[0])

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

  @property
  def buttons(self):
    """Sent along with the axes so the phone page can show them: [RB throttle deadman, B cancel]."""
    return [self.deadman, self.cancel]

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

    return self.handle(joystick_event.code, joystick_event.state)

  def handle(self, code, state):
    if code == self.deadman_button:
      self.deadman = state == 1
    elif code == self.cancel_button:
      self.cancel = state == 1
    elif code == 'ABS_X':
      # positive xpad state is stick right, openpilot wants negative torque for a right turn
      steer = float(np.clip(-state / self.stick_max, -1., 1.))
      steer = 0. if abs(steer) < 0.03 else steer
      self.axes_values['steer'] = EXPO * steer ** 3 + (1 - EXPO) * steer
    elif code == 'ABS_RZ':
      gas = float(np.clip(state / self.trigger_max, 0., 1.)) ** self.gas_curve
      self.axes_values['accel'] = gas * self.gas_max / JOYSTICK_ACCEL_MAX
    elif code == 'ABS_Z':
      brake = float(np.clip(state / self.trigger_max, 0., 1.)) ** self.brake_curve
      self.axes_values['accel'] = -brake * self.brake_max / JOYSTICK_ACCEL_MAX
    else:
      return False

    # Only throttle is deadman gated, brakes stay live so letting go never takes away stopping power
    if not self.deadman and self.axes_values['accel'] > 0.:
      self.axes_values['accel'] = 0.

    return True


class AutoJoystick:
  """
  What the manager runs when Joystick Debug Mode is on.

  Waits for a gamepad, including one plugged in after it started, registers pads the kernel's xpad doesn't know,
  then picks the mapping: pads driven by xpad get the Xbox layout, PlayStation pads get Joystick, and anything
  else is ignored. Nothing is sent while no pad is connected, which joystickd treats as centered.
  """

  def __init__(self, params=None):
    self.params = params
    self.axes_order = ['accel', 'steer']
    self.axes_values = {'accel': 0., 'steer': 0.}
    self.connected = False
    self.name = None
    self._impl = None
    self._event_path = None
    self._tried_ids = set()
    self._ignored = set()

  @property
  def buttons(self):
    return list(getattr(self._impl, 'buttons', [])) if self.connected else []

  def refresh(self):
    refresh = getattr(self._impl, 'refresh', None)
    if self.connected and refresh is not None:
      refresh()

  def _connect(self):
    bind_unknown_xbox_pads(self._tried_ids)
    for pad in rescan_gamepads():
      mapping = mapping_for(pad)
      if mapping is not None:
        break
      if pad.name not in self._ignored:
        self._ignored.add(pad.name)
        print(f'ignoring gamepad with no known mapping: {pad.name}')
    else:
      return False

    use_gamepad(pad)
    self.name = pad.name
    self._event_path = gamepad_event_path(pad)
    self._impl = Joystick() if mapping == 'playstation' else Xbox(params=self.params, event_path=self._event_path)
    self._sync()  # the mapping may already hold the pad's current position
    self.connected = True
    print(f'gamepad connected: {self.name} ({type(self._impl).__name__} mapping)')
    return True

  def _disconnect(self):
    if self.connected:
      print(f'gamepad disconnected: {self.name}')
    self.connected = False
    self._impl = None
    self.axes_values = {'accel': 0., 'steer': 0.}

  def update(self):
    if not self.connected and not self._connect():
      time.sleep(GAMEPAD_RESCAN_S)
      return False

    ok = self._impl.update()
    if not ok and _read_failed:  # unplugged, or the dongle re-enumerated
      self._disconnect()
      return False

    self._sync()
    return ok

  def _sync(self):
    impl = self._impl
    self.axes_values = {'accel': impl.axes_values[impl.axes_order[0]], 'steer': impl.axes_values[impl.axes_order[1]]}


def send_thread(joystick):
  pm = messaging.PubMaster(['testJoystick'])

  rk = Ratekeeper(100, print_delay_threshold=None)

  while True:
    if not getattr(joystick, 'connected', True):
      rk.keep_time()
      continue

    if rk.frame % 20 == 0:
      print('\n' + ', '.join(f'{name}: {round(v, 3)}' for name, v in joystick.axes_values.items()))

    refresh = getattr(joystick, 'refresh', None)
    if refresh is not None and rk.frame % 100 == 0:
      refresh()

    joystick_msg = messaging.new_message('testJoystick')
    joystick_msg.valid = True
    joystick_msg.testJoystick.axes = [joystick.axes_values[ax] for ax in joystick.axes_order]
    buttons = getattr(joystick, 'buttons', None)
    if buttons:
      joystick_msg.testJoystick.buttons = [bool(b) for b in buttons]

    pm.send('testJoystick', joystick_msg)

    rk.keep_time()


def joystick_control_thread(joystick):
  Params().put_bool('JoystickDebugMode', True, block=True)
  threading.Thread(target=send_thread, args=(joystick,), daemon=True).start()
  while True:
    if not joystick.update() and _read_failed and getattr(joystick, 'connected', True):
      time.sleep(0.2)  # no pad to read: don't spin a core waiting for one


def main():
  joystick_control_thread(AutoJoystick())


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Publishes events from your joystick to control your car.\n' +
                                               'openpilot must be offroad before starting joystick_control. This tool supports ' +
                                               'a PlayStation 5 DualSense controller on the comma 3X.',
                                   formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument('--keyboard', action='store_true', help='Use your keyboard instead of a joystick')
  parser.add_argument('--xbox', action='store_true', help='Use an Xbox controller (xpad): left stick steers, triggers are gas/brake')
  parser.add_argument('--gas-max', type=float, default=None, help='Right trigger ceiling in m/s^2 (default: JoystickGasMax)')
  parser.add_argument('--gas-curve', type=float, default=None, help='Right trigger exponent, >1 is gentler early (default: JoystickGasCurve)')
  parser.add_argument('--brake-max', type=float, default=None, help='Left trigger ceiling in m/s^2 (default: JoystickBrakeMax)')
  parser.add_argument('--brake-curve', type=float, default=None, help='Left trigger exponent, >1 is gentler early (default: JoystickBrakeCurve)')
  parser.add_argument('--list', action='store_true', help='Just print raw gamepad events and exit, useful for finding axis names')
  args = parser.parse_args()

  if args.list:
    bind_unknown_xbox_pads()
    time.sleep(1.)
    rescan_gamepad()
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
    print('- Left stick left/right: steering')
    print('- Right trigger: gas (hold `RB` to arm throttle)')
    print('- Left trigger: brake (always live)')
    print('- `B`: Cancel cruise control')
    print(f'Tuning: {pinned}' if pinned else 'Tuning: read live from the Joystick* params (phone settings page)')
  else:
    print('Using joystick, make sure to run openpilot/cereal/messaging/bridge on your device if running over the network!')
    print('If not running on a comma device, the mapping may need to be adjusted.')

  if args.xbox:
    bind_unknown_xbox_pads()
    time.sleep(1.)  # let the kernel create the input device
    pad = rescan_gamepad()
    print(f'Gamepad: {pad.name}' if pad else 'No gamepad found yet, plug it in')
    joystick = Xbox(gas_max=args.gas_max, gas_curve=args.gas_curve, brake_max=args.brake_max, brake_curve=args.brake_curve,
                    event_path=gamepad_event_path(pad))
  else:
    joystick = Keyboard() if args.keyboard else Joystick()
  joystick_control_thread(joystick)
