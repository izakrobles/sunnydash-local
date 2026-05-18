import hashlib
import json
import os
from pathlib import Path

import pytest

from openpilot.sunnypilot.private_dashcam.uploader import (
  PRIVATE_SEGMENT_UPLOAD_ATTR_NAME,
  PRIVATE_UPLOAD_ATTR_NAME,
  PRESERVE_ATTR_NAME,
  DeviceSegment,
  SegmentFile,
  build_manifest,
  discover_segments,
  filter_segments,
  parse_segment_dir_name,
  upload_segment,
  xattr_is_true,
)


class FakeUploader:
  def __init__(self):
    self.files = []
    self.bytes = []

  def upload_file(self, *, segment, file):
    self.files.append((segment.name, file.filename, file.sha256, file.size_bytes))
    return 201, "stored"

  def upload_bytes(self, *, segment, filename, body):
    self.bytes.append((segment.name, filename, body))
    return 201, "stored"


def set_xattr_or_skip(path: Path, name: str, value: bytes) -> None:
  try:
    os.setxattr(path, name, value)
  except (AttributeError, OSError) as exc:
    pytest.skip(f"xattrs are not available in this filesystem: {exc}")


def write_segment_file(root: Path, segment_name="0000013b--f0814c8efa--75", filename="qcamera.ts", body=b"qcamera") -> Path:
  path = root / segment_name / filename
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(body)
  return path


def test_parse_segment_dir_name_accepts_openpilot_route_formats():
  assert parse_segment_dir_name("0000013b--f0814c8efa--75") == ("0000013b--f0814c8efa", 75)
  assert parse_segment_dir_name("2026-05-18--13-45-01--12") == ("2026-05-18--13-45-01", 12)
  assert parse_segment_dir_name("not-a-segment") is None
  assert parse_segment_dir_name("route--bad") is None


def test_discover_segments_reads_media_locks_and_bookmark_xattr(tmp_path):
  media = write_segment_file(tmp_path, body=b"video")
  write_segment_file(tmp_path, filename="notes.txt", body=b"ignored")
  write_segment_file(tmp_path, filename="qcamera.ts.lock", body=b"")
  set_xattr_or_skip(media.parent, PRESERVE_ATTR_NAME, b"1")

  segments = discover_segments(tmp_path)

  assert len(segments) == 1
  assert segments[0].name == "0000013b--f0814c8efa--75"
  assert segments[0].route_id == "0000013b--f0814c8efa"
  assert segments[0].segment == 75
  assert segments[0].locked is True
  assert segments[0].bookmarked is True
  assert [file.filename for file in segments[0].files] == ["qcamera.ts"]


def test_filter_segments_skips_locked_and_already_uploaded_by_default(tmp_path):
  unlocked = write_segment_file(tmp_path, segment_name="0000013b--f0814c8efa--75")
  locked = write_segment_file(tmp_path, segment_name="0000013b--f0814c8efa--76")
  write_segment_file(tmp_path, segment_name="0000013b--f0814c8efa--76", filename="qcamera.ts.lock", body=b"")
  uploaded = write_segment_file(tmp_path, segment_name="0000013b--f0814c8efa--77")
  set_xattr_or_skip(uploaded.parent, PRIVATE_SEGMENT_UPLOAD_ATTR_NAME, b"1")

  segments = discover_segments(tmp_path)
  selected = filter_segments(segments, include_locked=False, force=False, newest_first=False, limit_segments=None)

  assert [segment.name for segment in selected] == [unlocked.parent.name]
  assert locked.parent.name not in [segment.name for segment in selected]


def test_upload_segment_posts_files_manifest_and_sets_private_xattrs_only(tmp_path):
  qcamera = write_segment_file(tmp_path, body=b"qcamera")
  fcamera = write_segment_file(tmp_path, filename="fcamera.hevc", body=b"fcamera")
  segment = discover_segments(tmp_path)[0]
  uploader = FakeUploader()

  result = upload_segment(uploader=uploader, segment=segment, device_id="comma4", mark_uploaded=True)

  assert result.status == "uploaded"
  assert [(name, filename) for name, filename, _, _ in uploader.files] == [
    ("0000013b--f0814c8efa--75", "fcamera.hevc"),
    ("0000013b--f0814c8efa--75", "qcamera.ts"),
  ]
  manifest = json.loads(uploader.bytes[0][2])
  assert manifest["device_id"] == "comma4"
  assert manifest["route_id"] == "0000013b--f0814c8efa"
  assert manifest["segment"] == 75
  assert sorted(file["filename"] for file in manifest["files"]) == ["fcamera.hevc", "qcamera.ts"]
  assert xattr_is_true(qcamera, PRIVATE_UPLOAD_ATTR_NAME)
  assert xattr_is_true(fcamera, PRIVATE_UPLOAD_ATTR_NAME)
  assert xattr_is_true(qcamera.parent, PRIVATE_SEGMENT_UPLOAD_ATTR_NAME)
  assert not xattr_is_true(qcamera, "user.upload")


def test_upload_segment_is_idempotent_when_private_xattrs_exist(tmp_path):
  qcamera = write_segment_file(tmp_path, body=b"qcamera")
  set_xattr_or_skip(qcamera, PRIVATE_UPLOAD_ATTR_NAME, b"1")
  set_xattr_or_skip(qcamera.parent, PRIVATE_SEGMENT_UPLOAD_ATTR_NAME, b"1")
  segment = discover_segments(tmp_path)[0]
  uploader = FakeUploader()

  result = upload_segment(uploader=uploader, segment=segment, device_id="comma4", mark_uploaded=True)

  assert result.status == "already_private_uploaded"
  assert uploader.files == []
  assert uploader.bytes == []
  assert result.files[0].sha256 == hashlib.sha256(b"qcamera").hexdigest()


def test_build_manifest_includes_bookmark_and_hashes(tmp_path):
  qcamera = write_segment_file(tmp_path, body=b"qcamera")
  segment = DeviceSegment(
    name="0000013b--f0814c8efa--75",
    route_id="0000013b--f0814c8efa",
    segment=75,
    path=qcamera.parent,
    locked=False,
    bookmarked=True,
    private_segment_uploaded=False,
    mtime_ns=1,
    files=[SegmentFile(filename="qcamera.ts", path=qcamera, size_bytes=len(b"qcamera"))],
  )

  manifest = json.loads(build_manifest(segment, device_id="comma4-test"))

  assert manifest["device_id"] == "comma4-test"
  assert manifest["bookmarked"] is True
  assert manifest["files"][0]["sha256"] == hashlib.sha256(b"qcamera").hexdigest()

