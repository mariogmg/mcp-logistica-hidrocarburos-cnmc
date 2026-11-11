"""Descubrimiento: qué instalaciones, detalles y operadores publica el portal.

Dos pasos separados a propósito:
1. `descargar_descubrimiento` baja portada, mapas, buscador y fichas a raw/.
2. `construir_inventario` es una función pura sobre raw/. Así el inventario de
   cualquier fecha pasada se puede regenerar sin red (lo usa `reconstruir`).

Fuentes y control cruzado:
- Mapas → idTanque, tipo (icono) y comunidad.
- Fichas → qué idTanqueInstalacion cuelgan de cada idTanque.
- Buscador por almacenista → operador (código estable) y producto de cada
  detalle. Debe listar exactamente los mismos detalles que las fichas.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import portal
from .almacen import AlmacenCrudo
from .avisos import Aviso
from .descarga import Descargador, ErrorDescarga
from .parsers.buscador import parsear_formulario, parsear_resultados
from .parsers.detalle import PaginaNoReconocida
from .parsers.ficha import parsear_ficha
from .parsers.mapa import comunidades_de_portada, parsear_mapa


def _bajar(desc: Descargador, recurso, avisos: list[Aviso]):
    try:
        return desc.obtener(recurso)
    except ErrorDescarga as exc:
        avisos.append(Aviso("error", "descarga", f"{recurso.tipo}/{recurso.clave}", str(exc)))
        return None


def descargar_descubrimiento(desc: Descargador) -> list[Aviso]:
    """Descarga todo lo necesario para construir el inventario."""
    avisos: list[Aviso] = []
    almacen = desc.almacen
    desc.comprobar_robots()

    entrada = _bajar(desc, portal.portada(), avisos)
    comunidades = comunidades_de_portada(almacen.leer_texto(entrada)[0]) if entrada else []
    ids_tanque: set[int] = set()
    for com in comunidades:
        if e := _bajar(desc, portal.mapa(com), avisos):
            ids_tanque |= {i.id_tanque for i in parsear_mapa(almacen.leer_texto(e)[0])}

    if e := _bajar(desc, portal.formulario_buscador(), avisos):
        formulario = parsear_formulario(almacen.leer_texto(e)[0])
        for codigo in formulario.almacenistas:
            primera = _bajar(desc, portal.buscador_almacenista(codigo), avisos)
            if primera is None:
                continue
            pag = parsear_resultados(almacen.leer_texto(primera)[0])
            for n in range(2, pag.paginas + 1):
                _bajar(desc, portal.buscador_almacenista(codigo, n, pag.parametro_pagina), avisos)

    for id_tanque in sorted(ids_tanque):
        _bajar(desc, portal.ficha(id_tanque), avisos)
    return avisos


def construir_inventario(almacen: AlmacenCrudo) -> tuple[dict, list[Aviso]]:
    """Inventario de una foto de raw/, con su control cruzado."""
    avisos: list[Aviso] = []

    def texto(entrada) -> str:
        contenido, aviso = almacen.leer_texto(entrada)
        if aviso:
            avisos.append(Aviso("aviso", "codificacion", f"{entrada.tipo}/{entrada.clave}", aviso))
        return contenido

    # --- mapas: instalaciones ---
    instalaciones: dict[int, dict] = {}
    for e in sorted(almacen.de_tipo("mapa"), key=lambda x: x.clave):
        for inst in parsear_mapa(texto(e)):
            if inst.id_tanque in instalaciones:
                otra = instalaciones[inst.id_tanque]["comunidad"]
                avisos.append(
                    Aviso("aviso", "instalacion_en_dos_mapas", f"ficha/{inst.id_tanque}",
                          f"aparece en {otra} y en {e.clave}")
                )  # fmt: skip
                continue
            if inst.tipo == "desconocido":
                avisos.append(
                    Aviso("error", "tipo_desconocido", f"ficha/{inst.id_tanque}",
                          f"icono no reconocido: {inst.icono}")
                )  # fmt: skip
            instalaciones[inst.id_tanque] = {
                "id_tanque": inst.id_tanque,
                "tipo": inst.tipo,
                "comunidad": e.clave,
                "titulo": inst.titulo,
                "detalles": [],
            }

    # --- fichas: detalles de cada instalación ---
    detalles: dict[int, dict] = {}
    for id_tanque, inst in sorted(instalaciones.items()):
        e = almacen.buscar("ficha", str(id_tanque))
        if e is None:
            avisos.append(
                Aviso("error", "ficha_no_descargada", f"ficha/{id_tanque}", inst["titulo"])
            )
            continue
        id_redirigido = portal.id_de_enlace(e.url_final, "idTanqueInstalacion")
        if id_redirigido is not None:
            # Un solo producto: la ficha redirige al detalle y esa URL final manda.
            encontrados = [(id_redirigido, None)]
        else:
            try:
                encontrados = parsear_ficha(texto(e), id_tanque).detalles
            except PaginaNoReconocida as exc:
                avisos.append(Aviso("error", "bloque_desconocido", f"ficha/{id_tanque}", str(exc)))
                continue
        if not encontrados:
            avisos.append(
                Aviso("aviso", "instalacion_sin_detalles", f"ficha/{id_tanque}", inst["titulo"])
            )
        for id_ti, producto in encontrados:
            inst["detalles"].append(id_ti)
            detalles[id_ti] = {
                "id_tanque_instalacion": id_ti,
                "id_tanque": id_tanque,
                "operador": None,  # código bajo el que lo lista el buscador
                "producto": producto,
                "producto_buscador": None,
            }

    # --- buscador: operadores y control cruzado ---
    operadores: dict[str, dict] = {}
    nombres_comunidad: dict[str, str] = {}
    if e := almacen.buscar("buscador", "formulario"):
        formulario = parsear_formulario(texto(e))
        nombres_comunidad = formulario.comunidades
        operadores = {
            codigo: {"codigo": codigo, "nombre": nombre, "detalles": 0}
            for codigo, nombre in formulario.almacenistas.items()
        }
    else:
        avisos.append(Aviso("error", "buscador_no_descargado", "buscador/formulario", ""))

    comunidad_por_nombre = {n.casefold(): c for c, n in nombres_comunidad.items()}
    en_buscador: dict[int, str] = {}
    comunidad_distinta: list[dict] = []
    filas_por_operador: dict[str, int] = {}
    anunciadas: dict[str, int] = {}
    for e in sorted(almacen.de_tipo("buscador"), key=lambda x: x.clave):
        consulta = parse_qs(urlsplit(e.url).query)
        codigo = consulta.get("almacenista", ["0"])[0]
        if codigo == "0":
            continue  # búsquedas por comunidad u otras: no asignan operador
        pag = parsear_resultados(texto(e))
        if pag.total is not None:
            anunciadas[codigo] = pag.total
        filas_por_operador[codigo] = filas_por_operador.get(codigo, 0) + len(pag.filas)
        for fila in pag.filas:
            id_ti = fila.id_tanque_instalacion
            if id_ti in en_buscador and en_buscador[id_ti] != codigo:
                avisos.append(
                    Aviso("error", "detalle_con_dos_operadores", f"detalle/{id_ti}",
                          f"{en_buscador[id_ti]} y {codigo}")
                )  # fmt: skip
                continue
            en_buscador[id_ti] = codigo
            if id_ti in detalles:
                det = detalles[id_ti]
                det["operador"] = codigo
                det["producto_buscador"] = fila.producto
                det["producto"] = det["producto"] or fila.producto
                esperada = instalaciones[det["id_tanque"]]["comunidad"]
                vista = comunidad_por_nombre.get(fila.comunidad.casefold())
                if vista != esperada:
                    comunidad_distinta.append(
                        {"detalle": id_ti, "mapa": esperada, "buscador": fila.comunidad}
                    )
    for codigo, total in anunciadas.items():
        if filas_por_operador.get(codigo, 0) != total:
            avisos.append(
                Aviso("error", "buscador_incompleto", f"buscador/{codigo}",
                      f"anuncia {total} filas y se han leído {filas_por_operador.get(codigo, 0)}",
                      operador=codigo)
            )  # fmt: skip
    for codigo in en_buscador.values():
        operadores.setdefault(codigo, {"codigo": codigo, "nombre": codigo, "detalles": 0})
        operadores[codigo]["detalles"] += 1

    solo_fichas = sorted(set(detalles) - set(en_buscador))
    solo_buscador = sorted(set(en_buscador) - set(detalles))
    for id_ti in solo_fichas:
        avisos.append(Aviso("error", "control_cruzado", f"detalle/{id_ti}",
                            "está en las fichas pero no en el buscador"))  # fmt: skip
    for id_ti in solo_buscador:
        avisos.append(Aviso("error", "control_cruzado", f"detalle/{id_ti}",
                            "está en el buscador pero no en ninguna ficha",
                            operador=en_buscador[id_ti]))  # fmt: skip
    for d in comunidad_distinta:
        avisos.append(Aviso("error", "control_cruzado", f"detalle/{d['detalle']}",
                            f"comunidad: mapa {d['mapa']}, buscador {d['buscador']}"))  # fmt: skip

    inventario = {
        "fecha": almacen.fecha,
        "comunidades": dict(sorted(nombres_comunidad.items())),
        "operadores": sorted(operadores.values(), key=lambda o: o["codigo"]),
        "instalaciones": [instalaciones[k] for k in sorted(instalaciones)],
        "detalles": [detalles[k] for k in sorted(detalles)],
        "control_cruzado": {
            "detalles_en_fichas": len(detalles),
            "detalles_en_buscador": len(en_buscador),
            "solo_fichas": solo_fichas,
            "solo_buscador": solo_buscador,
            "comunidad_distinta": comunidad_distinta,
        },
    }
    return inventario, avisos


def guardar_inventario(inventario: dict, ruta: Path) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(inventario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cargar_inventario(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))
