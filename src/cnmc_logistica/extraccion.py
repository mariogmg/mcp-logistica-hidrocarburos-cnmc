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
from .cobertura import calcular
from .inventario import construir_inventario
from .parsers.detalle import Detalle, PaginaNoReconocida, parsear_detalle
from .parsers.origen_destino import normalizar_origen, parsear_origen_destino
from .parsers.pdf import DocumentoPdf, PdfIlegible, analizar_pdf, parsear_capacidades
from .parsers.pdf_lectura import leer_paginas
from .parsers.pdf_precios import Extraccion, extraer_pdf

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
        "enlaces_documento": [],
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
            _filas_origen_destino(almacen, d, det["id_tanque"], operador, documentos, filas, avisos)
    _enlazar_pdf(filas, documentos, cfg, avisos)
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
                incluidos_por_servicio.get(p.etiqueta, []), tipo
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
        for clave_pdf in dict.fromkeys(p.pdfs):
            filas["enlaces_documento"].append(
                {k: comun[k] for k in ("id_tanque_instalacion", "seccion", "etiqueta",
                                       "subetiqueta", "ocurrencia")} | {"documento": clave_pdf}
            )  # fmt: skip
            documentos.categorias_enlace.setdefault(clave_pdf, set()).add(categoria)
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
        self.extracciones: dict[str, Extraccion] = {}
        self.categorias_enlace: dict[str, set[str]] = {}  # PDF → categorías de quien lo enlaza
        # detalle → (PDF de origen-destino, precios ofrecidos en el HTML, id_tanque)
        self.html_od: dict[int, tuple[list[str], Counter, int]] = {}
        self.filas: Filas = {
            "documentos": [], "filas_documento": [], "capacidades": [], "precios_pdf": [],
            "matrices_pdf": [], "bonificaciones": [], "notas_pdf": [], "cobertura_pdf": [],
        }  # fmt: skip

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
        usadas: dict[tuple, str] = {}
        parser = "capacidades"
        if entrada.clave == "capacidades":
            usadas = _filas_capacidades(datos, cfg, docs, avisos)
        else:
            ext = extraer_pdf(datos)
            docs.extracciones[entrada.clave] = ext
            usadas, parser = ext.usadas, ext.parser
            _filas_de_extraccion(entrada.clave, ext, doc.anio, docs, avisos)
        paginas = leer_paginas(datos)
        por_ocr = [str(pag.numero) for pag in paginas if pag.metodo == "ocr"]
        if por_ocr:
            avisos.append(
                Aviso("aviso", "valores_por_ocr", recurso,
                      f"páginas sin capa de texto leídas por OCR: {', '.join(por_ocr)}; "
                      "revisar los valores (metodo = 'ocr')")
            )  # fmt: skip
        cob = calcular(entrada.clave, paginas, usadas, cfg.descartes)
        docs.filas["cobertura_pdf"].append(
            {"documento": entrada.clave, "parser": parser, **cob.fila()}
        )
        for texto in cob.sin_cubrir:
            avisos.append(Aviso("error", "cifra_pdf_sin_cubrir", recurso, texto))
    return docs


def _coordenadas(palabra) -> dict:
    return {
        "metodo": palabra.metodo,
        "pagina": palabra.pagina,
        "x0": round(palabra.x0, 1),
        "top": round(palabra.top, 1),
        "x1": round(palabra.x1, 1),
        "bottom": round(palabra.bottom, 1),
    }


def _filas_de_extraccion(
    clave: str, ext: Extraccion, anio_doc: int | None, docs: _Documentos, avisos: list[Aviso]
) -> None:
    """Pasa lo extraído de un PDF de precios a filas de sus tablas."""
    recurso = f"pdf/{clave}"
    anio = ext.anio or anio_doc
    if ext.parser == "desconocido":
        avisos.append(
            Aviso("error", "pdf_sin_parser", recurso, "ningún parser reconoce este documento")
        )
    avisos += [Aviso("aviso", tipo, recurso, texto) for tipo, texto in ext.avisos]
    veces: Counter = Counter()
    for v in ext.valores:
        veces[v.seccion, v.concepto, v.subconcepto] += 1
        docs.filas["precios_pdf"].append(
            {
                "documento": clave,
                "seccion": v.seccion,
                "concepto": v.concepto,
                "subconcepto": v.subconcepto,
                "ocurrencia": veces[v.seccion, v.concepto, v.subconcepto],
                "categoria_servicio": None,  # se asigna al final, con los enlaces
                "valor": v.valor,
                "unidad": v.unidad,
                "anio_tabla": anio,
                "vigencia": ext.vigencia,
                "condicion": v.condicion,
                **_coordenadas(v.palabra),
                "texto_original": v.texto_original,
            }
        )
    vistas: set[tuple] = set()
    for c in ext.celdas:
        llave = (c.tipo, c.modalidad, c.destino, c.origen)
        if llave in vistas:
            avisos.append(Aviso("aviso", "celda_repetida", recurso, c.texto_original))
            continue
        vistas.add(llave)
        docs.filas["matrices_pdf"].append(
            {
                "documento": clave,
                "tipo": c.tipo,
                "modalidad": c.modalidad,
                "destino": c.destino,
                "origen": c.origen,
                "id_tanque_instalacion_destino": None,
                "id_tanque_destino": None,
                "id_tanque_origen": None,
                "estado": c.estado,
                "precio": c.precio,
                "unidad": c.unidad,
                "anio_tabla": anio,
                "marca": c.marca,
                **_coordenadas(c.palabra),
                "texto_original": c.texto_original,
            }
        )
    for b in ext.bonificaciones:
        docs.filas["bonificaciones"].append(
            {
                "documento": clave,
                "bloque": b.bloque,
                "tramo": b.tramo,
                "condicion": b.condicion,
                "valor": b.valor,
                "unidad": b.unidad,
                "anio": anio,
                **_coordenadas(b.palabra),
                "texto_original": b.texto_original,
            }
        )
    for orden, n in enumerate(ext.notas, start=1):
        docs.filas["notas_pdf"].append(
            {"documento": clave, "pagina": n.pagina, "orden": orden, "texto": n.texto,
             "metodo": n.metodo}
        )  # fmt: skip


def _enlazar_pdf(filas: Filas, docs: _Documentos, cfg: Configuracion, avisos: list[Aviso]) -> None:
    """Paso final: une lo extraído de los PDF con los detalles que remiten a ellos."""
    # 1. Categoría de cada precio de PDF: la propia del concepto o la de quien enlaza el PDF.
    for fila in filas["precios_pdf"]:
        propia = cfg.categorias.categoria_concepto_pdf(fila["concepto"])
        heredadas = docs.categorias_enlace.get(fila["documento"], set())
        if propia:
            fila["categoria_servicio"] = propia
        elif len(heredadas) == 1:
            fila["categoria_servicio"] = next(iter(heredadas))
        else:
            fila["categoria_servicio"] = "sin_categoria"
            avisos.append(
                Aviso("aviso", "sin_categoria", f"pdf/{fila['documento']}", fila["concepto"])
            )

    # 2. Origen-destino: cada fila del PDF se une al detalle cuyos precios HTML son
    #    exactamente los mismos. No se compara por nombre (difieren entre HTML y PDF).
    por_fila: dict[tuple[str, str], list[dict]] = {}
    for fila in filas["matrices_pdf"]:
        if fila["tipo"] == "origen_destino":
            por_fila.setdefault((fila["documento"], fila["destino"]), []).append(fila)
    huellas = {
        llave: Counter(f"{c['precio']:.2f}" for c in celdas if c["precio"] is not None)
        for llave, celdas in por_fila.items()
    }
    id_tanque_de: dict[str, int] = {}
    for id_ti, (pdfs, valores_html, id_tanque) in docs.html_od.items():
        candidatas = [k for k, h in huellas.items() if k[0] in pdfs and h == valores_html]
        if not candidatas:
            # El PDF puede traer algún precio que el HTML da como no ofrecido: se admite
            # si todos los precios del HTML están en la fila y sobran como mucho dos.
            candidatas = [
                k
                for k, h in huellas.items()
                if k[0] in pdfs
                and not (valores_html - h)
                and 0 < sum((h - valores_html).values()) <= 2
            ]
            if len(candidatas) == 1:
                sobran = sorted((huellas[candidatas[0]] - valores_html).elements())
                avisos.append(
                    Aviso("aviso", "pdf_con_precios_que_no_estan_en_html",
                          f"origen_destino/{id_ti}",
                          f"{candidatas[0][1]} (pdf/{candidatas[0][0]}): {', '.join(sobran)} €/m3 "
                          "figura en el PDF y la página lo da como ruta no ofrecida")
                )  # fmt: skip
        if len(candidatas) != 1:
            if any(c in docs.extracciones for c in pdfs):
                avisos.append(
                    Aviso("aviso", "origen_destino_no_cuadra_con_pdf", f"origen_destino/{id_ti}",
                          f"{len(candidatas)} filas del PDF {pdfs} con los mismos precios")
                )  # fmt: skip
            continue
        for celda in por_fila[candidatas[0]]:
            celda["id_tanque_instalacion_destino"] = id_ti
            celda["id_tanque_destino"] = id_tanque
        id_tanque_de[normalizar_origen(candidatas[0][1])] = id_tanque

    # 3. Reconsignación y consignación de éster: mismas instalaciones, con los mismos
    #    nombres que las filas de origen-destino ya identificadas. Coincidencia exacta.
    alias = {normalizar_origen(k): normalizar_origen(v) for k, v in cfg.alias_pdf.items()}

    def id_de(nombre: str) -> int | None:
        plano = normalizar_origen(nombre)
        return id_tanque_de.get(alias.get(plano, plano))

    for fila in filas["matrices_pdf"]:
        if fila["tipo"] != "origen_destino":
            fila["id_tanque_destino"] = id_de(fila["destino"])
            fila["id_tanque_origen"] = id_de(fila["origen"])

    # 4. Ninguna línea «pdf» puede quedarse sin datos estructurados detrás.
    con_datos = {
        f["documento"]
        for tabla in ("precios_pdf", "matrices_pdf", "bonificaciones")
        for f in filas[tabla]
    }
    for enlace in filas["enlaces_documento"]:
        if enlace["documento"] not in con_datos:
            avisos.append(
                Aviso("error", "pdf_sin_resolver",
                      f"detalle/{enlace['id_tanque_instalacion']}",
                      f"{enlace['etiqueta']} → pdf/{enlace['documento']} sin datos estructurados")
            )  # fmt: skip


def _filas_capacidades(
    datos: bytes, cfg: Configuracion, docs: _Documentos, avisos: list[Aviso]
) -> dict[tuple, str]:
    recurso = "pdf/capacidades"
    cap = parsear_capacidades(datos)
    for orden, (pagina, texto) in enumerate(cap.notas, start=1):
        docs.filas["notas_pdf"].append(
            {"documento": "capacidades", "pagina": pagina, "orden": orden, "texto": texto,
             "metodo": "texto"}
        )  # fmt: skip
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
    return cap.usadas


# --- origen-destino ------------------------------------------------------------------


def _filas_origen_destino(
    almacen: AlmacenCrudo,
    d: Detalle,
    id_tanque: int,
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

    # Control cruzado: el HTML es la fuente. Se guardan sus precios ofrecidos para
    # unirlos después con la fila del PDF que tenga exactamente los mismos.
    documentos.html_od[id_ti] = (
        pdfs_od,
        Counter(f"{f.precio:.2f}" for f in od.filas if f.precio is not None),
        id_tanque,
    )
