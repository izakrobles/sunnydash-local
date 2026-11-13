"""sunnydash: master switch that cuts the device off from comma and sunnylink cloud services.

When PrivateDashcamBlockCloud is on:
  - the processes in CLOUD_PROCESSES never run (and running daemons are killed)
  - first-boot comma registration is skipped
  - every comma/sunnylink HTTP call made through common.api.base.BaseApi raises CloudBlockedError
  - navd won't fall back to comma's routing proxy

Third-party traffic (GitHub updates, model and mapd downloads, u-blox AssistNow, AGNOS images) is not affected.
"""
import requests

CLOUD_BLOCK_PARAM = "PrivateDashcamBlockCloud"

# processes whose only job is talking to comma or sunnylink servers
CLOUD_PROCESSES = frozenset({
  # comma
  "manage_athenad",
  "uploader",
  # sunnylink
  "manage_sunnylinkd",
  "sunnylink_registration_manager",
  "statsd_sp",
  "backup_manager",
  "sunnylink_uploader",
})


class CloudBlockedError(requests.exceptions.ConnectionError):
  """Raised instead of making a request to comma or sunnylink while the cloud block is on."""


def cloud_blocked(params=None) -> bool:
  if params is None:
    from openpilot.common.params import Params
    params = Params()
  try:
    return bool(params.get_bool(CLOUD_BLOCK_PARAM))
  except Exception:
    # fail closed: if we can't read the switch, assume the user wants privacy
    return True
