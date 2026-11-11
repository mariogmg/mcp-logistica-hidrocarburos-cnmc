"""Números, fechas, precios y porcentajes tal como los escribe el portal."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Vocabulario cerrado de unidades. Lo que siga a la unidad es una observación
# («€/m3 Gasolina BOB» → unidad «€/m3», observación «Gasolina BOB»). Una unidad
# que no esté aquí no se adivina: se guarda entera y se avisa.
UNIDADES = (
    "€/m3 y mes",
    "€/m3 y día",
    "€/m3 y semana",
    "€/m3 y ºC",
    "€/Tm y ºC",
    "€/m3 suministrado",
    "€/Tm recepcionada",
    "€/operación",
    "€/m3",
    "€/Tm",
)
_UNIDADES_ORDENADAS = sorted(UNIDADES, key=len, reverse=True)

_PRECIO = re.compile(
    r"""Precio\s*
        (?P<pre>m[aá]x\.?|m[ií]n\.?)?\s*          # «Precio max. (fecha): 4,12 …»
        (?:\(\s*(?P<fecha>\d{1,2}/\d{1,2}/\d{4})\s*\))?\s*:\s*
        (?P<post>m[aá]x\.?|m[ií]n\.?)?\s*         # «Precio (fecha): Max. 5,15 …»
        (?P<num>\d[\d.]*(?:,\d+)?)(?![\d/])\s*
        (?P<cola>.*?)\s*(?=Precio\b|$)""",
    re.I | re.X,
)
_NUM = r"\d[\d.]*(?:,\d+)?"
_PORCENTAJE = re.compile(rf"^(?P<num>{_NUM})\s*%\s*(?P<resto>.*)$")
_MAX_MIN_PCT = re.compile(
    rf"^m[aá]x\.?\s*(?P<max>{_NUM})\s*%\s*m[ií]n\.?\s*(?P<min>{_NUM})\s*%\s*(?P<resto>.*)$", re.I
)


@dataclass(frozen=True)
class Valor:
    tipo_valor: str  # "unico" | "max" | "min"
    valor: float
    fecha: str | None  # ISO; vigencia que publica la CNMC
    unidad: str
    observacion: str | None = None


def numero(texto: str) -> float:
    """«1.180,565» → 1180.565 (coma decimal, punto de millares)."""
    return float(texto.replace(".", "").replace(",", "."))


def fecha_iso(texto: str) -> str:
    """«1/4/2026» o «01/04/2026» → «2026-04-01»."""
    dia, mes, anio = (int(x) for x in texto.split("/"))
    return f"{anio:04d}-{mes:02d}-{dia:02d}"


def sin_parentesis(texto: str) -> str:
    texto = texto.strip()
    if texto.startswith("(") and texto.endswith(")"):
        texto = texto[1:-1].strip()
    return texto


def partir_unidad(cola: str) -> tuple[str, str | None, bool]:
    """(unidad, observación, conocida). `cola` es lo que sigue al número."""
    cola = cola.strip()
    for unidad in _UNIDADES_ORDENADAS:
        if cola.casefold().startswith(unidad.casefold()):
            resto = cola[len(unidad) :]
            if resto and not resto[0].isspace() and resto[0] != "(":
                continue  # p. ej. «€/m3x»: no es esta unidad
            return unidad, sin_parentesis(resto) or None, True
    return cola, None, False


def extraer_precios(texto: str) -> tuple[list[Valor], str, list[str]]:
    """Devuelve (valores, texto que no es precio, unidades desconocidas)."""
    valores: list[Valor] = []
    desconocidas: list[str] = []
    for m in _PRECIO.finditer(texto):
        marca = (m.group("pre") or m.group("post") or "").lower()
        tipo = "max" if marca.startswith("m") and marca[1] in "aá" else "min" if marca else "unico"
        unidad, observacion, conocida = partir_unidad(m.group("cola"))
        if not conocida:
            desconocidas.append(unidad)
        valores.append(
            Valor(
                tipo_valor=tipo,
                valor=numero(m.group("num")),
                fecha=fecha_iso(m.group("fecha")) if m.group("fecha") else None,
                unidad=unidad,
                observacion=observacion,
            )
        )
    resto = re.sub(r"\s+", " ", _PRECIO.sub(" ", texto)).strip()
    return valores, resto, desconocidas


def extraer_porcentajes(texto: str) -> list[tuple[str | None, float | None, str | None]]:
    """Lista de (tipo_valor, valor, texto) de una merma o condición.

    «0,1% (si no…)»            → [('unico', 0.1, 'si no…')]
    «Max. 0,30 % Min. 0,20 %»  → [('max', 0.3, None), ('min', 0.2, None)]
    «Según legislación vigente» → [(None, None, 'Según legislación vigente')]
    Solo se extrae número cuando el texto empieza por él; «Revisión anual
    según % IPC» o «+/- 5%» se quedan como texto.
    """
    texto = texto.strip()
    if m := _MAX_MIN_PCT.match(texto):
        resto = sin_parentesis(m.group("resto")) or None
        return [("max", numero(m.group("max")), resto), ("min", numero(m.group("min")), resto)]
    if m := _PORCENTAJE.match(texto):
        return [("unico", numero(m.group("num")), sin_parentesis(m.group("resto")) or None)]
    return [(None, None, texto or None)]
