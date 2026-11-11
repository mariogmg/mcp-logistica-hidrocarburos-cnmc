"""CLI del proyecto.

Cinco órdenes, una por responsabilidad. El ETL (`descubrir`, `actualizar`,
`reconstruir`) y el servidor (`servir`) no comparten código de red: el
servidor solo lee la base de datos.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .config import USER_AGENT, Rutas

# Códigos de salida: 0 correcto, 1 terminó pero con errores registrados
# (el workflow se marca en rojo), 2 uso incorrecto u orden no disponible.
SALIDA_OK, SALIDA_CON_ERRORES, SALIDA_USO = 0, 1, 2


def _pendiente(nombre: str) -> Callable[[argparse.Namespace], int]:
    def orden(_: argparse.Namespace) -> int:
        print(f"«{nombre}» todavía no está implementada.", file=sys.stderr)
        return SALIDA_USO

    return orden


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
    "descubrir": ("Mapas + buscador → config/inventario.json", _pendiente("descubrir")),
    "actualizar": ("Descarga todo el inventario a raw/ y carga la base", _pendiente("actualizar")),
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
        sub.add_parser(nombre, help=ayuda).set_defaults(funcion=funcion)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = crear_parser().parse_args(argv)
    return args.funcion(args)


if __name__ == "__main__":
    sys.exit(main())
