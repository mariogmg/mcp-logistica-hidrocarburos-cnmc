"""De una foto de raw/ a filas listas para la base de datos.

Función pura sobre el crudo: no usa la red ni la base. Devuelve, por tabla,
las filas que describen lo que el portal publicaba ese día, más los avisos.
"""

from __future__ import annotations

import re
from collections import Counter

from .almacen import AlmacenCrudo
from .avisos import Aviso
from .categorias import INDICADORES, Categorizador, Indicadores
from .inventario import construir_inventario
from .parsers.detalle import Detalle, PaginaNoReconocida, parsear_detalle

Filas = dict[str, list[dict]]


def _plano(nombre: str) -> str:
    return re.sub(r"\s+", " ", nombre).strip().casefold()


def extraer_foto(
    almacen: AlmacenCrudo, cat: Categorizador, ind: Indicadores
) -> tuple[Filas, list[Aviso], dict]:
    inventario, avisos = construir_inventario(almacen)
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
            d, tipo_instalacion.get(det["id_tanque"]), operador, cat, ind, filas, avisos
        )
    return filas, _sin_repetir(avisos), inventario


def _filas_de_detalle(
    d: Detalle,
    tipo: str | None,
    operador: str | None,
    cat: Categorizador,
    ind: Indicadores,
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
        categoria, conocida = cat.categoria(p.etiqueta, tipo)
        if not conocida:
            avisos.append(Aviso("aviso", "sin_categoria", recurso, p.etiqueta, operador))
        indicadores: dict[str, int | None] = dict.fromkeys(INDICADORES)
        if p.seccion == "principal":
            indicadores, desconocidos = ind.de_servicio(incluidos_por_servicio.get(p.etiqueta, []))
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
            "anio_tabla": None,
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
