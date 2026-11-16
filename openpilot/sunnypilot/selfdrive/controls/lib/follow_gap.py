"""
Five-step following gap.

The distance button steps through five time gaps instead of openpilot's three personalities. Steps 2, 3 and 4 are
exactly the Aggressive, Standard and Relaxed gaps, step 1 is closer and step 5 is farther. The personality is derived
from the step so jerk, acceleration boost and the on-device UI keep working: 1-2 Aggressive, 3 Standard, 4-5 Relaxed.

FollowGap (1-5) is the source of truth. LongitudinalPersonality is kept in step with it, and a personality picked
elsewhere (the device's own settings) moves the gap to that personality's step.
"""
from openpilot.cereal import log

Personality = log.LongitudinalPersonality

FOLLOW_GAP_MIN = 1
FOLLOW_GAP_MAX = 5
FOLLOW_GAP_DEFAULT = 3

# seconds behind the lead car at each step
FOLLOW_GAP_T = {
  1: 1.0,
  2: 1.25,
  3: 1.45,
  4: 1.75,
  5: 2.1,
}

_PERSONALITY_STEP = {
  Personality.aggressive: 2,
  Personality.standard: 3,
  Personality.relaxed: 4,
}


def sanitize_gap(gap) -> int:
  try:
    gap = int(gap)
  except (TypeError, ValueError):
    return FOLLOW_GAP_DEFAULT
  return gap if FOLLOW_GAP_MIN <= gap <= FOLLOW_GAP_MAX else FOLLOW_GAP_DEFAULT


def gap_to_personality(gap: int) -> int:
  gap = sanitize_gap(gap)
  if gap <= 2:
    return Personality.aggressive
  if gap == 3:
    return Personality.standard
  return Personality.relaxed


def personality_to_gap(personality) -> int:
  return _PERSONALITY_STEP.get(int(personality), FOLLOW_GAP_DEFAULT)


def get_T_FOLLOW_gap(gap: int) -> float:
  return FOLLOW_GAP_T[sanitize_gap(gap)]


def follow_time_from_state(follow_gap: int) -> float | None:
  """Follow time for the planner from selfdriveStateSP.followGap, or None (use the personality) when it isn't set."""
  if FOLLOW_GAP_MIN <= follow_gap <= FOLLOW_GAP_MAX:
    return FOLLOW_GAP_T[follow_gap]
  return None


def next_gap(gap: int) -> int:
  """One button press: one step closer, wrapping from the closest step to the farthest (never closest from farthest)."""
  gap = sanitize_gap(gap)
  return gap - 1 if gap > FOLLOW_GAP_MIN else FOLLOW_GAP_MAX


def gap_label(gap: int) -> str:
  gap = sanitize_gap(gap)
  return f"{gap} of {FOLLOW_GAP_MAX} ({FOLLOW_GAP_T[gap]:.2f} s)"


class FollowGapSync:
  """Reconciles the FollowGap and LongitudinalPersonality params, whichever was changed last wins."""

  def __init__(self, gap_param, personality_param):
    personality_param = int(personality_param)
    gap = sanitize_gap(gap_param) if gap_param is not None else personality_to_gap(personality_param)
    if gap_to_personality(gap) != personality_param:
      gap = personality_to_gap(personality_param)
    self.gap = gap
    self.personality = gap_to_personality(gap)
    self._seen_gap = gap_param
    self._seen_personality = personality_param

  def set_gap(self, gap: int) -> None:
    """A change made here (the button). The caller writes .gap and .personality to the params."""
    self.gap = sanitize_gap(gap)
    self.personality = gap_to_personality(self.gap)
    self._seen_gap = self.gap
    self._seen_personality = self.personality

  def update(self, gap_param, personality_param) -> bool:
    """Feed the params as just read. Returns True when the params need writing back to match .gap/.personality."""
    personality_param = int(personality_param)
    if gap_param != self._seen_gap:
      self.gap = sanitize_gap(gap_param)
      self.personality = gap_to_personality(self.gap)
    elif personality_param != self._seen_personality and personality_param != gap_to_personality(self.gap):
      self.gap = personality_to_gap(personality_param)
      self.personality = personality_param
    self._seen_gap = gap_param
    self._seen_personality = personality_param
    return gap_param != self.gap or personality_param != self.personality
