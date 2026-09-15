"""pytest가 저장소 루트를 sys.path에 올리게 한다 (src 임포트용)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
