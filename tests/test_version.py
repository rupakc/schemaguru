import schemaguru
from schemaguru import __about__


def test_version() -> None:
    assert schemaguru.__version__ == "0.1.1" == __about__.__version__
    assert schemaguru.__all__[-1] == "__version__"  # the exact list: tests/unit/test_guru.py
