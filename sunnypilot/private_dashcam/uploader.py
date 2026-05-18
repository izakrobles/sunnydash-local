#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
  from openpilot.common.params import Params

ALLOWED_MEDIA_FILENAMES = frozenset({
  "rlog.zst",
  "qlog.zst",
  "fcamera.hevc",
  "ecamera.hevc",
  "dcamera.hevc",
  "qcamera.ts",
})

PRIVATE_UPLOAD_ATTR_NAME = "user.private_upload"
PRIVATE_SEGMENT_UPLOAD_ATTR_NAME = "user.private_segment_upload"
PRESERVE_ATTR_NAME = "user.preserve"
UPLOAD_ATTR_VALUE = b"1"

DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_TIMEOUT = 30.0
DEFAULT_SLEEP_WHEN_IDLE = 60.0
MAX_SEGMENTS_PER_RUN_PARAM = "PrivateDashcamMaxSegmentsPerRun"
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.|:-]{0,127}$")

allow_sleep = bool(int(os.getenv("UPLOADER_SLEEP", "1")))
force_wifi = os.getenv("FORCEWIFI") is not None


def get_cloudlog():
  from openpilot.common.swaglog import cloudlog
  return cloudlog


@dataclass(frozen=True)
class UploaderConfig:
  endpoint: str
  token: str
  device_id: str
  log_root: Path
  timeout: float = DEFAULT_TIMEOUT


@dataclass
class SegmentFile:
  filename: str
  path: Path
  size_bytes: int
  sha256: str | None = None
  already_private_uploaded: bool = False


@dataclass
class DeviceSegment:
  name: str
  route_id: str
  segment: int
  path: Path
  locked: bool
  bookmarked: bool
  private_segment_uploaded: bool
  mtime_ns: int
  files: list[SegmentFile] = field(default_factory=list)


@dataclass
class FileUploadResult:
  filename: str
  status: str
  status_code: int | None = None
  size_bytes: int = 0
  sha256: str | None = None
  detail: str | None = None


@dataclass
class SegmentUploadResult:
  name: str
  route_id: str
  segment: int
  bookmarked: bool
  locked: bool
  status: str
  files: list[FileUploadResult] = field(default_factory=list)
  manifest: FileUploadResult | None = None
  detail: str | None = None


class BytesReader:
  def __init__(self, body: bytes):
    self.body = body
    self.offset = 0

  def read(self, size: int = -1) -> bytes:
    if size is None or size < 0:
      size = len(self.body) - self.offset
    chunk = self.body[self.offset:self.offset + size]
    self.offset += len(chunk)
    return chunk


class HTTPUploader:
  def __init__(self, config: UploaderConfig, chunk_size: int = DEFAULT_CHUNK_SIZE):
    self.endpoint = config.endpoint.rstrip("/")
    self.token = config.token
    self.device_id = validate_safe_id(config.device_id, "device_id")
    self.timeout = config.timeout
    self.chunk_size = chunk_size

  def upload_file(self, *, segment: DeviceSegment, file: SegmentFile) -> tuple[int, str]:
    if file.sha256 is None:
      file.sha256 = sha256_file(file.path)

    with file.path.open("rb") as body:
      return self._upload_body(
        route_id=segment.route_id,
        segment=segment.segment,
        filename=file.filename,
        sha256=file.sha256,
        size_bytes=file.size_bytes,
        body=body,
      )

  def upload_bytes(self, *, segment: DeviceSegment, filename: str, body: bytes) -> tuple[int, str]:
    return self._upload_body(
      route_id=segment.route_id,
      segment=segment.segment,
      filename=filename,
      sha256=hashlib.sha256(body).hexdigest(),
      size_bytes=len(body),
      body=BytesReader(body),
    )

  def _upload_body(
    self,
    *,
    route_id: str,
    segment: int,
    filename: str,
    sha256: str,
    size_bytes: int,
    body: BinaryIO,
  ) -> tuple[int, str]:
    parsed = urlsplit(self._file_url(route_id=route_id, segment=segment, filename=filename))
    if parsed.scheme == "http":
      conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=self.timeout)
    elif parsed.scheme == "https":
      conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=self.timeout)
    else:
      raise ValueError(f"unsupported endpoint scheme: {parsed.scheme}")

    path = parsed.path
    if parsed.query:
      path = f"{path}?{parsed.query}"

    try:
      conn.putrequest("POST", path)
      conn.putheader("Authorization", f"Bearer {self.token}")
      conn.putheader("X-Content-Sha256", sha256)
      conn.putheader("Content-Type", "application/octet-stream")
      conn.putheader("Content-Length", str(size_bytes))
      conn.endheaders()

      for chunk in iter(lambda: body.read(self.chunk_size), b""):
        conn.send(chunk)

      response = conn.getresponse()
      response_body = response.read(4096).decode("utf-8", errors="replace")
      return response.status, response_body
    finally:
      conn.close()

  def _file_url(self, *, route_id: str, segment: int, filename: str) -> str:
    return (
      f"{self.endpoint}/api/devices/{quote(self.device_id, safe='')}"
      f"/routes/{quote(route_id, safe='')}"
      f"/segments/{segment}"
      f"/files/{quote(filename, safe='')}"
    )


class PrivateDashcamUploader:
  def __init__(self, config: UploaderConfig):
    self.config = config
    self.uploader = HTTPUploader(config)
    self.last_segment = ""

  def step(self) -> SegmentUploadResult | None:
    segments = filter_segments(
      discover_segments(self.config.log_root),
      include_locked=False,
      force=False,
      newest_first=False,
      limit_segments=1,
    )
    if not segments:
      return None

    result = upload_segment(
      uploader=self.uploader,
      segment=segments[0],
      device_id=self.config.device_id,
      mark_uploaded=True,
      force=False,
    )
    self.last_segment = result.name

    get_cloudlog().event(
      "private_dashcam_upload_result",
      result=json.dumps(asdict(result), sort_keys=True),
    )
    return result


def discover_segments(log_root: Path) -> list[DeviceSegment]:
  if not log_root.is_dir():
    return []

  segments: list[DeviceSegment] = []
  for path in sorted(log_root.iterdir(), key=lambda item: item.name):
    if not path.is_dir():
      continue

    parsed = parse_segment_dir_name(path.name)
    if parsed is None:
      continue
    route_id, segment_number = parsed

    try:
      children = sorted(path.iterdir(), key=lambda item: item.name)
      stat = path.stat()
      files = [
        SegmentFile(
          filename=child.name,
          path=child,
          size_bytes=child.stat().st_size,
          already_private_uploaded=xattr_is_true(child, PRIVATE_UPLOAD_ATTR_NAME),
        )
        for child in children
        if child.is_file() and child.name in ALLOWED_MEDIA_FILENAMES
      ]
    except OSError:
      continue

    segments.append(
      DeviceSegment(
        name=path.name,
        route_id=route_id,
        segment=segment_number,
        path=path,
        locked=any(child.name.endswith(".lock") for child in children),
        bookmarked=xattr_is_true(path, PRESERVE_ATTR_NAME),
        private_segment_uploaded=xattr_is_true(path, PRIVATE_SEGMENT_UPLOAD_ATTR_NAME),
        mtime_ns=stat.st_mtime_ns,
        files=files,
      )
    )

  return segments


def upload_segment(
  *,
  uploader,
  segment: DeviceSegment,
  device_id: str,
  mark_uploaded: bool = True,
  force: bool = False,
) -> SegmentUploadResult:
  if segment.locked:
    return SegmentUploadResult(
      name=segment.name,
      route_id=segment.route_id,
      segment=segment.segment,
      bookmarked=segment.bookmarked,
      locked=True,
      status="skipped_locked",
    )

  if not segment.files:
    return SegmentUploadResult(
      name=segment.name,
      route_id=segment.route_id,
      segment=segment.segment,
      bookmarked=segment.bookmarked,
      locked=False,
      status="skipped_empty",
    )

  result = SegmentUploadResult(
    name=segment.name,
    route_id=segment.route_id,
    segment=segment.segment,
    bookmarked=segment.bookmarked,
    locked=False,
    status="uploaded",
  )

  all_files_ok = True
  for file in segment.files:
    if file.already_private_uploaded and not force:
      if file.sha256 is None:
        file.sha256 = sha256_file(file.path)
      result.files.append(
        FileUploadResult(
          filename=file.filename,
          status="already_private_uploaded",
          size_bytes=file.size_bytes,
          sha256=file.sha256,
        )
      )
      continue

    if file.sha256 is None:
      file.sha256 = sha256_file(file.path)

    status_code, body = uploader.upload_file(segment=segment, file=file)
    file_result = FileUploadResult(
      filename=file.filename,
      status="uploaded" if status_code in (200, 201) else "failed",
      status_code=status_code,
      size_bytes=file.size_bytes,
      sha256=file.sha256,
      detail=body[:300] if status_code not in (200, 201) else None,
    )
    result.files.append(file_result)

    if status_code in (200, 201):
      if mark_uploaded:
        set_xattr_true(file.path, PRIVATE_UPLOAD_ATTR_NAME)
    else:
      all_files_ok = False

  manifest_body = build_manifest(segment, device_id=device_id)
  if segment.private_segment_uploaded and not force:
    result.manifest = FileUploadResult(filename="manifest.json", status="already_private_uploaded", size_bytes=len(manifest_body))
  else:
    status_code, body = uploader.upload_bytes(segment=segment, filename="manifest.json", body=manifest_body)
    result.manifest = FileUploadResult(
      filename="manifest.json",
      status="uploaded" if status_code in (200, 201) else "failed",
      status_code=status_code,
      size_bytes=len(manifest_body),
      sha256=hashlib.sha256(manifest_body).hexdigest(),
      detail=body[:300] if status_code not in (200, 201) else None,
    )
    if status_code not in (200, 201):
      all_files_ok = False

  if all_files_ok and mark_uploaded:
    set_xattr_true(segment.path, PRIVATE_SEGMENT_UPLOAD_ATTR_NAME)

  if not all_files_ok:
    result.status = "failed"
  elif all(file.status == "already_private_uploaded" for file in result.files) and result.manifest and result.manifest.status == "already_private_uploaded":
    result.status = "already_private_uploaded"

  return result


def build_manifest(segment: DeviceSegment, *, device_id: str) -> bytes:
  files = []
  for file in segment.files:
    if file.sha256 is None:
      file.sha256 = sha256_file(file.path)
    files.append({
      "filename": file.filename,
      "size_bytes": file.size_bytes,
      "sha256": file.sha256,
    })

  payload = {
    "device_id": device_id,
    "route_id": segment.route_id,
    "segment": segment.segment,
    "bookmarked": segment.bookmarked,
    "preserve_context_before": 2,
    "preserve_context_after": 1,
    "files": files,
  }
  return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def filter_segments(
  segments: list[DeviceSegment],
  *,
  include_locked: bool,
  force: bool,
  newest_first: bool,
  limit_segments: int | None,
) -> list[DeviceSegment]:
  filtered = []
  for segment in segments:
    if segment.locked and not include_locked:
      continue
    if not segment.files:
      continue
    if segment.private_segment_uploaded and not force:
      continue
    filtered.append(segment)

  filtered.sort(key=lambda item: (item.mtime_ns, item.name), reverse=newest_first)
  if limit_segments is not None:
    filtered = filtered[:limit_segments]
  return filtered


def load_config(params: "Params") -> UploaderConfig | None:
  from openpilot.system.hardware.hw import Paths

  endpoint = os.getenv("DASHCAM_PRIVATE_ENDPOINT") or params.get("PrivateDashcamEndpoint")
  token = os.getenv("DASHCAM_PRIVATE_TOKEN") or params.get("PrivateDashcamToken")
  if not endpoint or not token:
    return None

  device_id = (
    os.getenv("DASHCAM_DEVICE_ID")
    or params.get("PrivateDashcamDeviceId")
    or params.get("DongleId")
    or params.get("HardwareSerial")
    or "comma"
  )
  log_root = Path(os.getenv("DASHCAM_LOG_ROOT", Paths.log_root()))
  timeout = float(os.getenv("DASHCAM_PRIVATE_TIMEOUT", DEFAULT_TIMEOUT))

  try:
    return UploaderConfig(
      endpoint=endpoint,
      token=token,
      device_id=validate_safe_id(device_id, "device_id"),
      log_root=log_root,
      timeout=timeout,
    )
  except ValueError:
    get_cloudlog().exception("invalid private dashcam uploader config")
    return None


def parse_segment_dir_name(name: str) -> tuple[str, int] | None:
  route_id, separator, segment_text = name.rpartition("--")
  if not separator:
    return None

  try:
    route_id = validate_safe_id(route_id, "route_id")
    segment = int(segment_text)
  except ValueError:
    return None

  if segment < 0 or segment > 999_999:
    return None
  return route_id, segment


def validate_safe_id(value: str, field_name: str) -> str:
  if not SAFE_ID_RE.fullmatch(value):
    raise ValueError(f"invalid {field_name}: {value}")
  if ".." in value or "/" in value or "\\" in value:
    raise ValueError(f"invalid {field_name}: {value}")
  return value


def sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(DEFAULT_CHUNK_SIZE), b""):
      digest.update(chunk)
  return digest.hexdigest()


def xattr_is_true(path: Path, name: str) -> bool:
  try:
    return os.getxattr(path, name) == UPLOAD_ATTR_VALUE
  except (AttributeError, OSError):
    return False


def set_xattr_true(path: Path, name: str) -> None:
  os.setxattr(path, name, UPLOAD_ATTR_VALUE)


def get_max_segments_per_run(params: "Params") -> int:
  value = os.getenv("DASHCAM_PRIVATE_MAX_SEGMENTS_PER_RUN")
  if value is None:
    value = params.get(MAX_SEGMENTS_PER_RUN_PARAM)

  try:
    return max(int(value or 0), 0)
  except (TypeError, ValueError):
    return 0


def stop_after_run_limit(params: "Params", *, uploaded_segments: int, max_segments_per_run: int) -> bool:
  if max_segments_per_run <= 0 or uploaded_segments < max_segments_per_run:
    return False

  params.put_bool("PrivateDashcamEnabled", False)
  get_cloudlog().event(
    "private_dashcam_upload_limit_reached",
    uploaded_segments=uploaded_segments,
    max_segments_per_run=max_segments_per_run,
  )
  return True


def should_run_private_dashcam_uploader(started: bool, params: "Params") -> bool:
  if not params.get_bool("PrivateDashcamEnabled"):
    return False
  if started and not params.get_bool("PrivateDashcamUploadOnroad"):
    return False
  return load_config(params) is not None


def main(exit_event: threading.Event | None = None) -> None:
  from cereal import log
  import cereal.messaging as messaging
  from openpilot.common.params import Params
  from openpilot.common.realtime import set_core_affinity

  if exit_event is None:
    exit_event = threading.Event()

  try:
    set_core_affinity([0, 1, 2, 3])
  except Exception:
    get_cloudlog().exception("failed to set core affinity")

  NetworkType = log.DeviceState.NetworkType
  params = Params()
  sm = messaging.SubMaster(["deviceState"])
  uploader: PrivateDashcamUploader | None = None
  active_config: UploaderConfig | None = None
  uploaded_segments = 0
  backoff = 0.1

  while not exit_event.is_set():
    sm.update(0)

    if not params.get_bool("PrivateDashcamEnabled") and os.getenv("DASHCAM_PRIVATE_ENDPOINT") is None:
      if allow_sleep:
        time.sleep(DEFAULT_SLEEP_WHEN_IDLE)
      continue

    config = load_config(params)
    if config is None:
      get_cloudlog().info("private dashcam uploader missing endpoint, token, or device id")
      if allow_sleep:
        time.sleep(DEFAULT_SLEEP_WHEN_IDLE)
      continue

    if config != active_config:
      active_config = config
      uploader = PrivateDashcamUploader(config)
      uploaded_segments = 0

    network_type = sm["deviceState"].networkType if not force_wifi else NetworkType.wifi
    if network_type == NetworkType.none:
      if allow_sleep:
        time.sleep(60 if params.get_bool("IsOffroad") else 5)
      continue

    if sm["deviceState"].networkMetered and not params.get_bool("PrivateDashcamUploadMetered"):
      if allow_sleep:
        time.sleep(DEFAULT_SLEEP_WHEN_IDLE)
      continue

    try:
      result = uploader.step() if uploader is not None else None
    except Exception as e:
      result = None
      success = False
      get_cloudlog().event("private_dashcam_upload_failed", exc=(e, traceback.format_exc()))
    else:
      success = result is not None and result.status != "failed"
      if result is not None and result.status == "uploaded":
        uploaded_segments += 1

    max_segments_per_run = get_max_segments_per_run(params)
    if stop_after_run_limit(params, uploaded_segments=uploaded_segments, max_segments_per_run=max_segments_per_run):
      break

    if result is None:
      backoff = 60 if params.get_bool("IsOffroad") else 5
    elif success:
      backoff = 0.1
    else:
      backoff = min(backoff * 2, 120)

    if allow_sleep:
      time.sleep(backoff + random.uniform(0, backoff))


if __name__ == "__main__":
  main()
