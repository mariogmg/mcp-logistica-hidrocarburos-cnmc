"""Lectura de los ficheros de correspondencias de config/.

- categorias_servicio.yaml: etiqueta (+ tipo de instalación) → categoría.
- indicadores_incluye.yaml: texto de «Incluye:» → indicadores del servicio.
- capacidades_equivalencias.yaml: instalación del PDF de capacidades → idTanque.
- descartes_pdf.yaml: cifras de los PDF que no se guardan, con su motivo.

En ambos casos la correspondencia es exacta y revisable; el código no deduce
nada con expresiones regulares. Lo que no está en el fichero se marca y avisa.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .cobertura import Descartes

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
        self._conceptos_pdf = {_clave(k): v for k, v in (datos.get("conceptos_pdf") or {}).items()}
        usadas = set(self._etiquetas.values()) | set(self._conceptos_pdf.values())
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

    def categoria_concepto_pdf(self, concepto: str) -> str | None:
        """Categoría propia de un concepto de un PDF, si el fichero la declara."""
        return self._conceptos_pdf.get(_clave(concepto))


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
        self._por_tipo: dict[str, dict[str, frozenset[str]]] = {}
        for tipo, tabla in (datos.get("por_tipo_instalacion") or {}).items():
            for texto, lista in (tabla or {}).items():
                activos = frozenset(lista or ())
                if sobran := activos - set(INDICADORES):
                    raise ValueError(f"indicador desconocido en «{texto}»: {sorted(sobran)}")
                self._por_tipo.setdefault(tipo, {})[_clave(texto)] = activos

    @classmethod
    def de_fichero(cls, ruta: Path) -> Indicadores:
        return cls(yaml.safe_load(ruta.read_text(encoding="utf-8")) or {})

    def de_servicio(
        self, textos: list[str], tipo_instalacion: str | None = None
    ) -> tuple[dict[str, int], list[str]]:
        """({indicador: 0|1}, textos que no están en el fichero).

        `textos` debe ser la lista «Incluye:» del servicio y nada más.
        """
        activos: set[str] = set()
        desconocidos: list[str] = []
        por_tipo = self._por_tipo.get(tipo_instalacion or "", {})
        for texto in textos:
            clave = _clave(texto)
            if clave in por_tipo:
                activos |= por_tipo[clave]
            elif clave in self._textos:
                activos |= self._textos[clave]
            else:
                desconocidos.append(texto)
        return {i: int(i in activos) for i in INDICADORES}, desconocidos


def _anotaciones(ruta: Path) -> tuple[tuple[str, str], ...]:
    if not ruta.exists():
        return ()
    datos = (yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}).get("anotaciones") or []
    return tuple((a["titulo_contiene"], a["texto"]) for a in datos)


class Equivalencias:
    """Instalación del PDF de capacidades → idTanque (tabla curada a mano)."""

    def __init__(self, datos: dict):
        self._tabla: dict[tuple[str, str, str], int | None] = {}
        for ambito, companias in (datos or {}).items():
            for compania, instalaciones in (companias or {}).items():
                for instalacion, id_tanque in (instalaciones or {}).items():
                    self._tabla[ambito, _clave(compania), _clave(instalacion)] = id_tanque

    @classmethod
    def de_fichero(cls, ruta: Path) -> Equivalencias:
        return cls(yaml.safe_load(ruta.read_text(encoding="utf-8")) or {})

    def id_tanque(self, ambito: str, compania: str, instalacion: str) -> tuple[int | None, bool]:
        """(idTanque, revisada). `revisada` es False si la instalación no está en la tabla."""
        clave = (ambito, _clave(compania), _clave(instalacion))
        return self._tabla.get(clave), clave in self._tabla


@dataclass(frozen=True)
class Configuracion:
    """Los tres ficheros de correspondencias de config/, ya leídos."""

    categorias: Categorizador
    indicadores: Indicadores
    equivalencias: Equivalencias
    descartes: Descartes
    alias_pdf: dict[str, str]  # nombre abreviado en un PDF → nombre completo
    anotaciones_pdf: tuple[tuple[str, str], ...] = ()  # (título contiene, texto inferido)

    @classmethod
    def de_directorio(cls, config: Path) -> Configuracion:
        return cls(
            Categorizador.de_fichero(config / "categorias_servicio.yaml"),
            Indicadores.de_fichero(config / "indicadores_incluye.yaml"),
            Equivalencias.de_fichero(config / "capacidades_equivalencias.yaml"),
            Descartes.de_fichero(config / "descartes_pdf.yaml"),
            (
                yaml.safe_load((config / "alias_instalaciones_pdf.yaml").read_text("utf-8")) or {}
            ).get("alias")
            or {},
            _anotaciones(config / "anotaciones_pdf.yaml"),
        )
