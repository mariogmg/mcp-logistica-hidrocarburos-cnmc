"""Lectura de los ficheros de correspondencias de config/.

- categorias_servicio.yaml: etiqueta (+ tipo de instalación) → categoría.
- indicadores_incluye.yaml: texto de «Incluye:» → indicadores del servicio.

En ambos casos la correspondencia es exacta y revisable; el código no deduce
nada con expresiones regulares. Lo que no está en el fichero se marca y avisa.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

SIN_CATEGORIA = "sin_categoria"
INDICADORES = (
    "incluye_transporte",
    "incluye_almacenamiento",
    "incluye_expedicion",
    "incluye_puesta_a_bordo",
)


def _clave(etiqueta: str) -> str:
    return re.sub(r"\s+", " ", etiqueta).strip().casefold()


class Categorizador:
    def __init__(self, datos: dict):
        self.categorias: dict[str, str] = dict(datos.get("categorias") or {})
        self._etiquetas = {_clave(k): v for k, v in (datos.get("etiquetas") or {}).items()}
        self._por_tipo = {
            tipo: {_clave(k): v for k, v in (tabla or {}).items()}
            for tipo, tabla in (datos.get("por_tipo_instalacion") or {}).items()
        }
        usadas = set(self._etiquetas.values())
        for tabla in self._por_tipo.values():
            usadas |= set(tabla.values())
        if sobran := usadas - set(self.categorias):
            raise ValueError(f"categorías usadas pero no declaradas: {sorted(sobran)}")

    @classmethod
    def de_fichero(cls, ruta: Path) -> Categorizador:
        return cls(yaml.safe_load(ruta.read_text(encoding="utf-8")) or {})

    def categoria(self, etiqueta: str, tipo_instalacion: str | None = None) -> tuple[str, bool]:
        """(categoría, conocida). Desconocida → («sin_categoria», False)."""
        clave = _clave(etiqueta)
        if tipo_instalacion and clave in self._por_tipo.get(tipo_instalacion, {}):
            return self._por_tipo[tipo_instalacion][clave], True
        if clave in self._etiquetas:
            return self._etiquetas[clave], True
        return SIN_CATEGORIA, False


class Indicadores:
    def __init__(self, datos: dict):
        declarados = tuple(datos.get("indicadores") or ())
        if set(declarados) != set(INDICADORES):
            raise ValueError(f"los indicadores deben ser exactamente {INDICADORES}")
        self._textos: dict[str, frozenset[str]] = {}
        for texto, lista in (datos.get("textos") or {}).items():
            activos = frozenset(lista or ())
            if sobran := activos - set(INDICADORES):
                raise ValueError(f"indicador desconocido en «{texto}»: {sorted(sobran)}")
            self._textos[_clave(texto)] = activos

    @classmethod
    def de_fichero(cls, ruta: Path) -> Indicadores:
        return cls(yaml.safe_load(ruta.read_text(encoding="utf-8")) or {})

    def de_servicio(self, textos: list[str]) -> tuple[dict[str, int], list[str]]:
        """({indicador: 0|1}, textos que no están en el fichero)."""
        activos: set[str] = set()
        desconocidos: list[str] = []
        for texto in textos:
            clave = _clave(texto)
            if clave in self._textos:
                activos |= self._textos[clave]
            else:
                desconocidos.append(texto)
        return {i: int(i in activos) for i in INDICADORES}, desconocidos
