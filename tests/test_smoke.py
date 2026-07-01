import herdwatch

def test_version_present():
    assert isinstance(herdwatch.__version__, str)
    assert herdwatch.__version__
