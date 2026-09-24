import shutil
import tempfile
from pathlib import Path

from rpg_llm.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_campaign(name: str):
    """A throwaway copy of a fixture campaign, so evals never touch the real vault."""
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(FIXTURES / name, tmp / "campaigns" / name)
    return Vault(tmp).get(name)
