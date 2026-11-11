"""Lectura de config/categorias_servicio.yaml (etiqueta → categoría)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

SIN_CATEGORIA = "sin_categoria"


def _clave(etiqueta: str) -> str:
    return re.sub(r"\s+", " ", etiqueta).strip().casefold()


class Categorizador:
    def __init__(self, datos: dict):
        self.categorias: dict[str, str] = dict(datos.get("categorias") or {})
        self._etiquetas = {_clave(k): v for k, v in (datos.get("etiquetas") or {}).items()}
        self._por_operador = {
            op: {_clave(k): v for k, v in (tabla or {}).items()}
            for op, tabla in (datos.get("por_operador") or {}).items()
        }
        usadas = set(self._etiquetas.values())
        for tabla in self._por_operador.values():
            usadas |= set(tabla.values())
        if sobran := usadas - set(self.categorias):
            raise ValueError(f"categorías usadas pero no declaradas: {sorted(sobran)}")

    @classmethod
    def de_fichero(cls, ruta: Path) -> Categorizador:
        return cls(yaml.safe_load(ruta.read_text(encoding="utf-8")) or {})

    def categoria(self, etiqueta: str, operador: str | None = None) -> tuple[str, bool]:
        """(categoría, conocida). Desconocida → («sin_categoria», False)."""
        clave = _clave(etiqueta)
        if operador and clave in self._por_operador.get(operador, {}):
            return self._por_operador[operador][clave], True
        if clave in self._etiquetas:
            return self._etiquetas[clave], True
        return SIN_CATEGORIA, False
