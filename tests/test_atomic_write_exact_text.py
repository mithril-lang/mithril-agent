"""Byte-versioned native data can explicitly opt out of host newline conversion."""
import pytest
from utils import atomic_write_text

pytestmark = pytest.mark.platforms("any")


def test_exact_text_atomic_writer_retains_mixed_newlines_and_utf8_bytes(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    path = directory / "original.json"
    path.write_bytes(b"previous")
    target = 'first\r\nsecond\n日本語\r\n'
    atomic_write_text(path, target, newline="", preserve_mode=True, fsync_dir=True)
    assert path.read_bytes() == target.encode("utf-8")
    assert list(directory.iterdir()) == [path]
