"""CLI del proyecto.

Cinco órdenes, una por responsabilidad. El ETL (`descubrir`, `actualizar`,
`reconstruir`) y el servidor (`servir`) no comparten código de red: el
servidor solo lee la base de datos.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from . import __version__
from .almacen import AlmacenCrudo
from .avisos import Aviso
from .config import REPO, USER_AGENT, Rutas

# Códigos de salida: 0 correcto, 1 terminó pero con errores registrados
# (el workflow se marca en rojo), 2 uso incorrecto u orden no disponible.
SALIDA_OK, SALIDA_CON_ERRORES, SALIDA_USO = 0, 1, 2


def _hoy() -> str:
    return date.today().isoformat()


def _resumir(avisos: list[Aviso]) -> int:
    """Imprime los avisos y devuelve el código de salida que corresponde."""
    for a in avisos:
        operador = f" [{a.operador}]" if a.operador else ""
        print(f"{a.nivel.upper():5s} {a.tipo} {a.recurso}{operador}: {a.texto}", file=sys.stderr)
    errores = sum(a.es_error for a in avisos)
    print(f"{errores} errores, {len(avisos) - errores} avisos", file=sys.stderr)
    return SALIDA_CON_ERRORES if errores else SALIDA_OK


def _descubrir(args: argparse.Namespace) -> int:
    # Importación local: `servir` no debe cargar nunca el código de red.
    from .descarga import Descargador, ErrorDescarga
    from .inventario import construir_inventario, descargar_descubrimiento, guardar_inventario

    rutas = Rutas(args.raiz)
    almacen = AlmacenCrudo(rutas.raw, args.fecha)
    try:
        with Descargador(almacen) as desc:
            avisos = descargar_descubrimiento(desc)
            print(f"{desc.peticiones} peticiones", file=sys.stderr)
    except ErrorDescarga as exc:  # robots.txt inaccesible: no se sigue
        return _resumir([Aviso("error", "descarga", "robots/robots", str(exc))])
    inventario, mas = construir_inventario(almacen)
    guardar_inventario(inventario, rutas.inventario)
    cc = inventario["control_cruzado"]
    print(
        f"{len(inventario['instalaciones'])} instalaciones, {len(inventario['detalles'])} detalles "
        f"(buscador: {cc['detalles_en_buscador']}), {len(inventario['operadores'])} operadores "
        f"→ {rutas.inventario}"
    )
    return _resumir(avisos + mas)


def _actualizar(args: argparse.Namespace) -> int:
    from .actualizar import descargar_inventario
    from .descarga import Descargador, ErrorDescarga
    from .inventario import cargar_inventario

    rutas = Rutas(args.raiz)
    if not rutas.inventario.exists():
        print("Falta config/inventario.json: ejecuta antes «descubrir».", file=sys.stderr)
        return SALIDA_USO
    almacen = AlmacenCrudo(rutas.raw, args.fecha)
    try:
        with Descargador(almacen) as desc:
            avisos = descargar_inventario(desc, cargar_inventario(rutas.inventario))
            print(f"{desc.peticiones} peticiones", file=sys.stderr)
    except ErrorDescarga as exc:
        return _resumir([Aviso("error", "descarga", "robots/robots", str(exc))])
    print(f"{len(almacen.entradas())} líneas de manifiesto en {almacen.dir}")

    from .bd import conectar
    from .carga import (
        FotoFueraDeOrden,
        FotoYaCargada,
        cargar_foto,
        leer_config,
        registrar_ejecucion,
    )

    conn = conectar(rutas.base_datos)
    try:
        resumen = cargar_foto(conn, almacen, leer_config(rutas), avisos_previos=avisos)
        registrar_ejecucion(almacen, conn, resumen)
    except (FotoYaCargada, FotoFueraDeOrden) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return SALIDA_CON_ERRORES
    finally:
        conn.close()
    _imprimir_resumen(resumen)
    return _resumir(resumen.avisos)


def _imprimir_resumen(resumen) -> None:
    print(
        f"foto {resumen.fecha}: {resumen.altas} altas, {resumen.cambios} cambios, "
        f"{resumen.desapariciones} desapariciones"
    )
    for tabla, (altas, cambios, bajas) in resumen.por_tabla.items():
        if altas or cambios or bajas:
            print(f"  {tabla:24s} +{altas}  ~{cambios}  -{bajas}")
    for ejemplo in resumen.ejemplos[:10]:
        print(f"  · {ejemplo}")


def _reconstruir(args: argparse.Namespace) -> int:
    from .carga import reconstruir

    rutas = Rutas(args.raiz)
    destino = args.salida or rutas.base_datos
    temporal = destino.with_name(destino.name + ".nueva")
    temporal.unlink(missing_ok=True)  # resto de una reconstrucción interrumpida
    if destino.exists() and not args.reemplazar:
        print(f"{destino} ya existe: usa --reemplazar o --salida.", file=sys.stderr)
        return SALIDA_USO
    # Se construye aparte y se cambia de golpe: la base nunca queda a medias.
    resumenes = reconstruir(rutas, temporal)
    temporal.replace(destino)
    avisos: list[Aviso] = []
    for resumen in resumenes:
        _imprimir_resumen(resumen)
        avisos += resumen.avisos
    print(f"{len(resumenes)} fotos → {destino}")
    return _resumir(avisos)


def _servir(args: argparse.Namespace) -> int:
    # Único import del servidor: no arrastra descargador ni parsers.
    from .servidor import servir

    rutas = Rutas(args.raiz)
    ruta_bd = Path(os.environ.get("CNMC_BD") or rutas.base_datos)
    if not ruta_bd.exists():
        print(f"No existe {ruta_bd}: ejecuta antes «actualizar».", file=sys.stderr)
        return SALIDA_USO
    servir(ruta_bd, args.transporte, args.host, args.puerto)
    return SALIDA_OK


def _descargar_releases(args: argparse.Namespace) -> int:
    from .releases import ErrorReleases, descargar_releases

    rutas = Rutas(args.raiz)
    try:
        r = descargar_releases(rutas.raw, args.repo)
    except ErrorReleases as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return SALIDA_CON_ERRORES
    print(
        f"{len(r.fotos_nuevas)} fotos nuevas, {len(r.fotos_ya_presentes)} ya presentes, "
        f"{len(r.descargas_adicionales)} descargas adicionales, "
        f"{r.lineas_de_registro} ejecuciones añadidas al registro "
        f"({r.ficheros_descargados} ficheros descargados) → {rutas.raw}"
    )
    return SALIDA_OK


def _info(args: argparse.Namespace) -> int:
    rutas = Rutas(args.raiz)
    print(f"versión:     {__version__}")
    print(f"user-agent:  {USER_AGENT}")
    print(f"raíz:        {rutas.raiz.resolve()}")
    fotos = sorted(p.name for p in rutas.raw.glob("????-??-??") if p.is_dir())
    print(f"fotos raw:   {len(fotos)}" + (f" ({fotos[0]} … {fotos[-1]})" if fotos else ""))
    print(f"inventario:  {'sí' if rutas.inventario.exists() else 'no'}")
    if not rutas.base_datos.exists():
        print("base datos:  no")
        return SALIDA_OK
    import sqlite3

    from .bd import TABLAS

    conn = sqlite3.connect(f"file:{rutas.base_datos}?mode=ro", uri=True)
    n, primera, ultima = conn.execute(
        "SELECT COUNT(*), MIN(fecha), MAX(fecha) FROM ejecuciones"
    ).fetchone()
    print(f"base datos:  {rutas.base_datos} ({n} ejecuciones, fotos {primera} … {ultima})")
    for tabla in TABLAS:
        total = conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
        vigentes = conn.execute(
            f"SELECT COUNT(*) FROM {tabla} WHERE ultima_vez_visto = ?", (ultima,)
        ).fetchone()[0]
        print(f"  {tabla:24s} {vigentes:6d} vigentes / {total:6d} filas")
    conn.close()
    return SALIDA_OK


ORDENES: dict[str, tuple[str, Callable[[argparse.Namespace], int]]] = {
    "descubrir": ("Mapas + buscador → config/inventario.json", _descubrir),
    "actualizar": ("Descarga todo el inventario a raw/ y carga la base", _actualizar),
    "reconstruir": ("Crea una base nueva a partir de raw/", _reconstruir),
    "descargar-releases": ("Baja a raw/ el crudo publicado en las Releases", _descargar_releases),
    "servir": ("Arranca el servidor MCP (solo lectura)", _servir),
    "info": ("Muestra versión, rutas y estado local", _info),
}


def crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cnmc-logistica", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--raiz", type=Path, default=Path.cwd(), help="Directorio de trabajo (raw/, config/, data/)"
    )
    sub = parser.add_subparsers(dest="orden", required=True)
    for nombre, (ayuda, funcion) in ORDENES.items():
        orden = sub.add_parser(nombre, help=ayuda)
        orden.set_defaults(funcion=funcion)
        if nombre == "servir":
            orden.add_argument(
                "--transporte", choices=("stdio", "streamable-http"), default="stdio"
            )
            orden.add_argument("--host", default="127.0.0.1")
            orden.add_argument("--puerto", type=int, default=8000)
        if nombre == "descargar-releases":
            orden.add_argument("--repo", default=REPO, help="Repositorio (usuario/nombre)")
        if nombre == "reconstruir":
            orden.add_argument("--salida", type=Path, help="Fichero de la base nueva")
            orden.add_argument(
                "--reemplazar", action="store_true", help="Sustituye la base si ya existe"
            )
        if nombre in ("descubrir", "actualizar"):
            orden.add_argument(
                "--fecha", default=_hoy(), help="Foto de raw/ en la que se guarda (AAAA-MM-DD)"
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = crear_parser().parse_args(argv)
    return args.funcion(args)


if __name__ == "__main__":
    sys.exit(main())
