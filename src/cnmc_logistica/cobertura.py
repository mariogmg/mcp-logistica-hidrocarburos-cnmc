"""Comprobación de cobertura de los PDF.

Pregunta que responde: ¿hay alguna cifra en un PDF que no haya llegado a la
base? Se recorren todas las palabras con dígitos de cada página y cada una
debe estar en uno de estos casos:

- usada por el parser como valor, etiqueta, nota o cabecera (está en la base);
- descartada por una regla justificada de config/descartes_pdf.yaml;
- total verificado (suma comprobada) en el PDF de capacidades.

Lo que no cae en ninguno queda «sin cubrir» y es un error de la ejecución.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .parsers.pdf_lectura import Pagina, texto_de


@dataclass(frozen=True)
class Regla:
    motivo: str
    linea: re.Pattern | None
    palabra: re.Pattern | None
    documentos: frozenset[str] | None


class Descartes:
    def __init__(self, datos: dict):
        self.reglas: list[Regla] = []
        for r in (datos or {}).get("reglas") or []:
            if not r.get("motivo") or not (r.get("linea") or r.get("palabra")):
                raise ValueError(f"regla de descarte incompleta: {r}")
            self.reglas.append(
                Regla(
                    r["motivo"],
                    re.compile(r["linea"]) if r.get("linea") else None,
                    re.compile(r["palabra"]) if r.get("palabra") else None,
                    frozenset(map(str, r["documentos"])) if r.get("documentos") else None,
                )
            )

    @classmethod
    def de_fichero(cls, ruta: Path) -> Descartes:
        return cls(yaml.safe_load(ruta.read_text(encoding="utf-8")) or {})

    def motivo(self, documento: str, palabra: str, linea: str) -> str | None:
        for r in self.reglas:
            if r.documentos is not None and documento not in r.documentos:
                continue
            if r.linea is not None and not r.linea.search(linea):
                continue
            if r.palabra is not None and not r.palabra.fullmatch(palabra):
                continue
            return r.motivo
        return None


@dataclass
class Cobertura:
    cifras: int = 0
    en_valores: int = 0
    en_etiquetas: int = 0
    en_notas: int = 0
    en_cabecera: int = 0
    descartadas: int = 0
    ilegibles: int = 0
    sin_cubrir: list[str] = field(default_factory=list)  # «pN: «cifra» en «línea»»

    def fila(self) -> dict:
        return {
            "cifras": self.cifras,
            "en_valores": self.en_valores,
            "en_etiquetas": self.en_etiquetas,
            "en_notas": self.en_notas,
            "en_cabecera": self.en_cabecera,
            "descartadas": self.descartadas,
            "ilegibles": self.ilegibles,
            "sin_cubrir": len(self.sin_cubrir),
        }


_CAMPO = {
    "valor": "en_valores",
    "etiqueta": "en_etiquetas",
    "nota": "en_notas",
    "cabecera": "en_cabecera",
    "ilegible": "ilegibles",
    "total_verificado": "descartadas",
}


def calcular(
    documento: str, paginas: tuple[Pagina, ...], usadas: dict[tuple, str], descartes: Descartes
) -> Cobertura:
    cob = Cobertura()
    for pagina in paginas:
        linea_de = {p.clave: texto_de(ln) for ln in pagina.lineas() for p in ln}
        for p in pagina.palabras_con_cifras():
            cob.cifras += 1
            clase = usadas.get(p.clave)
            linea = linea_de.get(p.clave, p.texto)
            if clase in _CAMPO:
                setattr(cob, _CAMPO[clase], getattr(cob, _CAMPO[clase]) + 1)
            elif descartes.motivo(documento, p.texto, linea):
                cob.descartadas += 1
            else:
                donde = f"p{pagina.numero} (x={p.x0:.0f}, y={p.top:.0f})"
                cob.sin_cubrir.append(f"{donde}: «{p.texto}» en «{linea[:120]}»")
    return cob
