"""Carga de fotos de raw/ en la base de datos y reconstrucción completa."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .almacen import AlmacenCrudo
from .avisos import Aviso
from .bd import TABLAS, aplicar_scd2, conectar, crear_esquema
from .categorias import Categorizador, Indicadores
from .config import Rutas
from .extraccion import extraer_foto


class FotoYaCargada(Exception):
    """La foto ya está en la base con otro contenido."""


class FotoFueraDeOrden(Exception):
    """Se intenta cargar una foto anterior a la última cargada."""


@dataclass
class Resumen:
    fecha: str
    ejecucion_id: int
    altas: int = 0
    cambios: int = 0
    desapariciones: int = 0
    por_tabla: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    avisos: list[Aviso] = field(default_factory=list)
    ejemplos: list[str] = field(default_factory=list)

    @property
    def errores(self) -> int:
        return sum(a.es_error for a in self.avisos)


def leer_config(rutas: Rutas) -> tuple[Categorizador, Indicadores]:
    return (
        Categorizador.de_fichero(rutas.config / "categorias_servicio.yaml"),
        Indicadores.de_fichero(rutas.config / "indicadores_incluye.yaml"),
    )


def cargar_foto(
    conn: sqlite3.Connection,
    almacen: AlmacenCrudo,
    cat: Categorizador,
    ind: Indicadores,
    avisos_previos: list[Aviso] | None = None,
) -> Resumen:
    """Carga una foto en una sola transacción y deja constancia en `ejecuciones`.

    Las fotos se cargan en orden cronológico. Repetir la carga de la misma
    foto es inocuo si el resultado es idéntico; si daría filas distintas (ha
    cambiado un parser o un fichero de config) se rechaza: el historial no se
    corrige, se reconstruye una base nueva desde raw/.
    """
    fecha = almacen.fecha
    crear_esquema(conn)
    ultima = conn.execute("SELECT MAX(fecha) FROM ejecuciones").fetchone()[0]
    if ultima and fecha < ultima:
        raise FotoFueraDeOrden(f"la base ya contiene la foto {ultima}, posterior a {fecha}")
    ya_cargada = ultima == fecha
    previa = conn.execute(
        "SELECT MAX(fecha) FROM ejecuciones WHERE fecha < ?", (fecha,)
    ).fetchone()[0]

    filas, avisos, _ = extraer_foto(almacen, cat, ind)
    avisos = list(avisos_previos or []) + avisos
    resumen = Resumen(fecha=fecha, ejecucion_id=0)
    try:
        insertadas = 0
        for tabla in TABLAS:
            if tabla not in filas:
                continue
            c = aplicar_scd2(conn, tabla, filas[tabla], fecha, previa)
            insertadas += c.insertadas
            resumen.altas += c.altas
            resumen.cambios += c.cambios
            resumen.desapariciones += c.desapariciones
            resumen.por_tabla[tabla] = (c.altas, c.cambios, c.desapariciones)
            resumen.ejemplos += c.ejemplos
        if ya_cargada and insertadas:
            raise FotoYaCargada(
                f"la foto {fecha} ya está cargada y ahora daría {insertadas} filas distintas "
                "(¿cambió un parser o un fichero de config?). Usa «reconstruir»."
            )
        cur = conn.execute(
            """INSERT INTO ejecuciones (fecha, registrada_en, version, recursos, altas, cambios,
                   desapariciones, errores, avisos, por_tabla)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fecha,
                datetime.now(UTC).isoformat(timespec="seconds"),
                __version__,
                len({(e.tipo, e.clave) for e in almacen.entradas() if e.ok}),
                resumen.altas,
                resumen.cambios,
                resumen.desapariciones,
                sum(a.es_error for a in avisos),
                sum(not a.es_error for a in avisos),
                json.dumps(resumen.por_tabla, ensure_ascii=False),
            ),
        )
        resumen.ejecucion_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO incidencias (ejecucion_id, nivel, tipo, recurso, operador, texto) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(cur.lastrowid, a.nivel, a.tipo, a.recurso, a.operador, a.texto) for a in avisos],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    resumen.avisos = avisos
    return resumen


def reconstruir(rutas: Rutas, destino: Path) -> list[Resumen]:
    """Crea en `destino` una base nueva cargando todas las fotos de raw/ en orden."""
    if destino.exists():
        raise FileExistsError(destino)
    cat, ind = leer_config(rutas)
    conn = conectar(destino)
    try:
        return [
            cargar_foto(conn, AlmacenCrudo(rutas.raw, fecha), cat, ind)
            for fecha in AlmacenCrudo.fechas(rutas.raw)
        ]
    finally:
        conn.close()
