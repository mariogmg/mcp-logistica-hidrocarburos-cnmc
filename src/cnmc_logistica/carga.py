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
from .categorias import Configuracion
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


def leer_config(rutas: Rutas) -> Configuracion:
    return Configuracion.de_directorio(rutas.config)


def cargar_foto(
    conn: sqlite3.Connection,
    almacen: AlmacenCrudo,
    cfg: Configuracion,
    avisos_previos: list[Aviso] | None = None,
    *,
    tipo: str = "actualizacion",
    registradas: list[dict] | None = None,
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

    # Ejecuciones reales de esta foto recuperadas de su registro (solo al reconstruir).
    for r in registradas or []:
        cur = conn.execute(
            """INSERT INTO ejecuciones (fecha, registrada_en, version, tipo, origen, recursos,
                   altas, cambios, desapariciones, errores, avisos, por_tabla)
               VALUES (?, ?, ?, 'actualizacion', 'registro', ?, ?, ?, ?, ?, ?, ?)""",
            (fecha, r["registrada_en"], r["version"], r.get("recursos", 0), r["altas"],
             r["cambios"], r["desapariciones"], r["errores"], r["avisos"],
             json.dumps(r.get("por_tabla", {}), ensure_ascii=False)),
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO incidencias (ejecucion_id, nivel, tipo, recurso, operador, texto) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(cur.lastrowid, i["nivel"], i["tipo"], i["recurso"], i.get("operador"), i["texto"])
             for i in r.get("incidencias", [])],
        )  # fmt: skip

    filas, avisos, _ = extraer_foto(almacen, cfg)
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
            """INSERT INTO ejecuciones (fecha, registrada_en, version, tipo, recursos, altas,
                   cambios, desapariciones, errores, avisos, por_tabla)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fecha,
                datetime.now(UTC).isoformat(timespec="seconds"),
                __version__,
                tipo,
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


NOMBRE_REGISTRO = "ejecuciones.jsonl"


def registrar_ejecucion(almacen: AlmacenCrudo, conn: sqlite3.Connection, resumen: Resumen) -> None:
    """Deja constancia de una actualización real dentro de su foto de raw/.

    La base se puede reconstruir desde raw/; sin este registro, reconstruir
    perdería cuándo se ejecutó cada actualización y qué incidencias tuvo. Es
    un fichero de solo añadir (una línea por ejecución), como el manifiesto.
    """
    fila = conn.execute(
        "SELECT * FROM ejecuciones WHERE id = ?", (resumen.ejecucion_id,)
    ).fetchone()
    registro = {
        "fecha": fila["fecha"],
        "registrada_en": fila["registrada_en"],
        "version": fila["version"],
        "recursos": fila["recursos"],
        "altas": fila["altas"],
        "cambios": fila["cambios"],
        "desapariciones": fila["desapariciones"],
        "errores": fila["errores"],
        "avisos": fila["avisos"],
        "por_tabla": json.loads(fila["por_tabla"]),
        "incidencias": [
            {"nivel": a.nivel, "tipo": a.tipo, "recurso": a.recurso, "operador": a.operador,
             "texto": a.texto}
            for a in resumen.avisos
        ],
    }  # fmt: skip
    almacen.dir.mkdir(parents=True, exist_ok=True)
    with (almacen.dir / NOMBRE_REGISTRO).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(registro, ensure_ascii=False) + "\n")


def leer_registro(almacen: AlmacenCrudo) -> list[dict]:
    ruta = almacen.dir / NOMBRE_REGISTRO
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8") as fh:
        return [json.loads(linea) for linea in fh if linea.strip()]


def reconstruir(rutas: Rutas, destino: Path) -> list[Resumen]:
    """Crea en `destino` una base nueva cargando todas las fotos de raw/ en orden.

    De cada foto recupera las actualizaciones reales de su registro
    (ejecuciones.jsonl) y añade la reconstrucción como un evento propio.
    """
    if destino.exists():
        raise FileExistsError(destino)
    cfg = leer_config(rutas)
    conn = conectar(destino)
    try:
        resumenes = []
        for fecha in AlmacenCrudo.fechas(rutas.raw):
            almacen = AlmacenCrudo(rutas.raw, fecha)
            resumenes.append(
                cargar_foto(
                    conn, almacen, cfg, tipo="reconstruccion", registradas=leer_registro(almacen)
                )
            )
        return resumenes
    finally:
        conn.close()
