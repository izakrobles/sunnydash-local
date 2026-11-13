"""sunnydash: master switch that cuts the device off from comma and sunnylink cloud services.

When PrivateDashcamBlockCloud is on:
  - the processes in CLOUD_PROCESSES never run (and running daemons are killed)
  - first-boot comma registration is skipped
  - every comma/sunnylink HTTP call made through common.api.base.BaseApi raises CloudBlockedError
  - navd won't fall back to comma's routing proxy

PrivateDashcamBlockCommaUploads is a second, comma-only layer that stays on even if the master switch is turned off
(e.g. to use sunnylink settings): it stops the comma uploader and athenad, which can send route files, camera
snapshots and live location to comma.

Both switches default to on and fail closed. Third-party downloads (GitHub updates, model and mapd downloads,
u-blox AssistNow, AGNOS images) are not affected.
"""
import requests

CLOUD_BLOCK_PARAM = "PrivateDashcamBlockCloud"
COMMA_UPLOADS_BLOCK_PARAM = "PrivateDashcamBlockCommaUploads"

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

# comma processes that can send route files, camera snapshots or location to comma
COMMA_DATA_PROCESSES = frozenset({
  "manage_athenad",
  "uploader",
})


class CloudBlockedError(requests.exceptions.ConnectionError):
  """Raised instead of making a request to comma or sunnylink while the cloud block is on."""


def _switch_on(key: str, params) -> bool:
  if params is None:
    from openpilot.common.params import Params
    params = Params()
  try:
    return bool(params.get_bool(key))
  except Exception:
    # fail closed: if we can't read the switch, assume the user wants privacy
    return True


def cloud_blocked(params=None) -> bool:
  return _switch_on(CLOUD_BLOCK_PARAM, params)


def comma_uploads_blocked(params=None) -> bool:
  return _switch_on(COMMA_UPLOADS_BLOCK_PARAM, params)
