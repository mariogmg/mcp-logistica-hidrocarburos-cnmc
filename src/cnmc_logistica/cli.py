"""CLI del proyecto.

Cinco órdenes, una por responsabilidad. El ETL (`descubrir`, `actualizar`,
`reconstruir`) y el servidor (`servir`) no comparten código de red: el
servidor solo lee la base de datos.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from . import __version__
from .almacen import AlmacenCrudo
from .avisos import Aviso
from .config import USER_AGENT, Rutas

# Códigos de salida: 0 correcto, 1 terminó pero con errores registrados
# (el workflow se marca en rojo), 2 uso incorrecto u orden no disponible.
SALIDA_OK, SALIDA_CON_ERRORES, SALIDA_USO = 0, 1, 2


def _pendiente(nombre: str) -> Callable[[argparse.Namespace], int]:
    def orden(_: argparse.Namespace) -> int:
        print(f"«{nombre}» todavía no está implementada.", file=sys.stderr)
        return SALIDA_USO

    return orden


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
    print(f"{len(almacen.entradas())} recursos en {almacen.dir}")
    return _resumir(avisos)


def _info(args: argparse.Namespace) -> int:
    rutas = Rutas(args.raiz)
    print(f"versión:     {__version__}")
    print(f"user-agent:  {USER_AGENT}")
    print(f"raíz:        {rutas.raiz.resolve()}")
    fotos = sorted(p.name for p in rutas.raw.glob("????-??-??") if p.is_dir())
    print(f"fotos raw:   {len(fotos)}" + (f" ({fotos[0]} … {fotos[-1]})" if fotos else ""))
    print(f"inventario:  {'sí' if rutas.inventario.exists() else 'no'}")
    print(f"base datos:  {'sí' if rutas.base_datos.exists() else 'no'}")
    return SALIDA_OK


ORDENES: dict[str, tuple[str, Callable[[argparse.Namespace], int]]] = {
    "descubrir": ("Mapas + buscador → config/inventario.json", _descubrir),
    "actualizar": ("Descarga todo el inventario a raw/ y carga la base", _actualizar),
    "reconstruir": ("Crea una base nueva a partir de raw/", _pendiente("reconstruir")),
    "servir": ("Arranca el servidor MCP (solo lectura)", _pendiente("servir")),
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
