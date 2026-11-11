"""Constantes y rutas del proyecto.

Todo lo que depende del portal (URL base) o de la identidad del proyecto
(User-Agent) vive aquí para que cambiarlo sea tocar un solo sitio.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import __version__

NOMBRE = "mcp-logistica-hidrocarburos-cnmc"
REPO = "mariogmg/mcp-logistica-hidrocarburos-cnmc"
URL_REPO = f"https://github.com/{REPO}"
# Identifica al proyecto y da una vía de contacto (el repo) sin datos personales.
USER_AGENT = f"{NOMBRE}/{__version__} (+{URL_REPO})"

ORIGEN_PORTAL = "https://hidrocarburos.cnmc.es"
BASE_PORTAL = f"{ORIGEN_PORTAL}/hidrocarburos"

# Suelo de cortesía: ninguna configuración puede bajar de 1 s entre peticiones.
INTERVALO_MINIMO_S = 1.0


@dataclass(frozen=True)
class Rutas:
    """Rutas derivadas de la raíz de trabajo (por defecto, el directorio actual)."""

    raiz: Path

    @property
    def raw(self) -> Path:
        return self.raiz / "raw"

    @property
    def config(self) -> Path:
        return self.raiz / "config"

    @property
    def inventario(self) -> Path:
        return self.config / "inventario.json"

    @property
    def base_datos(self) -> Path:
        return self.raiz / "data" / "cnmc_logistica.db"
