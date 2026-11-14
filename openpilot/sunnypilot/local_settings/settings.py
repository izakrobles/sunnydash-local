"""sunnydash: settings logic for the on-device phone settings page.

Reads sunnylink's compiled settings_ui.json (the same file the sunnylink website uses) and enforces its rules on the
device: a change is only written if the item exists, isn't blocked, its enablement/visibility rules pass right now
(offroad-only, depends-on-another-setting, car capability, ...) and the value fits the widget and the param's type.
"""
import json
import math
import os
from dataclasses import dataclass, field

from openpilot.common.params import ParamKeyType

SETTINGS_UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sunnylink", "settings_ui.json")

# Never writable from the phone page. Mirrors sunnylinkd.BLOCKED_PARAMS, plus the page's own switch and PIN.
BLOCKED_PARAMS = frozenset({
  "AdbEnabled",
  "CompletedSunnylinkConsentVersion",
  "CompletedTrainingVersion",
  "GithubUsername",
  "GithubSshKeys",
  "HasAcceptedTerms",
  "HasAcceptedTermsSP",
  "OnroadCycleRequested",
  "ParamsVersion",
  "LocalSettingsEnabled",
  "LocalSettingsPin",
})

REASON_OFFROAD = "Park the car to change this"
REASON_ENGAGED = "Disengage to change this"
REASON_CAR = "Not available on this car"
REASON_DEVICE = "Not available on this device"
REASON_DEVICE_ONLY = "Can only be changed on the device"
REASON_UNAVAILABLE = "Unavailable"


class SettingError(Exception):
  def __init__(self, status: int, message: str):
    super().__init__(message)
    self.status = status
    self.message = message


@dataclass
class Context:
  values: dict
  caps: dict
  onroad: bool
  engaged: bool


@dataclass
class Entry:
  item: dict
  gates: list = field(default_factory=list)  # rule lists inherited from section / sub panel / brand
  read_only_reason: str | None = None


def load_schema(path: str = SETTINGS_UI_PATH) -> dict:
  with open(path) as f:
    return json.load(f)


def _walk_items(items):
  for item in items or []:
    yield item
    yield from _walk_items(item.get("sub_items"))


def index_schema(schema: dict) -> tuple[dict[str, Entry], dict[str, str]]:
  """Map every param key to its item plus the rules inherited from where it sits. Also returns key -> title."""
  entries: dict[str, Entry] = {}
  titles: dict[str, str] = {}

  def add(items, gates, read_only_reason=None):
    for item in _walk_items(items):
      titles.setdefault(item["key"], item.get("title", item["key"]))
      reason = read_only_reason
      if item.get("blocked") or item["key"] in BLOCKED_PARAMS:
        reason = REASON_DEVICE_ONLY
      elif item["widget"] in ("info", "button"):
        reason = REASON_DEVICE_ONLY
      entries[item["key"]] = Entry(item, list(gates), reason)

  for panel in schema.get("panels", []):
    for section in panel.get("sections", []):
      gates = [r for r in (section.get("enablement"), section.get("visibility")) if r]
      read_only = REASON_DEVICE_ONLY if section.get("attestation_required") else None
      add(section.get("items"), gates, read_only)
      for sub in section.get("sub_panels", []):
        sub_gates = gates + ([[sub["trigger_condition"]]] if sub.get("trigger_condition") else [])
        add(sub.get("items"), sub_gates, read_only)

  for brand, vehicle in schema.get("vehicle_settings", {}).items():
    add(vehicle.get("items"), [[{"type": "capability", "field": "brand", "equals": brand}]])

  return entries, titles


def read_value(params, key: str, item: dict):
  """Current value as the page should show it. Unset params with a "" option (meaning default) read as ""."""
  if any(o.get("value") == "" for o in item.get("options", [])) and params.get(key) is None:
    return ""
  value = params.get(key, return_default=True)
  if isinstance(value, bytes):
    return value.decode(errors="replace")
  return value


def _equals(actual, expected) -> bool:
  if isinstance(expected, bool):
    return bool(actual) == expected
  if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
    return math.isclose(float(actual), float(expected), abs_tol=1e-6)
  return actual == expected


def evaluate(rule: dict, ctx: Context) -> bool:
  t = rule.get("type")
  if t == "all":
    return all(evaluate(r, ctx) for r in rule.get("conditions", []))
  if t == "any":
    return any(evaluate(r, ctx) for r in rule.get("conditions", []))
  if t == "not":
    return not evaluate(rule["condition"], ctx)
  if t == "offroad_only":
    return not ctx.onroad
  if t == "not_engaged":
    return not ctx.engaged
  if t == "capability":
    return _equals(ctx.caps.get(rule["field"]), rule.get("equals"))
  if t == "param":
    return _equals(ctx.values.get(rule["key"]), rule.get("equals"))
  if t == "param_compare":
    actual = ctx.values.get(rule["key"])
    if not isinstance(actual, (int, float)):
      return False
    value, op = rule["value"], rule["op"]
    return {">": actual > value, "<": actual < value, ">=": actual >= value, "<=": actual <= value}.get(op, False)
  return False  # unknown rule types fail closed


def _rule_types(rule: dict) -> set[str]:
  types = {rule.get("type")}
  for r in rule.get("conditions", []):
    types |= _rule_types(r)
  if rule.get("condition"):
    types |= _rule_types(rule["condition"])
  return types


def _capability_fields(rule: dict) -> set[str]:
  fields = {rule["field"]} if rule.get("type") == "capability" else set()
  for r in rule.get("conditions", []) + ([rule["condition"]] if rule.get("condition") else []):
    fields |= _capability_fields(r)
  return fields


def _first_param_key(rule: dict) -> str | None:
  if rule.get("type") in ("param", "param_compare"):
    return rule["key"]
  for r in rule.get("conditions", []) + ([rule["condition"]] if rule.get("condition") else []):
    if key := _first_param_key(r):
      return key
  return None


def reason_for(rule: dict, titles: dict[str, str]) -> str:
  types = _rule_types(rule)
  if rule.get("type") == "offroad_only" or types == {"offroad_only"}:
    return REASON_OFFROAD
  if rule.get("type") == "not_engaged" or types == {"not_engaged"}:
    return REASON_ENGAGED
  if "capability" in types:
    return REASON_DEVICE if _capability_fields(rule) == {"device_type"} else REASON_CAR
  if key := _first_param_key(rule):
    return f"Depends on “{titles.get(key, key)}”"
  if "offroad_only" in types:
    return REASON_OFFROAD
  return REASON_UNAVAILABLE


def first_failure(rule_lists, ctx: Context, titles: dict[str, str]) -> str | None:
  for rules in rule_lists:
    for rule in rules or []:
      if not evaluate(rule, ctx):
        return reason_for(rule, titles)
  return None


class Settings:
  def __init__(self, params, schema: dict | None = None):
    self.params = params
    self.schema = schema if schema is not None else load_schema()
    self.entries, self.titles = index_schema(self.schema)

  def page_schema(self, caps: dict) -> dict:
    """The schema the page renders: all panels, plus only this car's brand settings."""
    brand = caps.get("brand")
    vehicle = self.schema.get("vehicle_settings", {}).get(brand) if brand else None
    return {"panels": self.schema.get("panels", []), "vehicle": vehicle, "brand": brand}

  def context(self, caps: dict, onroad: bool, engaged: bool) -> Context:
    values = {key: read_value(self.params, key, e.item) for key, e in self.entries.items()}
    return Context(values, caps, onroad, engaged)

  def status(self, entry: Entry, ctx: Context) -> tuple[bool, str | None]:
    if entry.read_only_reason:
      return False, entry.read_only_reason
    reason = first_failure(entry.gates + [entry.item.get("visibility"), entry.item.get("enablement")], ctx, self.titles)
    return reason is None, reason

  def state(self, caps: dict, onroad: bool, engaged: bool) -> dict:
    ctx = self.context(caps, onroad, engaged)
    status = {}
    for key, entry in self.entries.items():
      enabled, reason = self.status(entry, ctx)
      options = [first_failure([o.get("enablement")], ctx, self.titles) is None for o in entry.item.get("options", [])]
      status[key] = {"enabled": enabled, "reason": reason, "options": options}
    return {
      "values": ctx.values,
      "status": status,
      "onroad": onroad,
      "engaged": engaged,
      "is_metric": bool(ctx.values.get("IsMetric", self.params.get_bool("IsMetric"))),
    }

  def apply(self, key: str, value, caps: dict, onroad: bool, engaged: bool):
    """Validate and write one setting. Raises SettingError on anything that isn't allowed right now."""
    entry = self.entries.get(key)
    if entry is None:
      raise SettingError(404, "Unknown setting")

    ctx = self.context(caps, onroad, engaged)
    enabled, reason = self.status(entry, ctx)
    if not enabled:
      raise SettingError(409, reason or REASON_UNAVAILABLE)

    item = entry.item
    param_type = self.params.get_type(key)

    if item["widget"] == "toggle":
      if not isinstance(value, bool):
        raise SettingError(400, "Expected on or off")
      if param_type == ParamKeyType.BOOL:
        self.params.put_bool(key, value, block=True)
      elif param_type == ParamKeyType.INT:
        self.params.put(key, int(value), block=True)
      else:
        raise SettingError(400, "Unsupported setting type")
      return

    if item.get("options"):
      for option in item["options"]:
        if _option_matches(option["value"], value):
          if first_failure([option.get("enablement")], ctx, self.titles) is not None:
            raise SettingError(409, "That choice isn't available right now")
          if option["value"] == "":
            self.params.remove(key)
          else:
            self.params.put(key, _coerce(option["value"], param_type), block=True)
          return
      raise SettingError(400, "Not one of the allowed choices")

    if "min" in item and "max" in item:
      if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SettingError(400, "Expected a number")
      lo, hi, step = item["min"], item["max"], item.get("step") or 1
      if value < lo - 1e-9 or value > hi + 1e-9:
        raise SettingError(400, f"Must be between {lo} and {hi}")
      steps = round((value - lo) / step)
      snapped = lo + steps * step
      if not math.isclose(snapped, value, abs_tol=step * 1e-3):
        raise SettingError(400, f"Must be in steps of {step}")
      decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
      self.params.put(key, _coerce(round(snapped, decimals + 2), param_type), block=True)
      return

    raise SettingError(403, REASON_DEVICE_ONLY)


def _option_matches(option_value, value) -> bool:
  if isinstance(option_value, str) or isinstance(value, str):
    return option_value == value
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return False
  return math.isclose(float(option_value), float(value), abs_tol=1e-6)


def _coerce(value, param_type):
  if param_type == ParamKeyType.INT:
    if not math.isclose(float(value), round(float(value)), abs_tol=1e-9):
      raise SettingError(400, "Expected a whole number")
    return int(round(float(value)))
  if param_type == ParamKeyType.FLOAT:
    return float(value)
  if param_type == ParamKeyType.BOOL:
    return bool(value)
  if param_type == ParamKeyType.STRING:
    return str(value)
  raise SettingError(400, "Unsupported setting type")
