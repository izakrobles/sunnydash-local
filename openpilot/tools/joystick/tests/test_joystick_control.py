import os
import tempfile
from types import SimpleNamespace
from unittest import mock

from inputs import UnpluggedError

from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.tools.joystick import joystick_control as jc


def event(code, state):
  return [SimpleNamespace(code=code, state=state)]


def usb_interface(root, name, triple, vid, pid, bound):
  intf = os.path.join(root, name)
  dev = os.path.join(root, name.split(":")[0])
  os.makedirs(intf, exist_ok=True)
  os.makedirs(dev, exist_ok=True)
  for fname, value in zip(("bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol"), triple, strict=True):
    with open(os.path.join(intf, fname), "w") as f:
      f.write(value + "\n")
  for fname, value in (("idVendor", vid), ("idProduct", pid)):
    with open(os.path.join(dev, fname), "w") as f:
      f.write(value + "\n")
  if bound:
    os.makedirs(os.path.join(intf, "driver"))


class TestBindUnknownPads(OpenpilotTestCase):
  def test_registers_unbound_xinput_pads_once(self):
    with tempfile.TemporaryDirectory() as root:
      new_id = os.path.join(root, "new_id")
      open(new_id, "w").close()
      usb_interface(root, "3-1:1.0", ("ff", "5d", "01"), "1234", "abcd", bound=False)  # unknown XInput pad, no driver
      usb_interface(root, "3-2:1.0", ("ff", "5d", "01"), "045e", "028e", bound=True)   # already has a driver
      usb_interface(root, "1-1:1.0", ("ff", "00", "00"), "2c7c", "6007", bound=False)  # the modem, not a gamepad
      usb_interface(root, "3-3:1.0", ("ff", "5d", "01"), "zz;rm", "0001", bound=False)  # never reaches the shell

      calls, tried = [], set()
      added = jc.bind_unknown_xbox_pads(tried, usb_root=root, new_id_path=new_id, run=lambda cmd, **kw: calls.append(cmd))
      assert set(added) == {("2dc8", "3106"), ("1234", "abcd")}
      assert all(c[:3] == ["sudo", "sh", "-c"] for c in calls)
      assert {c[3] for c in calls} == {f"echo 2dc8 3106 > {new_id}", f"echo 1234 abcd > {new_id}"}

      # asked again (it runs every second while no pad is connected): nothing new to register
      assert jc.bind_unknown_xbox_pads(tried, usb_root=root, new_id_path=new_id, run=lambda cmd, **kw: calls.append(cmd)) == []
      assert len(calls) == 2

  def test_no_xpad_driver(self):
    calls = []
    assert jc.bind_unknown_xbox_pads(new_id_path="/nonexistent/new_id", run=lambda cmd, **kw: calls.append(cmd)) == []
    assert calls == []


class TestXbox(OpenpilotTestCase):
  def make(self, **kw):
    return jc.Xbox(params=Params(), gas_max=2.0, gas_curve=1.0, brake_max=4.0, brake_curve=1.0, **kw)

  def feed(self, pad, code, state):
    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event(code, state)):
      return pad.update()

  def test_mapping(self):
    pad = self.make()
    assert self.feed(pad, "ABS_RZ", 255) and pad.axes_values["accel"] == 0.  # throttle needs RB held
    self.feed(pad, "BTN_TR", 1)
    self.feed(pad, "ABS_RZ", 255)
    assert abs(pad.axes_values["accel"] - 0.5) < 1e-6  # 2.0 of the 4.0 m/s^2 joystickd scale
    self.feed(pad, "ABS_Z", 255)
    assert abs(pad.axes_values["accel"] + 1.0) < 1e-6  # brake is live without RB
    self.feed(pad, "ABS_X", 32767)
    assert abs(pad.axes_values["steer"] + 1.0) < 1e-6  # stick right is negative (right turn)
    self.feed(pad, "ABS_RX", -32767)  # the right stick does nothing
    assert abs(pad.axes_values["steer"] + 1.0) < 1e-6
    self.feed(pad, "BTN_EAST", 1)
    assert pad.buttons == [True, True]

  def test_starts_from_the_pads_current_position(self):
    # a brake trigger resting off zero (or a stale value after a dropout) must show up without waiting for an event
    current = {jc.ABS_X: (0, 32767), jc.ABS_Z: (42, 255), jc.ABS_RZ: (200, 255)}
    with mock.patch.object(jc, "abs_info", side_effect=lambda path, code: current.get(code)):
      pad = self.make(event_path="/dev/input/event3")
    assert abs(pad.axes_values["accel"] + 42 / 255) < 1e-6  # brake shows; the held gas trigger does not (no RB)
    assert pad.axes_values["steer"] == 0.

  def test_trigger_range_from_device(self):
    pad = self.make()
    pad.trigger_max = 1023.  # Xbox One style pads report 0-1023, not 0-255
    self.feed(pad, "ABS_Z", 511)
    assert abs(pad.axes_values["accel"] + 0.4995) < 1e-3
    self.feed(pad, "ABS_Z", 5000)  # never past full
    assert abs(pad.axes_values["accel"] + 1.0) < 1e-6


class TestAutoJoystick(OpenpilotTestCase):
  def setup_method(self):
    self.pads = []       # what a rescan finds
    self.patches = [
      mock.patch.object(jc, "bind_unknown_xbox_pads", return_value=[]),
      mock.patch.object(jc, "rescan_gamepads", side_effect=lambda: list(self.pads)),
      mock.patch.object(jc, "use_gamepad"),
      mock.patch.object(jc, "gamepad_driver", side_effect=lambda pad: pad.driver),
      mock.patch.object(jc.time, "sleep"),
    ]
    for p in self.patches:
      p.start()

  def teardown_method(self):
    for p in self.patches:
      p.stop()

  def test_waits_then_picks_xbox_mapping_and_survives_unplug(self):
    auto = jc.AutoJoystick(params=Params())
    assert not auto.update() and not auto.connected and auto.buttons == []

    self.pads = [SimpleNamespace(name="Generic X-Box pad", driver="xpad", _character_device_path=None)]
    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event("ABS_X", -32767)):
      assert auto.update()
    assert auto.connected and isinstance(auto._impl, jc.Xbox)
    assert abs(auto.axes_values["steer"] - 1.0) < 1e-6 and auto.axes_values["accel"] == 0.
    assert auto.buttons == [False, False]

    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event("ABS_HAT0X", 1)):
      assert not auto.update()  # an event the mapping doesn't use
    assert auto.connected       # ...is not an unplug

    self.pads = []
    with mock.patch.object(jc.inputs, "get_gamepad", side_effect=UnpluggedError("gone")):
      assert not auto.update()
    assert not auto.connected and auto.axes_values == {"accel": 0., "steer": 0.}

    # plugged back in: found again on the next rescan
    self.pads = [SimpleNamespace(name="Generic X-Box pad", driver="xpad", _character_device_path=None)]
    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event("BTN_TR", 1)):
      assert auto.update()
    assert auto.connected and auto.buttons == [True, False]

  def test_playstation_pad_keeps_upstream_mapping(self):
    self.pads = [SimpleNamespace(name="Sony Interactive Entertainment DualSense Wireless Controller", driver="playstation",
                                 _character_device_path=None)]
    auto = jc.AutoJoystick(params=Params())
    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event("BTN_SOUTH", 1)):
      auto.update()
    assert auto.connected and isinstance(auto._impl, jc.Joystick)

  def test_ignores_pads_without_a_known_mapping(self):
    # the 8BitDo dongle shows up like this for a moment while it hunts for a host mode
    switch = SimpleNamespace(name="Nintendo.Co.Ltd. Pro Controller", driver="hid-generic", _character_device_path=None)
    xbox = SimpleNamespace(name="Generic X-Box pad", driver="xpad", _character_device_path=None)
    auto = jc.AutoJoystick(params=Params())

    self.pads = [switch]
    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event("ABS_RZ", 255)):
      assert not auto.update()
    assert not auto.connected

    self.pads = [switch, xbox]  # picks the one it can map, not just the first
    with mock.patch.object(jc.inputs, "get_gamepad", return_value=event("BTN_TR", 1)):
      assert auto.update()
    assert auto.connected and auto.name == "Generic X-Box pad"
    jc.use_gamepad.assert_called_with(xbox)

  def test_shows_current_position_before_any_event(self):
    self.pads = [SimpleNamespace(name="Generic X-Box pad", driver="xpad", _character_device_path="/dev/input/event3")]
    auto = jc.AutoJoystick(params=Params())
    current = {jc.ABS_X: (0, 32767), jc.ABS_Z: (255, 255)}
    with mock.patch.object(jc, "abs_info", side_effect=lambda path, code: current.get(code)):
      assert auto._connect()
    assert auto.connected and auto.axes_values["accel"] < 0  # brake already held when the pad was found
