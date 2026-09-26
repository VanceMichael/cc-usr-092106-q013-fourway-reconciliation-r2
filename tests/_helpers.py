"""测试夹具装载辅助。"""

from datetime import date
from pathlib import Path

from src.loader import load_bundle
from src.store import EvidenceStore

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "evidence"


def load_case(name: str) -> tuple:
    bundle = load_bundle(FIXTURE_DIR / name)
    store = EvidenceStore()
    store.load(bundle)
    return bundle, store


AS_OF = date(2026, 9, 26)
