import schemaguru
from schemaguru import __about__


def test_version() -> None:
    assert schemaguru.__version__ == "0.1.0" == __about__.__version__
    assert schemaguru.__all__ == ["__version__"]
