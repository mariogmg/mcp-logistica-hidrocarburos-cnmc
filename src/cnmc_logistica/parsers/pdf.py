"""Lectura de PDFs con pdfplumber.

- `analizar_pdf`: metadatos y tablas genéricas de cualquier PDF.
- `parsear_capacidades`: el PDF global de capacidades, que no tiene tablas con
  líneas; se lee por la posición y el tipo de letra de cada palabra.

Leer un PDF como texto plano mezcla las columnas; por eso todo se hace a
partir de las palabras con sus coordenadas. El OCR (Tesseract) es opcional y
solo se intenta en páginas sin capa de texto.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

import pdfplumber

from .valores import numero

logging.getLogger("pdfminer").setLevel(logging.ERROR)

_ANIO = re.compile(r"\b(20\d{2})\b")
_NUM = re.compile(r"\d{1,3}(?:\.\d{3})*,\d+")
_NUM_OD = re.compile(r"\d+,\d{2}")


class PdfIlegible(Exception):
    """El fichero no es un PDF o pdfplumber no puede abrirlo."""


@dataclass
class DocumentoPdf:
    paginas: int
    paginas_sin_texto: int
    titulo: str | None
    anio: int | None
    tablas: list[tuple[int, int, list[list[str]]]] = field(default_factory=list)  # pág, nº, filas
    lineas: list[str] = field(default_factory=list)
    ocr: bool = False


def _ocr(pagina) -> str | None:
    """Texto por OCR si pytesseract está instalado; si no, None."""
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        return pytesseract.image_to_string(pagina.to_image(resolution=300).original, lang="spa")
    except Exception:
        return None


def anio_de(*textos: str | None) -> int | None:
    for texto in textos:
        if texto and (m := _ANIO.search(texto)):
            return int(m.group(1))
    return None


@lru_cache(maxsize=64)  # el mismo PDF se repite entre fotos; el resultado no se modifica
def analizar_pdf(datos: bytes, nombre_archivo: str | None = None) -> DocumentoPdf:
    if not datos.lstrip().startswith(b"%PDF"):
        raise PdfIlegible("el contenido no empieza por %PDF")
    try:
        pdf = pdfplumber.open(io.BytesIO(datos))
    except Exception as exc:
        raise PdfIlegible(str(exc)) from exc
    with pdf:
        doc = DocumentoPdf(len(pdf.pages), 0, None, None)
        for n, pagina in enumerate(pdf.pages, start=1):
            texto = pagina.extract_text() or ""
            if not texto.strip():
                texto = _ocr(pagina) or ""
                if texto.strip():
                    doc.ocr = True
                else:
                    doc.paginas_sin_texto += 1
            doc.lineas += [ln.strip() for ln in texto.splitlines() if ln.strip()]
            for i, tabla in enumerate(pagina.extract_tables(), start=1):
                filas = [[(c or "").strip() for c in fila] for fila in tabla]
                filas = [f for f in filas if any(f)]
                if filas:
                    doc.tablas.append((n, i, filas))
    doc.titulo = doc.lineas[0][:200] if doc.lineas else None
    # El año de la tabla sale del encabezado («PRECIOS AÑO 2026»); si no, del nombre.
    doc.anio = anio_de(" ".join(doc.lineas[:3]), nombre_archivo)
    return doc


def numeros_por_linea(doc: DocumentoPdf) -> list[list[str]]:
    """Importes «12,34» de cada línea de texto, para el control cruzado origen-destino."""
    return [nums for ln in doc.lineas if (nums := _NUM_OD.findall(ln))]


# --- capacidades -----------------------------------------------------------------


@dataclass
class Capacidad:
    ambito: str  # "almacenamiento" | "aeroportuaria"
    compania: str
    instalacion: str
    producto: str  # "TOTAL" = total de la instalación
    capacidad_miles_m3: float | None
    pagina: int


@dataclass
class Capacidades:
    anio: int | None
    filas: list[Capacidad] = field(default_factory=list)
    descuadres: list[str] = field(default_factory=list)  # compañías cuyo TOTAL no suma
    sin_interpretar: list[str] = field(default_factory=list)


@dataclass
class _Celda:
    texto: str
    valor: float | None
    x0: float
    top: float
    negrita: bool


def _celdas_de_linea(palabras: list[dict], hueco: float) -> list[_Celda]:
    """Parte una línea en celdas: un hueco ancho o un número cierran la celda."""
    celdas: list[_Celda] = []
    actual: list[dict] = []

    def cerrar(valor: float | None = None) -> None:
        if actual:
            celdas.append(
                _Celda(
                    " ".join(p["text"] for p in actual),
                    valor,
                    actual[0]["x0"],
                    actual[0]["top"],
                    all("Bold" in p["fontname"] for p in actual),
                )
            )
            actual.clear()
        elif valor is not None and celdas and celdas[-1].valor is None:
            celdas[-1].valor = valor

    previa = None
    for p in palabras:
        if _NUM.fullmatch(p["text"]):
            if actual:
                cerrar(numero(p["text"]))
            else:
                cerrar(numero(p["text"]))
            previa = None
            continue
        if previa is not None and p["x0"] - previa["x1"] > hueco:
            cerrar()
        actual.append(p)
        previa = p
    cerrar()
    return celdas


@lru_cache(maxsize=8)
def parsear_capacidades(datos: bytes) -> Capacidades:
    """Capacidades por compañía, instalación y producto (miles de m3).

    Estructura del PDF: bloques por compañía (título en negrita y centrado),
    dos columnas por bloque; cada instalación va en negrita con su total y sus
    productos debajo en letra normal; el bloque acaba en una línea «TOTAL».
    """
    if not datos.lstrip().startswith(b"%PDF"):
        raise PdfIlegible("el contenido no empieza por %PDF")
    resultado = Capacidades(None)
    ambito = "almacenamiento"
    compania: str | None = None
    bloque: list[_Celda] = []
    pagina_actual = 0
    mitad = 0.0

    def cerrar_bloque(total: float | None = None) -> None:
        if not bloque or compania is None:
            bloque.clear()
            return
        izquierda = sorted((c for c in bloque if c.x0 < mitad), key=lambda c: c.top)
        derecha = sorted((c for c in bloque if c.x0 >= mitad), key=lambda c: c.top)
        instalacion: str | None = None
        suma = 0.0
        # Bloque sin ninguna negrita (p. ej. aeropuertos de Exolum Aviation): es una
        # lista plana de instalaciones con su total, sin desglose por producto.
        lista_plana = not any(c.negrita for c in bloque)
        for c in izquierda + derecha:
            if c.negrita or lista_plana:
                if not re.search(r"[^\W\d]", c.texto):  # una cifra suelta, no un nombre
                    resultado.sin_interpretar.append(f"p{pagina_actual} {compania}: {c.texto}")
                    continue
                # Una instalación puede venir sin cifra (p. ej. «cierre temporal»).
                instalacion = c.texto
                suma += c.valor or 0.0
                resultado.filas.append(
                    Capacidad(ambito, compania, instalacion, "TOTAL", c.valor, pagina_actual)
                )
            elif instalacion is None:
                resultado.sin_interpretar.append(f"p{pagina_actual} {compania}: {c.texto}")
            else:
                resultado.filas.append(
                    Capacidad(ambito, compania, instalacion, c.texto, c.valor, pagina_actual)
                )
        if total is not None and abs(suma - total) > 0.0015:
            resultado.descuadres.append(f"{compania}: instalaciones {suma:.3f} ≠ TOTAL {total:.3f}")
        bloque.clear()

    with pdfplumber.open(io.BytesIO(datos)) as pdf:
        for n, pagina in enumerate(pdf.pages, start=1):
            cerrar_bloque()
            pagina_actual, mitad = n, pagina.width / 2
            palabras = pagina.extract_words(extra_attrs=["fontname", "size"])
            palabras = [p for p in palabras if p["size"] > 5]  # marcas invisibles de 1 pt
            lineas: list[list[dict]] = []
            for p in sorted(palabras, key=lambda p: (round(p["top"]), p["x0"])):
                if lineas and abs(p["top"] - lineas[-1][0]["top"]) <= 3:
                    lineas[-1].append(p)
                else:
                    lineas.append([p])
            for linea in lineas:
                linea.sort(key=lambda p: p["x0"])
                texto = " ".join(p["text"] for p in linea)
                if "Capacidad de almacenamiento" in texto:
                    resultado.anio = resultado.anio or anio_de(texto)
                    ambito = "aeroportuaria" if "aeroportuarias" in texto else "almacenamiento"
                    continue
                if texto.startswith("Instalaciones") and "por Compañías" in texto:
                    continue
                if texto.startswith(("Unidad:", "Fuente:")) or re.match(r"\(\d+\)\s+\D", texto):
                    cerrar_bloque()
                    continue  # pie y notas
                if texto == "PÚBLICA":
                    continue  # marca de clasificación del documento
                celdas = _celdas_de_linea(linea, hueco=pagina.width * 0.03)
                totales = [c for c in celdas if c.texto == "TOTAL"]
                if totales:
                    # El TOTAL de la compañía puede compartir línea con la última celda.
                    bloque.extend(c for c in celdas if c.texto != "TOTAL")
                    cerrar_bloque(totales[0].valor)
                    compania = None
                    continue
                if len(celdas) == 1 and celdas[0].negrita and celdas[0].valor is None:
                    cerrar_bloque()
                    compania = re.sub(r"(\s*\(\d+\))+$", "", celdas[0].texto).strip()
                    continue
                if compania is None:
                    resultado.sin_interpretar.append(f"p{n}: {texto}")
                    continue
                bloque.extend(celdas)
        cerrar_bloque()
    return resultado
