"""Incidencias que no detienen la ejecución.

Un bloque o un PDF que no se entiende nunca aborta: se anota aquí (con el id,
el operador y el texto), se conserva el crudo y se sigue. Los de nivel
«error» hacen que la orden termine con código distinto de cero.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Aviso:
    nivel: str  # "error" | "aviso"
    tipo: str  # p. ej. bloque_desconocido, unidad_desconocida, sin_categoria, descarga
    recurso: str  # "detalle/17", "ficha/16", "pdf/24"…
    texto: str
    operador: str | None = None

    @property
    def es_error(self) -> bool:
        return self.nivel == "error"
