"""Parsers específicos de los PDF de precios de la CNMC.

Cada tipo de documento tiene su parser, elegido por el título del propio PDF
(no por su número de directorio, que puede cambiar de un año a otro). Todos
trabajan sobre palabras con coordenadas y devuelven una `Extraccion` con:

- `valores`        → tabla precios_pdf (tarifas concepto → precio)
- `celdas`         → tabla matrices_pdf (matrices origen × destino)
- `bonificaciones` → tabla bonificaciones
- `notas`          → tabla notas_pdf (texto íntegro de notas y condiciones)
- `usadas`         → qué palabras con cifras ha entendido el parser y cómo.
  Es la base de la comprobación de cobertura: una cifra del PDF que ningún
  parser haya usado y que no esté en la lista de descartes es un error.

Un número que no se puede interpretar con certeza nunca se inventa: se guarda
con valor nulo, su texto original y un aviso «numero_ilegible».
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .pdf_lectura import Pagina, Palabra, leer_paginas, texto_de, tiene_digito

NUM = re.compile(r"\d{1,3}(?:\.\d{3})*,\d+|\d+,\d+")  # coma decimal, punto de millares
NUM_PUNTO = re.compile(r"\d+\.\d{4}")  # «9.0526»: cuatro decimales no puede ser millares
ENTERO = re.compile(r"\d{1,3}(?:\.\d{3})+|\d+")
_ANIO = re.compile(r"\b(20\d{2})\b")
_PIE = re.compile(r"^Página \d+ de \d+$")
_VIGENCIA = re.compile(r"\(\s*(\d{1,2}-[A-Z]{3}-\d{4})\s*/\s*(\d{1,2}-[A-Z]{3}-\d{4})\s*\)")


@dataclass
class ValorPdf:
    seccion: str | None
    concepto: str
    subconcepto: str | None
    valor: float | None
    unidad: str | None
    palabra: Palabra
    texto_original: str
    condicion: str | None = None


@dataclass
class CeldaMatriz:
    tipo: str  # "origen_destino" | "reconsignacion" | "consignacion_ester"
    modalidad: str | None
    destino: str
    origen: str
    estado: str  # "ofrecida" | "no_aplica"
    precio: float | None
    unidad: str | None
    marca: str | None
    palabra: Palabra
    texto_original: str


@dataclass
class Bonificacion:
    bloque: str  # "bonificacion" | "compensacion"
    tramo: str
    condicion: str
    valor: float | None
    unidad: str
    palabra: Palabra
    texto_original: str


@dataclass
class Nota:
    pagina: int
    texto: str
    metodo: str


@dataclass
class CondicionPdf:
    """Una cantidad dentro de una nota: «Facturación mínima 500 toneladas» → 500 toneladas."""

    texto: str
    valor: float
    unidad: str
    palabra: Palabra


# Unidades que convierten una cifra de una nota en una condición con cantidad.
_UNIDADES_CONDICION = {
    "días": "días", "día": "días", "toneladas": "toneladas", "ºc": "ºC", "m3": "m3", "%": "%",
}  # fmt: skip


@dataclass
class Extraccion:
    parser: str
    anio: int | None = None
    vigencia: str | None = None
    valores: list[ValorPdf] = field(default_factory=list)
    celdas: list[CeldaMatriz] = field(default_factory=list)
    bonificaciones: list[Bonificacion] = field(default_factory=list)
    notas: list[Nota] = field(default_factory=list)
    condiciones: list[CondicionPdf] = field(default_factory=list)
    usadas: dict[tuple, str] = field(default_factory=dict)  # clave de palabra → clase
    avisos: list[tuple[str, str]] = field(default_factory=list)  # (tipo, texto)

    def marcar(self, palabras: list[Palabra], clase: str) -> None:
        for p in palabras:
            if tiene_digito(p.texto):
                self.usadas.setdefault(p.clave, clase)

    def valor(self, v: ValorPdf) -> None:
        self.usadas[v.palabra.clave] = "valor" if v.valor is not None else "ilegible"
        repetido = any(
            (o.seccion, o.concepto, o.subconcepto, o.valor, o.unidad)
            == (v.seccion, v.concepto, v.subconcepto, v.valor, v.unidad)
            for o in self.valores
        )
        if not repetido:  # la misma nota se repite al pie de cada página
            self.valores.append(v)

    def nota(self, linea: list[Palabra]) -> None:
        """Guarda una nota o condición y extrae los importes en €/m3 que contenga."""
        texto = texto_de(linea)
        cantidades = self._cantidades(linea, texto)
        if not cantidades:  # solo explicativa: se queda como nota
            self.notas.append(Nota(linea[0].pagina, texto, linea[0].metodo))
        self.marcar(linea, "nota")
        for i, p in enumerate(linea):
            siguiente = linea[i + 1].texto if i + 1 < len(linea) else ""
            if NUM.fullmatch(p.texto) and siguiente.startswith(("Euros/m", "€/m")):
                self.valor(
                    ValorPdf(None, "Suplemento o recargo (nota)", texto, numero(p.texto),
                             "€/m3", p, texto)
                )  # fmt: skip


def _cantidades_de(self: Extraccion, linea: list[Palabra], texto: str) -> int:
    """Pasa a `condiciones` las cantidades con unidad de una nota (días, toneladas, %…)."""
    encontradas = 0
    for i, p in enumerate(linea):
        if self.usadas.get(p.clave) == "valor":
            continue  # ya es un precio o un umbral
        cifra, pegada = p.texto.rstrip(".,;)"), ""
        if cifra.endswith("%"):
            cifra, pegada = cifra[:-1], "%"
        siguientes = [q.texto.rstrip(".,;)").casefold() for q in linea[i + 1 : i + 3]]
        if not pegada and siguientes:
            pegada = "m3" if siguientes[:2] == ["m", "3"] else siguientes[0]
        unidad = _UNIDADES_CONDICION.get(pegada)
        if unidad is None or not (NUM.fullmatch(cifra) or ENTERO.fullmatch(cifra)):
            continue
        valor = float(cifra.replace(".", "").replace(",", "."))
        encontradas += 1
        self.usadas[p.clave] = "valor"
        if not any(
            (c.texto, c.valor, c.unidad) == (texto, valor, unidad) for c in self.condiciones
        ):
            self.condiciones.append(CondicionPdf(texto, valor, unidad, p))
    return encontradas


Extraccion._cantidades = _cantidades_de


def numero(texto: str) -> float | None:
    """Valor de una cifra, o None si no se puede interpretar con certeza."""
    texto = texto.strip()
    if NUM.fullmatch(texto):
        return float(texto.replace(".", "").replace(",", "."))
    if NUM_PUNTO.fullmatch(texto):
        return float(texto)
    return None


def unidad_de(texto: str) -> str | None:
    t = texto.casefold()
    if "€/operación" in t:
        return "€/operación"
    if not ("euros/m3" in t or "€/m3" in t):
        return None
    if "y mes" in t:
        return "€/m3 y mes"
    if "por día" in t or "y día" in t:
        return "€/m3 y día"
    if "entregado" in t:
        return "€/m3 entregado"
    return "€/m3"


def _es_titulo(texto: str) -> bool:
    return (
        "PRECIOS" in texto
        or texto.startswith(("PARA ", "BONIFICACIONES", "COMPENSACIONES", "ALMACENADAS Y/O"))
        or texto.endswith(("UTILIZADO EN LA MEZCLA EN LÍNEA (1)", "GASOLINAS Y GASÓLEOS"))
    )


def _cabecera(ext: Extraccion, linea: list[Palabra], texto: str) -> None:
    ext.marcar(linea, "cabecera")
    if ext.anio is None and (m := _ANIO.search(texto)):
        ext.anio = int(m.group(1))
    if m := _VIGENCIA.search(texto):
        ext.vigencia = f"{m.group(1)} / {m.group(2)}"


def _lineas(paginas: tuple[Pagina, ...]):
    for pagina in paginas:
        for linea in pagina.lineas():
            texto = texto_de(linea)
            if not _PIE.match(texto):  # el nº de página queda para la lista de descartes
                yield pagina, linea, texto


# --- tarifas «concepto → precio» ----------------------------------------------------


def _concepto_valor(
    paginas: tuple[Pagina, ...],
    ext: Extraccion,
    *,
    concepto_fijo: str | None = None,
    otra: Callable[[list[Palabra], str], bool] | None = None,
) -> None:
    """Tablas de una columna de precios: «etiqueta … 12,34».

    Las líneas de solo texto entre filas forman la sección; la unidad sale de
    una línea que solo contiene la unidad («€/m3», «(Euros/m3)»…).
    """
    filas: list[tuple[str | None, list[Palabra], Palabra]] = []
    seccion: list[str] = []
    tras_fila = False
    unidad: str | None = None
    for _, linea, texto in _lineas(paginas):
        if _es_titulo(texto) and not NUM.fullmatch(linea[-1].texto):
            _cabecera(ext, linea, texto)
        elif re.fullmatch(r"\(?(€|Euros)/m3\)?( entregado)?", texto):
            unidad = unidad_de(texto)
            ext.marcar(linea, "etiqueta")
        elif (otra is not None and otra(linea, texto)) or texto in (
            "SERVICIO PRECIO",
            "PRODUCTO",
            "PRECIO",
            "SERVICIO",
        ):
            continue
        elif (
            len(linea) > 1
            and NUM.fullmatch(linea[-1].texto)
            and not any(NUM.fullmatch(p.texto) for p in linea[:-1])
        ):
            filas.append((" ".join(seccion) or None, linea[:-1], linea[-1]))
            tras_fila = True
        elif not any(tiene_digito(p.texto) for p in linea):
            seccion = [texto] if tras_fila else [*seccion, texto]
            tras_fila = False
        elif texto.startswith(("(", "*", "•")):
            ext.nota(linea)
    for sec, etiqueta, palabra in filas:
        ext.marcar(etiqueta, "etiqueta")
        nombre = texto_de(etiqueta)
        ext.valor(
            ValorPdf(
                seccion=None if concepto_fijo else sec,
                concepto=concepto_fijo or nombre,
                subconcepto=nombre if concepto_fijo else None,
                valor=numero(palabra.texto),
                unidad=unidad,
                palabra=palabra,
                texto_original=f"{nombre} {palabra.texto}",
            )
        )


def _disa(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    pendiente: list[ValorPdf] = []

    def otra(linea: list[Palabra], texto: str) -> bool:
        if texto.startswith("PRECIO POR ALMACENAMIENTO ADICIONAL"):
            i = next(k for k, p in enumerate(linea) if p.texto.endswith(":")) + 1
            palabra = linea[i]
            valor = numero(palabra.texto)
            if valor is None:
                # P. ej. «01021» (falta la coma): no se adivina dónde va.
                ext.avisos.append(("numero_ilegible", texto))
            ext.marcar(linea[:i], "etiqueta")
            ext.marcar(linea[i + 1 :], "etiqueta")
            v = ValorPdf(None, "Almacenamiento adicional", None, valor, unidad_de(texto),
                         palabra, texto, condicion=texto_de(linea[i + 1 :]))  # fmt: skip
            pendiente.append(v)
            ext.valor(v)
            return True
        if pendiente and texto.startswith("entre el volumen"):
            pendiente[-1].condicion = f"{pendiente[-1].condicion} {texto}"
            pendiente[-1].texto_original = f"{pendiente[-1].texto_original} {texto}"
            return True
        if texto.startswith("Los precios son aplicables"):
            ext.nota(linea)
            return True
        return texto == "PRECIOS"

    _concepto_valor(paginas, ext, concepto_fijo="Servicios logísticos", otra=otra)


# --- Terminales Canarios -------------------------------------------------------------


def _terca_basica(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    """Dos bloques por producto con dos columnas de precio (servicio | arrendamiento)."""
    columnas: list[tuple[float, str, str]] = []  # (x, título, unidad)
    titulos: list[Palabra] = []
    producto: list[str] = []
    margen = min(p.x0 for pag in paginas for p in pag.palabras)
    for _, linea, texto in _lineas(paginas):
        if "PRECIOS AÑO" in texto:
            _cabecera(ext, linea, texto)
        elif "(Euros/m" in texto:
            # Línea de unidades: fija las columnas; los títulos son la línea anterior.
            inicios = [p for p in linea if p.texto.startswith("(Euros/m")]
            columnas = []
            for k, p in enumerate(inicios):
                fin = inicios[k + 1].x0 if k + 1 < len(inicios) else float("inf")
                trozo = [q for q in linea if p.x0 <= q.x0 < fin]
                limite = (p.x0 + inicios[k - 1].x0) / 2 if k else 0.0
                limite_dcho = (p.x0 + inicios[k + 1].x0) / 2 if k + 1 < len(inicios) else 1e9
                titulo = " ".join(t.texto for t in titulos if limite <= t.centro < limite_dcho)
                columnas.append((p.centro, titulo, unidad_de(texto_de(trozo)) or "€/m3"))
            ext.marcar(linea, "etiqueta")
            producto = []
        elif all(p.texto.isupper() or p.texto in ("Y", "DE") for p in linea) and len(linea) > 2:
            titulos = [p for p in linea if p.texto != "PRODUCTO"]
        elif texto == "PRODUCTO":
            continue
        elif columnas and any(NUM.fullmatch(p.texto) for p in linea):
            etiqueta = [p for p in linea if not NUM.fullmatch(p.texto)]
            if etiqueta and etiqueta[0].x0 - margen < 5:
                producto.append(texto_de(etiqueta))
                etiqueta = []
            ext.marcar(etiqueta, "etiqueta")
            for p in linea:
                if NUM.fullmatch(p.texto):
                    _, titulo, unidad = min(columnas, key=lambda c: abs(c[0] - p.centro))
                    ext.valor(
                        ValorPdf(" ".join(producto) or None, titulo, texto_de(etiqueta) or None,
                                 numero(p.texto), unidad, p, texto)
                    )  # fmt: skip
        elif columnas and re.match(r"\(\d+\) ", texto):
            ext.nota(linea)
        elif columnas and not any(tiene_digito(p.texto) for p in linea):
            if linea[0].x0 - margen < 5:
                producto.append(texto)
    # El nombre del producto puede seguir en la línea de debajo del primer precio.
    for v in ext.valores:
        if v.seccion:
            completo = [
                s for s in {o.seccion for o in ext.valores if o.seccion} if s.startswith(v.seccion)
            ]
            v.seccion = max(completo, key=len)


def _vertical(pagina: Pagina) -> tuple[str | None, list[Palabra]]:
    """Rótulo girado 90° del margen (el producto) y sus palabras."""
    return pagina.rotulo_vertical, [p for p in pagina.palabras if p.vertical]


def _terca_primaria(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    producto, girado = _vertical(paginas[0])
    ext.marcar(girado, "etiqueta")
    grupo: list[str] = []
    abiertos: list[ValorPdf] = []
    for _, linea, texto in _lineas(paginas):
        if "PRECIOS AÑO" in texto:
            _cabecera(ext, linea, texto)
        elif texto.startswith("*"):
            ext.nota(linea)
        elif texto.isupper() or any(NUM.fullmatch(p.texto) for p in linea):
            etiqueta = [p for p in linea if (p.texto.isupper() and not tiene_digito(p.texto))
                        or p.texto in ("BB/TT", "CC/CC")]  # fmt: skip
            if texto.startswith("ARRENDAMIENTO"):
                grupo, abiertos = [], []  # servicio distinto del anterior
            grupo.append(texto_de(etiqueta))
            for v in abiertos:
                v.concepto = " / ".join(grupo)
            for p in linea:
                if NUM.fullmatch(p.texto):
                    ext.marcar(linea, "etiqueta")
                    v = ValorPdf(producto, " / ".join(grupo), None, numero(p.texto),
                                 unidad_de(texto), p, texto)  # fmt: skip
                    abiertos.append(v)
                    ext.valor(v)


# --- Exolum: tarifas sueltas -----------------------------------------------------------


def _ester_almacenamiento(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    for _, linea, texto in _lineas(paginas):
        if _es_titulo(texto):
            _cabecera(ext, linea, texto)
        elif m := re.match(r"(Almacenamiento de éster metílico): (\S+) (€/m3 y mes)", texto):
            palabra = next(p for p in linea if p.texto == m.group(2))
            ext.marcar(linea, "etiqueta")
            ext.valor(
                ValorPdf(None, m.group(1), None, numero(m.group(2)), m.group(3), palabra, texto)
            )
        elif m := re.match(r"(Capacidad mayor o igual que \S+ m3) (\d+)%$", texto):
            ext.marcar(linea[:-1], "etiqueta")
            ext.valor(
                ValorPdf(None, "Descuento por capacidad contratada", m.group(1),
                         float(m.group(2)), "%", linea[-1], texto)
            )  # fmt: skip
        elif tiene_digito(texto) or texto.startswith("precio anterior"):
            ext.nota(linea)


def _no_operativo(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    for pagina in paginas:
        lineas = [(ln, texto_de(ln)) for ln in pagina.lineas()]
        seccion = None
        modalidades: list[tuple[float, ValorPdf]] = []
        tabla: list[list[Palabra]] = []
        for linea, texto in lineas:
            precios = [p for p in linea if NUM.fullmatch(p.texto)]
            if _PIE.match(texto):
                continue
            if m := re.match(r"(\d)\. (.+)", texto):
                seccion = m.group(2)
                ext.marcar(linea, "etiqueta")
            elif _es_titulo(texto):
                _cabecera(ext, linea, texto)
            elif precios and seccion == "Almacenamiento":
                # Fila «2  1,8883 Euros/m3 y mes  …»: la primera cifra es la modalidad.
                modalidad = linea[0].texto if linea[0].texto.isdigit() else "?"
                resto = [p for p in linea if p.x0 > precios[0].x1]
                unidad = unidad_de(texto)
                v = ValorPdf(seccion, f"Modalidad {modalidad}", None, numero(precios[0].texto),
                             unidad, precios[0], texto)  # fmt: skip
                ext.marcar(linea[:1], "etiqueta")
                ext.marcar([p for p in resto if p.texto == "3"][:1], "etiqueta")  # el «3» de m3
                modalidades.append((linea[0].top, v))
                tabla.append([p for p in resto if p.texto not in ("Euros/m", "y", "mes")
                              and not (p.texto == "3" and p.x0 < resto[0].x0 + 60)])  # fmt: skip
                ext.valor(v)
            elif precios and seccion:
                ext.marcar(linea, "etiqueta")
                ext.valor(ValorPdf(None, seccion, None, numero(precios[0].texto),
                                   unidad_de(texto), precios[0], texto))  # fmt: skip
            elif seccion == "Almacenamiento" and texto != "Modalidad Precio Instalación Productos":
                tabla.append(linea)
            elif seccion and seccion != "Almacenamiento":
                ext.nota(linea)
        # Instalación y productos de cada modalidad: las líneas más cercanas a su fila.
        # Columnas «Instalación» y «Productos»: se separan por la posición de sus títulos.
        titulos = {
            p.texto: p.x0 for ln, t in lineas if t.startswith("Modalidad Precio") for p in ln
        }
        corte = (titulos.get("Instalación", 0) + titulos.get("Productos", 1e9)) / 2
        partes: dict[int, tuple[list[str], list[str]]] = {}
        for linea in tabla:
            if not linea or not modalidades:
                continue
            _, v = min(modalidades, key=lambda m: abs(m[0] - linea[0].top))
            inst, prod = partes.setdefault(id(v), ([], []))
            inst += [p.texto for p in linea if p.x0 < corte]
            prod += [p.texto for p in linea if p.x0 >= corte]
            v.condicion = f"Instalación: {' '.join(inst)} · Productos: {' '.join(prod)}"
            ext.marcar(linea, "etiqueta")


def _avgas(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    columnas: list[tuple[float, str, str]] = []
    servicio: list[str] = []
    producto = None
    for _, linea, texto in _lineas(paginas):
        if "PRECIOS" in texto:
            _cabecera(ext, linea, texto)
        elif "(€/" in texto:
            for k, p in enumerate(linea):
                if p.texto.startswith("(€/"):
                    columnas.append((p.centro, linea[k - 1].texto, unidad_de(p.texto) or p.texto))
            ext.marcar(linea, "etiqueta")
        elif columnas and any(NUM.fullmatch(p.texto) for p in linea):
            etiqueta = [p for p in linea if not NUM.fullmatch(p.texto)]
            for p in linea:
                if NUM.fullmatch(p.texto):
                    _, titulo, unidad = min(columnas, key=lambda c: abs(c[0] - p.centro))
                    ext.valor(
                        ValorPdf(producto, f"{' '.join(servicio)} — {titulo}", texto_de(etiqueta),
                                 numero(p.texto), unidad, p, texto)
                    )  # fmt: skip
        elif not columnas:
            producto = texto
            ext.marcar(linea, "etiqueta")
        elif texto.isupper():
            servicio.append(texto)


# --- bonificaciones --------------------------------------------------------------------


def _bonificaciones(paginas: tuple[Pagina, ...], ext: Extraccion) -> None:
    bloque = "bonificacion"
    columnas: list[str] = []
    cabeceras: list[list[Palabra]] = []
    condicion_compensacion = ""
    for _, linea, texto in _lineas(paginas):
        importes = [p for p in linea if NUM.fullmatch(p.texto)]
        if texto.startswith(("BONIFICACIONES", "COMPENSACIONES")):
            bloque = "bonificacion" if texto.startswith("BONIF") else "compensacion"
            _cabecera(ext, linea, texto)
        elif texto.startswith("ALMACENADAS Y/O"):
            continue
        elif bloque == "bonificacion" and not columnas and texto.startswith(("De ", "garantizada")):
            cabeceras.append(linea)
        elif texto.startswith("Por cada") and importes:
            if not columnas and len(cabeceras) == 2:
                # Dos columnas cuyo título ocupa dos líneas: se unen por posición.
                corte = (cabeceras[0][0].x0 + cabeceras[0][-1].x1) / 2
                for lado in (lambda p: p.centro < corte, lambda p: p.centro >= corte):
                    columnas.append(" ".join(p.texto for cab in cabeceras for p in cab if lado(p)))
            tramo = texto_de([p for p in linea if p.x0 < importes[0].x0])
            ext.marcar(linea, "etiqueta")
            for k, p in enumerate(importes):
                condicion = columnas[k] if k < len(columnas) else f"columna {k + 1}"
                ext.bonificaciones.append(
                    Bonificacion(bloque, tramo, condicion, numero(p.texto), "€", p, texto)
                )
                ext.usadas[p.clave] = "valor"
        elif texto.startswith("•") and importes:
            tramo = texto_de([p for p in linea if p.x0 > importes[0].x1 and p.texto != "€"])
            ext.marcar(linea, "etiqueta")
            ext.bonificaciones.append(
                Bonificacion(bloque, tramo, condicion_compensacion, numero(importes[0].texto),
                             "€", importes[0], texto)
            )  # fmt: skip
            ext.usadas[importes[0].clave] = "valor"
        else:
            # Umbrales en porcentaje dentro del texto: también son datos.
            for patron, tramo in (
                (r"equivalente al (\d+)%", "Umbral mínimo para aplicar la bonificación"),
                (r"máximo del (\d+)%", "Límite máximo bonificable"),
                (r"inferiores al (\d+)%", "Umbral por debajo del cual se compensa"),
            ):
                if m := re.search(patron, texto):
                    palabra = next(p for p in linea if p.texto.startswith(m.group(1) + "%"))
                    ext.bonificaciones.append(
                        Bonificacion(bloque, tramo, "sobre las cantidades garantizadas",
                                     float(m.group(1)), "%", palabra, texto)
                    )  # fmt: skip
                    ext.usadas[palabra.clave] = "valor"
                    if "inferiores" in patron:
                        condicion_compensacion = (
                            f"volúmenes inferiores al {m.group(1)}% de las cantidades garantizadas"
                        )
            ext.nota(linea)  # después: los umbrales ya extraídos no se duplican


# --- matrices origen × destino -----------------------------------------------------------

_ETIQUETAS_DE_FILA = {
    "INSTALACIONES", "DE", "DESTINO", "AEROPUERTOS", "AEROPUERTO", "INSTALACIÓN", "ORIGEN",
}  # fmt: skip


def _es_celda(p: Palabra) -> bool:
    return bool(NUM.fullmatch(p.texto)) or p.texto == "(*)"


def _matriz(paginas: tuple[Pagina, ...], ext: Extraccion, tipo: str) -> None:
    for pagina in paginas:
        cabecera: list[list[Palabra]] = []
        modalidades: list[list[Palabra]] = []
        filas: list[tuple[list[Palabra], list[Palabra]]] = []
        unidad = None
        for linea in pagina.lineas():
            texto = texto_de(linea)
            if _PIE.match(texto):
                continue
            i = next((k for k, p in enumerate(linea) if _es_celda(p)), None)
            es_fila = (
                i is not None and i > 0 and all(_es_celda(p) for p in linea[i:])
                and not texto.startswith("(") and any(NUM.fullmatch(p.texto) for p in linea[i:])
            )  # fmt: skip
            if es_fila:
                filas.append((linea[:i], linea[i:]))
            elif filas:
                ext.nota(linea)  # notas al pie de la matriz
            elif "PRECIOS" in texto or _es_titulo(texto):
                _cabecera(ext, linea, texto)
            elif texto.startswith("Euros/m"):
                unidad = unidad_de(texto) or "€/m3"  # el superíndice «3» puede ir aparte
                ext.marcar(linea, "etiqueta")
            elif "RECEPCIÓN" in texto or "CONECTADAS" in texto:
                # El título puede compartir línea con el rótulo de las filas («AEROPUERTO»).
                inicio = next(
                    k for k, p in enumerate(linea) if p.texto in ("RECEPCIÓN", "CONECTADAS")
                )
                modalidades.append(linea[inicio:])
                if inicio:  # lo anterior al título es cabecera de columnas
                    cabecera.append(linea[:inicio])
            elif texto != "INSTALACIÓN ORIGEN":
                cabecera.append(linea)
        if not filas:
            continue

        # Columnas: se agrupan las posiciones horizontales de todas las cifras.
        centros = sorted(p.centro for _, celdas in filas for p in celdas)
        grupos: list[list[float]] = [[centros[0]]]
        for c in centros[1:]:
            if c - grupos[-1][-1] > 12:
                grupos.append([c])
            else:
                grupos[-1].append(c)
        columnas = [sum(g) / len(g) for g in grupos]
        limites = [(a + b) / 2 for a, b in zip(columnas, columnas[1:], strict=False)]
        ancho = (limites[0] - columnas[0]) if limites else 40.0
        bordes = [columnas[0] - ancho, *limites, columnas[-1] + ancho]

        def columna_de(x: float) -> int | None:
            for k in range(len(columnas)):
                if bordes[k] <= x < bordes[k + 1]:
                    return k
            return None

        nombres: list[list[Palabra]] = [[] for _ in columnas]
        for linea in cabecera:
            for p in linea:
                k = columna_de(p.centro)
                if k is not None:
                    nombres[k].append(p)
                elif p.texto not in _ETIQUETAS_DE_FILA:
                    ext.avisos.append(
                        ("cabecera_matriz_no_asignada", f"p{pagina.numero}: {p.texto}")
                    )
        origenes, marcas = [], []
        for palabras in nombres:
            palabras.sort(key=lambda p: (round(p.top), p.x0))
            marca = [p for p in palabras if re.fullmatch(r"\(\d+\)", p.texto)]
            ext.marcar(marca, "etiqueta")
            origenes.append(" ".join(p.texto for p in palabras if p not in marca))
            marcas.append(" ".join(p.texto for p in marca) or None)

        # Modalidades: cada «RECEPCIÓN …» encabeza un grupo de columnas (una tabla del PDF).
        segmentos: list[list[Palabra]] = []
        for linea in modalidades:
            if "RECEPCIÓN" not in texto_de(linea) and segmentos:
                centro = (linea[0].x0 + linea[-1].x1) / 2
                destino = min(segmentos, key=lambda s: abs((s[0].x0 + s[-1].x1) / 2 - centro))
                destino.extend(linea)
                continue
            for p in linea:
                if p.texto == "RECEPCIÓN":
                    segmentos.append([])
                if segmentos:
                    segmentos[-1].append(p)
        modalidad_de: list[str | None] = [None] * len(columnas)
        if segmentos:
            segmentos.sort(key=lambda s: s[0].x0)
            cajas = sorted({(c[0], c[2]) for c in pagina.cajas_tablas
                            if any(c[0] <= x <= c[2] for x in columnas) and c[2] - c[0] > 60})  # fmt: skip
            bloques: list[list[int]] = []
            for x0, x1 in cajas:
                dentro = [k for k, x in enumerate(columnas) if x0 <= x <= x1]
                if (
                    dentro
                    and dentro not in bloques
                    and not any(set(dentro) <= set(b) for b in bloques)
                ):
                    bloques = [b for b in bloques if not set(b) <= set(dentro)] + [dentro]
            bloques.sort()
            if len(segmentos) == 1:
                bloques = [list(range(len(columnas)))]
            rangos = [(s[0].x0, max(p.x1 for p in s)) for s in segmentos]
            if len(bloques) != len(segmentos) or sum(map(len, bloques)) != len(columnas):
                # Sin una tabla por modalidad: vale si los títulos están uno al lado del
                # otro (no se solapan); cada columna va con el título que tiene encima.
                disjuntos = all(a[1] < b[0] for a, b in zip(rangos, rangos[1:], strict=False))
                bloques = [[] for _ in segmentos] if disjuntos else []
                for k, x in enumerate(columnas if disjuntos else []):
                    cerca = min(
                        range(len(rangos)),
                        key=lambda i: max(rangos[i][0] - x, x - rangos[i][1], 0),
                    )
                    bloques[cerca].append(k)
            if len(bloques) != len(segmentos) or not all(bloques):
                ext.avisos.append(
                    (
                        "matriz_sin_modalidad",
                        f"p{pagina.numero}: {len(segmentos)} títulos, {bloques}",
                    )
                )
            else:
                for segmento, bloque in zip(segmentos, bloques, strict=True):
                    marca = [p for p in segmento if re.fullmatch(r"\(\d+\)", p.texto)]
                    ext.marcar(marca, "etiqueta")
                    nombre = texto_de([p for p in segmento if p not in marca])
                    for k in bloque:
                        modalidad_de[k] = nombre

        for etiqueta, celdas in filas:
            llamadas = [p for p in etiqueta if re.fullmatch(r"\(\d+\)", p.texto)]
            ext.marcar(llamadas, "etiqueta")  # llamadas a nota: «Baleares (Ibiza) (3)»
            destino = texto_de([p for p in etiqueta if p not in llamadas])
            for p in celdas:
                k = columna_de(p.centro)
                if k is None or not origenes[k]:
                    ext.avisos.append(
                        ("celda_sin_columna", f"p{pagina.numero} {destino}: {p.texto}")
                    )
                    continue
                precio = numero(p.texto)
                ext.celdas.append(
                    CeldaMatriz(
                        tipo=tipo,
                        modalidad=modalidad_de[k],
                        destino=destino,
                        origen=origenes[k],
                        estado="ofrecida" if precio is not None else "no_aplica",
                        precio=precio,
                        unidad=unidad if precio is not None else None,
                        marca=marcas[k],
                        palabra=p,
                        texto_original=f"{destino} | {origenes[k]} | {p.texto}",
                    )
                )
                if precio is not None:
                    ext.usadas[p.clave] = "valor"


# --- despacho por título -------------------------------------------------------------------

# (marcas que deben aparecer en el título, nombre del parser, función)
_PARSERS: list[tuple[tuple[str, ...], str, Callable[[tuple[Pagina, ...], Extraccion], None]]] = [
    (("BONIFICACIONES",), "bonificaciones", _bonificaciones),
    (("RECONSIGNACIÓN",), "reconsignacion", lambda p, e: _matriz(p, e, "reconsignacion")),
    (("CONSIGNACIÓN DEL", "ÉSTER"), "consignacion_ester",
     lambda p, e: _matriz(p, e, "consignacion_ester")),
    (("ORIGEN DESTINO",), "origen_destino", lambda p, e: _matriz(p, e, "origen_destino")),
    (("LOGÍSTICA BÁSICA PARA QUEROSENOS",), "origen_destino",
     lambda p, e: _matriz(p, e, "origen_destino")),
    (("ALMACENAMIENTO NO OPERATIVO",), "no_operativo", _no_operativo),
    (("ALMACENAMIENTO DE ÉSTER",), "ester_almacenamiento", _ester_almacenamiento),
    (("TERMINALES CANARIOS", "LOGÍSTICOS BÁSICOS"), "terca_basica", _terca_basica),
    (("TERMINALES CANARIOS", "LOGÍSTICA PRIMARIA"), "terca_primaria", _terca_primaria),
    (("POR SERVICIOS LOGÍSTICOS", "DISA"), "disa", _disa),
    (("AVGAS",), "avgas", _avgas),
    (("SERVICIOS DE BÚNKER",), "concepto_valor", _concepto_valor),
    (("MEZCLA EN BRAZO DE CARGA",), "concepto_valor", _concepto_valor),
    (("SERVICIO LOGÍSTICO INTEGRADO",), "concepto_valor", _concepto_valor),
]  # fmt: skip


def extraer_pdf(datos: bytes) -> Extraccion:
    """Elige el parser por el contenido del PDF y devuelve lo extraído."""
    paginas = leer_paginas(datos)
    completo = " ".join(texto_de(ln) for pag in paginas for ln in pag.lineas()).upper()
    for marcas, nombre, funcion in _PARSERS:
        if all(m in completo for m in marcas):
            ext = Extraccion(parser=nombre)
            funcion(paginas, ext)
            return ext
    return Extraccion(parser="desconocido")
