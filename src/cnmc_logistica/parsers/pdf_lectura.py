"""Lectura de un PDF como palabras con coordenadas.

- Si la página tiene capa de texto se usa tal cual (pdfplumber): es exacta.
- Solo si una página NO tiene ninguna palabra se intenta OCR (Tesseract, spa).
  Lo que sale del OCR se marca con metodo='ocr' para que quien lo use pueda
  avisar y revisarlo. Nunca se pasa OCR a una página que ya tiene texto.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from functools import lru_cache

import pdfplumber

TOLERANCIA_LINEA = 3.0  # puntos: los superíndices («m 3») quedan en su línea
PPP_OCR = 300


class PdfIlegible(Exception):
    """El fichero no es un PDF o no se puede abrir."""


@dataclass(frozen=True)
class Palabra:
    texto: str
    pagina: int
    x0: float
    top: float
    x1: float
    bottom: float
    negrita: bool = False
    vertical: bool = False
    metodo: str = "texto"  # "texto" | "ocr"

    @property
    def centro(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def clave(self) -> tuple[int, float, float, str]:
        """Identifica la palabra en la página (para la comprobación de cobertura)."""
        return (self.pagina, round(self.x0, 1), round(self.top, 1), self.texto)


@dataclass
class Pagina:
    numero: int
    ancho: float
    alto: float
    palabras: list[Palabra] = field(default_factory=list)
    metodo: str = "texto"  # "texto" | "ocr" | "vacia"
    cajas_tablas: list[tuple[float, float, float, float]] = field(default_factory=list)
    rotulo_vertical: str | None = None  # texto girado 90° (p. ej. «Jet A-1» en un margen)

    def palabras_con_cifras(self) -> list[Palabra]:
        """Todas las palabras con algún dígito (tras unir decimales partidos)."""
        en_lineas = [p for ln in self.lineas() for p in ln]
        verticales = [p for p in self.palabras if p.vertical]
        return [p for p in en_lineas + verticales if tiene_digito(p.texto)]

    def lineas(self) -> list[list[Palabra]]:
        """Palabras horizontales agrupadas por línea, de arriba abajo y de izquierda a derecha."""
        lineas: list[list[Palabra]] = []
        for p in sorted((p for p in self.palabras if not p.vertical), key=lambda p: p.top):
            if lineas and abs(p.top - lineas[-1][0].top) <= TOLERANCIA_LINEA:
                lineas[-1].append(p)
            else:
                lineas.append([p])
        return [_unir_decimales(sorted(ln, key=lambda p: p.x0)) for ln in lineas]


def _unir_decimales(linea: list[Palabra]) -> list[Palabra]:
    """«0» + «,5462» (un espacio colado en el PDF) → «0,5462»."""
    salida: list[Palabra] = []
    for p in linea:
        previa = salida[-1] if salida else None
        if (
            previa is not None
            and re.fullmatch(r"\d+", previa.texto)
            and re.fullmatch(r",\d+", p.texto)
            and p.x0 - previa.x1 < 15
        ):
            salida[-1] = Palabra(
                previa.texto + p.texto, p.pagina, previa.x0, previa.top, p.x1,
                max(previa.bottom, p.bottom), previa.negrita, False, previa.metodo,
            )  # fmt: skip
        else:
            salida.append(p)
    return salida


def texto_de(palabras: list[Palabra]) -> str:
    """Texto de una línea; recompone la unidad «m 3» (superíndice) como «m3»."""
    texto = " ".join(p.texto for p in palabras)
    return re.sub(r"\bm 3\b", "m3", texto).replace("( ", "(").replace(" )", ")")


def tiene_digito(texto: str) -> bool:
    return any(c.isdigit() for c in texto)


def _palabras_ocr(pagina, numero: int) -> list[Palabra] | None:
    """Palabras por OCR, o None si Tesseract no está disponible o falla."""
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        imagen = pagina.to_image(resolution=PPP_OCR).original
        # --psm 6: un bloque de texto uniforme. El modo por defecto de Tesseract se
        # salta las cifras de las celdas de una tabla.
        datos = pytesseract.image_to_data(
            imagen, lang="spa", config="--psm 6", output_type=pytesseract.Output.DICT
        )
    except Exception:
        return None
    escala = 72 / PPP_OCR
    palabras = []
    for i, texto in enumerate(datos["text"]):
        if not texto.strip():
            continue
        x, y = datos["left"][i] * escala, datos["top"][i] * escala
        ancho, alto = datos["width"][i] * escala, datos["height"][i] * escala
        palabras.append(Palabra(texto.strip(), numero, x, y, x + ancho, y + alto, metodo="ocr"))
    return palabras


@lru_cache(maxsize=64)  # el mismo PDF se repite entre fotos; el resultado no se modifica
def leer_paginas(datos: bytes) -> tuple[Pagina, ...]:
    if not datos.lstrip().startswith(b"%PDF"):
        raise PdfIlegible("el contenido no empieza por %PDF")
    try:
        pdf = pdfplumber.open(io.BytesIO(datos))
    except Exception as exc:
        raise PdfIlegible(str(exc)) from exc
    paginas: list[Pagina] = []
    with pdf:
        for numero, pagina in enumerate(pdf.pages, start=1):
            pag = Pagina(numero, float(pagina.width), float(pagina.height))
            crudas = pagina.extract_words(extra_attrs=["fontname", "size", "upright"])
            for w in crudas:
                if w["size"] <= 5:
                    continue  # marcas invisibles de 1 pt
                pag.palabras.append(
                    Palabra(
                        w["text"], numero, float(w["x0"]), float(w["top"]), float(w["x1"]),
                        float(w["bottom"]), "Bold" in w["fontname"], not w["upright"],
                    )
                )  # fmt: skip
            if not pag.palabras:
                ocr = _palabras_ocr(pagina, numero)
                if ocr:
                    pag.palabras, pag.metodo = ocr, "ocr"
                else:
                    pag.metodo = "vacia"
            else:
                pag.cajas_tablas = [tuple(t.bbox) for t in pagina.find_tables()]
                girados = [c for c in pagina.chars if not c.get("upright", True)]
                if girados:  # se lee de abajo arriba
                    letras = sorted(girados, key=lambda c: -c["top"])
                    pag.rotulo_vertical = "".join(c["text"] for c in letras).strip() or None
            paginas.append(pag)
    return tuple(paginas)
