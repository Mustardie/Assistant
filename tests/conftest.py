import pytest

from config.settings import settings


@pytest.fixture(autouse=True)
def skills_dir_isolated(tmp_path):
    """Every test redirects the skills storage root to a temp folder so no
    test ever reads or writes the user's real Downloads/Nova/Skills."""
    original = settings.skills_dir
    object.__setattr__(settings, "skills_dir", str(tmp_path / "skills"))
    yield
    object.__setattr__(settings, "skills_dir", original)
