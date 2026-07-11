import pathlib
import shutil
import tempfile

import pytest


@pytest.fixture
def sock_dir():
    """Create a short-path temp directory for unix socket binding.

    On macOS, the default TMPDIR can make socket paths exceed the
    ~104-byte AF_UNIX sun_path limit. This fixture uses /tmp directly
    to keep paths short and portable.
    """
    tmpdir = tempfile.mkdtemp(prefix="hw-", dir="/tmp")
    yield pathlib.Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)
