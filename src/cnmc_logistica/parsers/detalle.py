"""Parser de detalle.htm (un producto dentro de una instalación).

Máquina de estados sobre las líneas de la página, guiada por las etiquetas de
texto. Lo que no encaja en ningún estado se devuelve como aviso
«bloque_desconocido» con su texto; nunca se lanza una excepción por contenido
inesperado dentro de un bloque.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..avisos import Aviso
from ..portal import id_de_enlace
from .lineas import Linea, lineas_de
from .valores import Valor, extraer_porcentajes, extraer_precios

# Subtítulo → conexión. Coincidencia exacta de la primera frase; lo que no
# esté aquí queda como «sin_dato» con aviso.
CONEXIONES = {
    "instalación conectada a la red de oleoductos": "red",
    "instalación conectada por oleoducto": "por_oleoducto",
    "instalación no conectada a la red de oleoductos": "no_conectada",
}


class PaginaNoReconocida(Exception):
    """La página no tiene la estructura mínima de un detalle."""


@dataclass
class Precio:
    seccion: str  # "principal" | "otros_servicios"
    etiqueta: str
    subetiqueta: str | None
    tipo_precio: str  # "numerico" | "regla" | "texto" | "pdf"
    valores: list[Valor]
    texto: str | None  # regla, observación o texto libre (lo que no es número)
    pdfs: list[str]  # claves de documento (directorioHijo)
    texto_original: str


@dataclass
class Condicion:
    etiqueta: str
    subetiqueta: str | None
    tipo_valor: str | None  # "unico" | "max" | "min"; None si no hay número
    valor: float | None
    unidad: str | None  # "%" cuando hay valor
    texto: str | None  # condición o redacción no numérica
    texto_original: str


@dataclass
class ServicioIncluido:
    servicio: str  # etiqueta del servicio principal al que pertenece
    texto: str


@dataclass
class EnlacePdf:
    clave: str
    texto: str
    contexto: str  # etiqueta del servicio desde el que se enlaza


@dataclass
class Detalle:
    id_tanque_instalacion: int
    titulo: str
    subtitulo: str | None
    conexion: str
    titular: str
    contacto: list[str]
    productos_texto: str
    productos: list[str]
    tiene_origen_destino: bool
    precios: list[Precio] = field(default_factory=list)
    condiciones: list[Condicion] = field(default_factory=list)
    incluidos: list[ServicioIncluido] = field(default_factory=list)
    pdfs: list[EnlacePdf] = field(default_factory=list)
    avisos: list[Aviso] = field(default_factory=list)


def conexion_de(subtitulo: str | None) -> tuple[str, bool]:
    """(conexión, reconocida)."""
    if not subtitulo:
        return "sin_dato", True
    primera = subtitulo.split(".")[0].strip().casefold()
    if primera in CONEXIONES:
        return CONEXIONES[primera], True
    return "sin_dato", False


def partir_productos(cadena: str) -> list[str]:
    """«Gasolinas, Gasóleos y Biodiésel» → ['Gasolinas', 'Gasóleos', 'Biodiésel'].

    Solo separa por comas y por la conjunción « y »; no renombra ni agrupa.
    """
    partes = re.split(r"\s*,\s*|\s+y\s+", cadena)
    return [p.strip() for p in partes if p.strip()]


def partir_etiqueta(texto: str) -> tuple[str, str]:
    """«Mermas: 0,06 %» → ('Mermas', '0,06 %'); «Mermas:» → ('Mermas', '')."""
    etiqueta, sep, resto = texto.partition(":")
    if not sep:
        return texto.strip(), ""
    return etiqueta.strip(), resto.strip().rstrip(":").strip()


def cabecera_y_cuerpo(lineas: list[Linea]) -> tuple[list[Linea], list[Linea]]:
    """Separa las líneas de cabecera (título, titular…) de las del contenido.

    El contenido empieza en «PRODUCTOS:» y acaba en el pie («©»); la cabecera
    es lo que hay entre la barra «… Buscar:» y «PRODUCTOS:».
    """
    try:
        i_prod = next(i for i, ln in enumerate(lineas) if ln.texto == "PRODUCTOS:")
    except StopIteration:
        raise PaginaNoReconocida("no aparece la etiqueta «PRODUCTOS:»") from None
    i_barra = max((i for i in range(i_prod) if lineas[i].texto.endswith("Buscar:")), default=None)
    if i_barra is None:
        raise PaginaNoReconocida("no aparece la barra de título antes de «PRODUCTOS:»")
    i_fin = next(
        (i for i in range(i_prod, len(lineas)) if lineas[i].texto.startswith("©")), len(lineas)
    )
    # Los botones (volver, imprimir) son enlaces sin texto: no son cabecera.
    cabecera = [ln for ln in lineas[i_barra + 1 : i_prod] if ln.texto]
    return cabecera, lineas[i_prod + 1 : i_fin]


def _claves_pdf(linea: Linea) -> list[tuple[str, str]]:
    """[(clave, texto del enlace)] de los enlaces a PDF de una línea."""
    salida = []
    for texto, href in linea.enlaces:
        if "downloadfile" in href:
            hijo = id_de_enlace(href, "directorioHijo")
            salida.append((str(hijo) if hijo is not None else href, texto))
    return salida


def _clasificar(valores: list[Valor], pdfs: list[str], texto: str) -> str:
    if valores:
        return "numerico"
    if pdfs:
        return "pdf"
    # «Precio según contrato», «Precios vigentes hasta…»: el portal usa la
    # propia palabra «Precio» para marcar un precio que es una regla.
    if texto.casefold().startswith("precio"):
        return "regla"
    return "texto"


def parsear_detalle(html: str, id_ti: int) -> Detalle:
    recurso = f"detalle/{id_ti}"
    cabecera, cuerpo = cabecera_y_cuerpo(lineas_de(html))
    if not cabecera:
        raise PaginaNoReconocida("cabecera vacía")

    titulo = cabecera[0].texto
    resto = cabecera[1:]
    subtitulo = None
    if resto and resto[0].texto.casefold().startswith("instalación"):
        subtitulo, resto = resto[0].texto, resto[1:]
    if not resto:
        raise PaginaNoReconocida("no aparece el titular")
    titular = resto[0].texto
    conexion, reconocida = conexion_de(subtitulo)

    d = Detalle(
        id_tanque_instalacion=id_ti,
        titulo=titulo,
        subtitulo=subtitulo,
        conexion=conexion,
        titular=titular,
        contacto=[ln.texto for ln in resto[1:] if ln.texto],
        productos_texto="",
        productos=[],
        tiene_origen_destino=False,
    )

    def avisar(tipo: str, texto: str, nivel: str = "aviso") -> None:
        d.avisos.append(Aviso(nivel, tipo, recurso, texto, operador=titular))

    if not reconocida:
        avisar("conexion_desconocida", subtitulo or "")

    estado = "productos"
    servicio: str | None = None  # servicio principal abierto
    cab_servicio: list[Linea] = []  # líneas de su cabecera (precio, PDFs, notas)
    cab_abierta = False  # la cabecera del servicio aún no se ha convertido en precio
    padre: str | None = None  # último <li> no sangrado
    padre_vacio: Linea | None = None  # padre sin valor a la espera de hijos

    def cerrar_cabecera_servicio() -> None:
        nonlocal cab_servicio, cab_abierta
        if servicio is None or not cab_abierta:
            return
        cab_abierta = False
        texto = " ".join(ln.texto for ln in cab_servicio if not ln.enlaces)
        valores, resto_txt, desconocidas = extraer_precios(texto)
        claves = []
        for ln in cab_servicio:
            for clave, texto_enlace in _claves_pdf(ln):
                claves.append(clave)
                d.pdfs.append(EnlacePdf(clave, texto_enlace, servicio))
        for u in desconocidas:
            avisar("unidad_desconocida", f"{servicio}: {u}")
        original = " | ".join([f"» {servicio}", *(ln.texto for ln in cab_servicio)])
        d.precios.append(
            Precio(
                seccion="principal",
                etiqueta=servicio,
                subetiqueta=None,
                tipo_precio=_clasificar(valores, claves, resto_txt),
                valores=valores,
                texto=resto_txt or None,
                pdfs=claves,
                texto_original=original,
            )
        )
        cab_servicio = []

    def cerrar_padre() -> None:
        """Un padre («Mermas:») que se queda sin hijos se guarda igualmente."""
        nonlocal padre_vacio
        if padre_vacio is not None:
            avisar("padre_sin_hijos", padre_vacio.texto)
            _emitir_li(padre_vacio, padre or "", None, "")
            padre_vacio = None

    def _emitir_li(ln: Linea, etiqueta: str, subetiqueta: str | None, resto_txt: str) -> None:
        if estado == "otros":
            texto_sin_enlaces = resto_txt
            pdfs = _claves_pdf(ln)
            for _, texto_enlace in pdfs:
                texto_sin_enlaces = texto_sin_enlaces.replace(texto_enlace, "").strip()
            valores, libre, desconocidas = extraer_precios(texto_sin_enlaces)
            if desconocidas:
                avisar("unidad_desconocida", ln.texto)
            claves = [c for c, _ in pdfs]
            for clave, texto_enlace in pdfs:
                d.pdfs.append(EnlacePdf(clave, texto_enlace, etiqueta))
            texto_final = libre or (resto_txt if pdfs and not valores else "")
            d.precios.append(
                Precio(
                    seccion="otros_servicios",
                    etiqueta=etiqueta,
                    subetiqueta=subetiqueta,
                    tipo_precio=_clasificar(valores, claves, libre),
                    valores=valores,
                    texto=texto_final or None,
                    pdfs=claves,
                    texto_original=ln.texto,
                )
            )
        else:  # condiciones
            for tipo_valor, valor, texto in extraer_porcentajes(resto_txt):
                d.condiciones.append(
                    Condicion(
                        etiqueta=etiqueta,
                        subetiqueta=subetiqueta,
                        tipo_valor=tipo_valor,
                        valor=valor,
                        unidad="%" if valor is not None else None,
                        texto=texto,
                        texto_original=ln.texto,
                    )
                )

    for ln in cuerpo:
        t = ln.texto
        # --- cambios de bloque, siempre por etiqueta de texto ---
        if not ln.en_li and t in ("PRECIOS Y CONDICIONES:", "OTROS SERVICIOS", "CONDICIONES"):
            cerrar_cabecera_servicio()
            cerrar_padre()
            servicio, padre = None, None
            estado = {"PRECIOS Y CONDICIONES:": "precios", "OTROS SERVICIOS": "otros"}.get(
                t, "condiciones"
            )
            continue
        if not ln.en_li and t.startswith("»") and estado in ("precios", "servicio", "incluye"):
            cerrar_cabecera_servicio()
            servicio = t.lstrip("» ").strip()
            if not servicio:
                avisar("servicio_sin_etiqueta", t)
                servicio = "(sin etiqueta)"
            estado = "servicio"
            cab_abierta = True
            continue

        if estado == "productos":
            od = [h for _, h in ln.enlaces if "precio-producto" in h]
            if od:
                d.tiene_origen_destino = True
            elif not d.productos_texto and t:
                d.productos_texto = t
                d.productos = partir_productos(t)
            elif t:
                avisar("bloque_desconocido", f"[productos] {t}")
        elif estado == "servicio":
            if t == "Incluye:":
                cerrar_cabecera_servicio()
                estado = "incluye"
            elif ln.en_li:
                avisar("bloque_desconocido", f"[{servicio}] {t}")
            else:
                cab_servicio.append(ln)
        elif estado == "incluye":
            if ln.en_li and servicio:
                d.incluidos.append(ServicioIncluido(servicio, t))
            elif t:
                avisar("bloque_desconocido", f"[{servicio} / Incluye] {t}")
        elif estado in ("otros", "condiciones"):
            if not ln.en_li:
                if t:
                    avisar("bloque_desconocido", f"[{estado}] {t}")
                continue
            etiqueta, resto_txt = partir_etiqueta(t)
            if ln.sangrada:
                if padre is None:
                    avisar("hijo_sin_padre", t)
                    _emitir_li(ln, etiqueta, None, resto_txt)
                else:
                    padre_vacio = None
                    _emitir_li(ln, padre, etiqueta, resto_txt)
            else:
                cerrar_padre()
                padre = etiqueta
                if t.endswith(":") and not resto_txt and not ln.enlaces:
                    padre_vacio = ln  # «Mermas:» → se espera a los hijos
                else:
                    _emitir_li(ln, etiqueta, None, resto_txt)
        elif t:
            avisar("bloque_desconocido", f"[{estado}] {t}")

    cerrar_cabecera_servicio()
    cerrar_padre()
    if not d.productos_texto:
        avisar("sin_productos", "el bloque «PRODUCTOS:» está vacío")
    return d
