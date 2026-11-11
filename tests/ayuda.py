"""Utilidades compartidas por los tests."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
RAIZ = Path(__file__).parent.parent


def leer(ruta: str) -> str:
    # Las fixtures son bytes reales del portal: UTF-8 aunque el <meta> diga ISO-8859-1.
    return (FIXTURES / ruta).read_text(encoding="utf-8")
