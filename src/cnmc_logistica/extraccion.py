"""De una foto de raw/ a filas listas para la base de datos.

Función pura sobre el crudo: no usa la red ni la base. Devuelve, por tabla,
las filas que describen lo que el portal publicaba ese día, más los avisos.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from .almacen import AlmacenCrudo
from .avisos import Aviso
from .categorias import INDICADORES, Configuracion
from .inventario import construir_inventario
from .parsers.detalle import Detalle, PaginaNoReconocida, parsear_detalle
from .parsers.origen_destino import parsear_origen_destino
from .parsers.pdf import (
    DocumentoPdf,
    PdfIlegible,
    analizar_pdf,
    numeros_por_linea,
    parsear_capacidades,
)

Filas = dict[str, list[dict]]


def _plano(nombre: str) -> str:
    return re.sub(r"\s+", " ", nombre).strip().casefold()


def extraer_foto(almacen: AlmacenCrudo, cfg: Configuracion) -> tuple[Filas, list[Aviso], dict]:
    inventario, avisos = construir_inventario(almacen)
    documentos = _extraer_documentos(almacen, cfg, avisos)
    filas: Filas = {
        "operadores": [{"codigo": o["codigo"]} for o in inventario["operadores"]],
        "operadores_nombres": [
            {"codigo": o["codigo"], "nombre": o["nombre"]} for o in inventario["operadores"]
        ],
        "instalaciones": [
            {
                "id_tanque": i["id_tanque"],
                "tipo": i["tipo"],
                "comunidad": i["comunidad"],
                "comunidad_nombre": inventario["comunidades"].get(i["comunidad"]),
                "titulo": i["titulo"],
            }
            for i in inventario["instalaciones"]
        ],
        "detalles": [],
        "detalle_productos": [],
        "precios": [],
        "condiciones": [],
        "servicios_incluidos": [],
        "precios_origen_destino": [],
        "notas_origen_destino": [],
        **documentos.filas,
    }
    codigo_por_nombre = {_plano(o["nombre"]): o["codigo"] for o in inventario["operadores"]}
    tipo_instalacion = {i["id_tanque"]: i["tipo"] for i in inventario["instalaciones"]}

    for det in inventario["detalles"]:
        id_ti = det["id_tanque_instalacion"]
        recurso = f"detalle/{id_ti}"
        entrada = almacen.buscar("detalle", str(id_ti))
        if entrada is None:
            avisos.append(
                Aviso("error", "detalle_no_descargado", recurso, "", operador=det["operador"])
            )
            continue
        texto, aviso_cod = almacen.leer_texto(entrada)
        if aviso_cod:
            avisos.append(Aviso("aviso", "codificacion", recurso, aviso_cod))
        try:
            d = parsear_detalle(texto, id_ti)
        except PaginaNoReconocida as exc:
            avisos.append(
                Aviso("error", "bloque_desconocido", recurso, str(exc), operador=det["operador"])
            )
            continue

        operador = codigo_por_nombre.get(_plano(d.titular))
        if operador is None:
            avisos.append(Aviso("error", "titular_sin_operador", recurso, d.titular))
        # Los avisos del parser llevan el nombre del titular: se pasan a código.
        avisos += [
            Aviso(a.nivel, a.tipo, a.recurso, a.texto, operador or det["operador"])
            for a in d.avisos
        ]
        filas["detalles"].append(
            {
                "id_tanque_instalacion": id_ti,
                "id_tanque": det["id_tanque"],
                "operador": operador,
                "operador_buscador": det["operador"],
                "discrepancia_operador": int(operador != det["operador"]),
                "titular": d.titular,
                "titulo": d.titulo,
                "subtitulo": d.subtitulo,
                "conexion": d.conexion,
                "productos_texto": d.productos_texto,
                "producto_buscador": det.get("producto_buscador"),
                "contacto": " | ".join(d.contacto) or None,
                "tiene_origen_destino": int(d.tiene_origen_destino),
            }
        )
        _filas_de_detalle(
            d, tipo_instalacion.get(det["id_tanque"]), operador, cfg, documentos, filas, avisos
        )
        if d.tiene_origen_destino:
            _filas_origen_destino(almacen, d, operador, documentos, filas, avisos)
    return filas, _sin_repetir(avisos), inventario


def _filas_de_detalle(
    d: Detalle,
    tipo: str | None,
    operador: str | None,
    cfg: Configuracion,
    documentos: _Documentos,
    filas: Filas,
    avisos: list[Aviso],
) -> None:
    id_ti = d.id_tanque_instalacion
    recurso = f"detalle/{id_ti}"
    for producto in dict.fromkeys(d.productos):
        filas["detalle_productos"].append({"id_tanque_instalacion": id_ti, "producto": producto})

    veces: Counter = Counter()
    incluidos_por_servicio: dict[str, list[str]] = {}
    for s in d.incluidos:
        incluidos_por_servicio.setdefault(s.servicio, []).append(s.texto)
        veces["inc", s.servicio, s.texto] += 1
        filas["servicios_incluidos"].append(
            {
                "id_tanque_instalacion": id_ti,
                "servicio": s.servicio,
                "texto": s.texto,
                "ocurrencia": veces["inc", s.servicio, s.texto],
            }
        )

    for p in d.precios:
        categoria, conocida = cfg.categorias.categoria(p.etiqueta, tipo)
        if not conocida:
            avisos.append(Aviso("aviso", "sin_categoria", recurso, p.etiqueta, operador))
        indicadores: dict[str, int | None] = dict.fromkeys(INDICADORES)
        if p.seccion == "principal":
            indicadores, desconocidos = cfg.indicadores.de_servicio(
                incluidos_por_servicio.get(p.etiqueta, [])
            )
            for texto in desconocidos:
                avisos.append(Aviso("aviso", "incluye_sin_clasificar", recurso, texto, operador))
        veces["pre", p.seccion, p.etiqueta, p.subetiqueta] += 1
        comun = {
            "id_tanque_instalacion": id_ti,
            "seccion": p.seccion,
            "etiqueta": p.etiqueta,
            "subetiqueta": p.subetiqueta,
            "ocurrencia": veces["pre", p.seccion, p.etiqueta, p.subetiqueta],
            "categoria_servicio": categoria,
            **indicadores,
            "tipo_precio": p.tipo_precio,
            # Un precio que remite a un PDF no tiene fecha: solo el año de la tabla.
            "anio_tabla": documentos.anio_comun(p.pdfs) if p.tipo_precio == "pdf" else None,
            "texto": p.texto,
            "documentos": ",".join(p.pdfs) or None,
            "texto_original": p.texto_original,
        }
        if not p.valores:
            filas["precios"].append(
                {**comun, "tipo_valor": None, "valor": None, "unidad": None,
                 "fecha_precio": None, "observacion": None}
            )  # fmt: skip
        for v in p.valores:
            filas["precios"].append(
                {**comun, "tipo_valor": v.tipo_valor, "valor": v.valor, "unidad": v.unidad,
                 "fecha_precio": v.fecha, "observacion": v.observacion}
            )  # fmt: skip

    for c in d.condiciones:
        # Máximo y mínimo salen de la misma línea: comparten ocurrencia.
        if c.tipo_valor != "min":
            veces["con", c.etiqueta, c.subetiqueta] += 1
        filas["condiciones"].append(
            {
                "id_tanque_instalacion": id_ti,
                "etiqueta": c.etiqueta,
                "subetiqueta": c.subetiqueta,
                "ocurrencia": max(1, veces["con", c.etiqueta, c.subetiqueta]),
                "tipo_valor": c.tipo_valor,
                "valor": c.valor,
                "unidad": c.unidad,
                "texto": c.texto,
                "texto_original": c.texto_original,
            }
        )


def _sin_repetir(avisos: list[Aviso]) -> list[Aviso]:
    return list(dict.fromkeys(avisos))


# --- documentos (PDF) --------------------------------------------------------------


class _Documentos:
    def __init__(self) -> None:
        self.analizados: dict[str, DocumentoPdf] = {}
        self.filas: Filas = {"documentos": [], "filas_documento": [], "capacidades": []}

    def anio_comun(self, claves: list[str]) -> int | None:
        anios = {self.analizados[c].anio for c in claves if c in self.analizados}
        return anios.pop() if len(anios) == 1 else None


def _extraer_documentos(
    almacen: AlmacenCrudo, cfg: Configuracion, avisos: list[Aviso]
) -> _Documentos:
    """Analiza todos los PDF de la foto. Un PDF ilegible se anota y se sigue."""
    docs = _Documentos()
    for entrada in sorted(almacen.de_tipo("pdf"), key=lambda e: e.clave):
        recurso = f"pdf/{entrada.clave}"
        datos = almacen.leer_bytes(entrada)
        try:
            doc = analizar_pdf(datos, entrada.nombre_archivo)
        except PdfIlegible as exc:
            avisos.append(Aviso("error", "pdf_ilegible", recurso, str(exc)))
            continue
        docs.analizados[entrada.clave] = doc
        if doc.paginas_sin_texto:
            avisos.append(
                Aviso("aviso", "pdf_sin_texto", recurso,
                      f"{doc.paginas_sin_texto} de {doc.paginas} páginas sin capa de texto ni OCR")
            )  # fmt: skip
        docs.filas["documentos"].append(
            {
                "clave": entrada.clave,
                "nombre_archivo": entrada.nombre_archivo,
                "sha256": entrada.sha256,
                "bytes": entrada.bytes,
                "paginas": doc.paginas,
                "paginas_sin_texto": doc.paginas_sin_texto,
                "tablas": len(doc.tablas),
                "anio": doc.anio,
                "titulo": doc.titulo,
            }
        )
        for pagina, tabla, filas_tabla in doc.tablas:
            for n, celdas in enumerate(filas_tabla, start=1):
                docs.filas["filas_documento"].append(
                    {
                        "documento": entrada.clave,
                        "pagina": pagina,
                        "tabla": tabla,
                        "fila": n,
                        "celdas": json.dumps(celdas, ensure_ascii=False),
                    }
                )
        if entrada.clave == "capacidades":
            _filas_capacidades(datos, cfg, docs, avisos)
    return docs


def _filas_capacidades(
    datos: bytes, cfg: Configuracion, docs: _Documentos, avisos: list[Aviso]
) -> None:
    recurso = "pdf/capacidades"
    cap = parsear_capacidades(datos)
    for texto in cap.sin_interpretar:
        avisos.append(Aviso("aviso", "capacidades_sin_interpretar", recurso, texto))
    for texto in cap.descuadres:
        avisos.append(Aviso("aviso", "capacidades_descuadre", recurso, texto))
    if not cap.filas:
        avisos.append(Aviso("error", "bloque_desconocido", recurso, "no se ha leído ninguna fila"))
    veces: Counter = Counter()
    for f in cap.filas:
        id_tanque, revisada = cfg.equivalencias.id_tanque(f.ambito, f.compania, f.instalacion)
        if not revisada and f.producto == "TOTAL":
            avisos.append(
                Aviso("aviso", "capacidad_sin_equivalencia", recurso,
                      f"{f.ambito} | {f.compania} | {f.instalacion}", operador=f.compania)
            )  # fmt: skip
        veces[f.ambito, f.compania, f.instalacion, f.producto] += 1
        if veces[f.ambito, f.compania, f.instalacion, f.producto] > 1:
            avisos.append(
                Aviso("aviso", "capacidad_repetida", recurso,
                      f"{f.compania} | {f.instalacion} | {f.producto}")
            )  # fmt: skip
            continue
        docs.filas["capacidades"].append(
            {
                "ambito": f.ambito,
                "compania": f.compania,
                "instalacion": f.instalacion,
                "producto": f.producto,
                "id_tanque": id_tanque,
                "capacidad_miles_m3": f.capacidad_miles_m3,
                "anio": cap.anio,
                "documento": "capacidades",
            }
        )


# --- origen-destino ------------------------------------------------------------------


def _filas_origen_destino(
    almacen: AlmacenCrudo,
    d: Detalle,
    operador: str | None,
    documentos: _Documentos,
    filas: Filas,
    avisos: list[Aviso],
) -> None:
    id_ti = d.id_tanque_instalacion
    recurso = f"origen_destino/{id_ti}"
    entrada = almacen.buscar("origen_destino", str(id_ti))
    if entrada is None:
        avisos.append(Aviso("error", "origen_destino_no_descargado", recurso, "", operador))
        return
    od = parsear_origen_destino(almacen.leer_texto(entrada)[0], id_ti)
    avisos += [Aviso(a.nivel, a.tipo, a.recurso, a.texto, operador) for a in od.avisos]

    # El HTML no dice el año: sale del PDF de origen-destino enlazado desde el detalle.
    pdfs_od = [e.clave for e in d.pdfs if "origen-destino" in e.texto.casefold()]
    anio = documentos.anio_comun(pdfs_od)
    if anio is None and od.filas:
        avisos.append(Aviso("aviso", "origen_destino_sin_anio", recurso, str(pdfs_od), operador))

    for f in od.filas:
        filas["precios_origen_destino"].append(
            {
                "id_tanque_instalacion": id_ti,
                "destino": od.destino,
                "modalidad": f.modalidad,
                "origen": f.origen,
                "origen_normalizado": f.origen_normalizado,
                "estado": f.estado,
                "precio": f.precio,
                "unidad": od.unidad if f.precio is not None else None,
                "anio_tabla": anio,
                "texto_original": f.texto_original,
            }
        )
    for n, nota in enumerate(od.notas, start=1):
        filas["notas_origen_destino"].append(
            {"id_tanque_instalacion": id_ti, "orden": n, "destino": od.destino, "texto": nota}
        )

    # Control cruzado: el HTML es la fuente; el PDF solo confirma. Los precios
    # ofrecidos de cada modalidad deben aparecer juntos en una línea del PDF.
    lineas = [
        Counter(nums) for c in pdfs_od if c in documentos.analizados
        for nums in numeros_por_linea(documentos.analizados[c])
    ]  # fmt: skip
    if not lineas:
        return
    por_modalidad: dict[str, Counter] = {}
    for f in od.filas:
        if f.precio is not None:
            valor = f"{f.precio:.2f}".replace(".", ",")
            por_modalidad.setdefault(f.modalidad, Counter())[valor] += 1
    for modalidad, valores in por_modalidad.items():
        if not any(not (valores - linea) for linea in lineas):
            avisos.append(
                Aviso("aviso", "origen_destino_no_cuadra_con_pdf", recurso,
                      f"{od.destino} | {modalidad}: {sorted(valores)}", operador)
            )  # fmt: skip
