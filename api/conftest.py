import sys
from pathlib import Path

# Los tests importan `app.*`; el paquete vive junto a este archivo.
sys.path.insert(0, str(Path(__file__).parent))
