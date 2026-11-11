"""Actualización automática de la base en el ordenador del usuario.

Quien usa el paquete fuera del repositorio no tiene `data/`: su base
vive en la carpeta de datos del usuario y se trae de la última Release
`datos-AAAA-MM-DD` de GitHub. Reglas:

- Como mucho una comprobación al día.
- Antes de usar una base descargada se comprueban su sha256 y su integridad,
  y se sustituye de forma atómica (fichero temporal + rename).
- Nunca impide arrancar: sin red o ante cualquier fallo se sigue con la base
  que haya y se anota en el log (stderr; stdout es del protocolo MCP).
- `CNMC_SIN_ACTUALIZAR=1` lo desactiva.

Solo habla con GitHub. No importa nada del ETL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .config import REPO, USER_AGENT

NOMBRE_BD = "cnmc_logistica.db"
NOMBRE_ESTADO = "actualizacion.json"
CADA = timedelta(days=1)
_ETIQUETA = re.compile(r"datos-(\d{4}-\d{2}-\d{2})")


def _log(mensaje: str) -> None:
    print(f"[cnmc-logistica] {mensaje}", file=sys.stderr)


def dir_datos_usuario() -> Path:
    """Carpeta de datos del usuario (CNMC_DIR_DATOS la cambia)."""
    if propia := os.environ.get("CNMC_DIR_DATOS"):
        return Path(propia)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "cnmc-logistica"


def elegir_base(raiz: Path) -> tuple[Path, bool]:
    """(ruta de la base, ¿la gestiona la actualización automática?).

    1. CNMC_BD: la que diga el usuario; no se toca.
    2. data/cnmc_logistica.db bajo la raíz: se está en el repositorio; no se toca.
    3. La carpeta de datos del usuario: esta sí se descarga y se actualiza sola.
    """
    if fija := os.environ.get("CNMC_BD"):
        return Path(fija), False
    del_repo = raiz / "data" / NOMBRE_BD
    if del_repo.exists():
        return del_repo, False
    return dir_datos_usuario() / NOMBRE_BD, True


def foto_de(ruta: Path) -> str:
    """Última foto de una base, comprobando antes su integridad."""
    conn = sqlite3.connect(f"{ruta.resolve().as_uri()}?mode=ro", uri=True)
    try:
        estado = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if estado != "ok":
            raise ValueError(f"integrity_check: {estado}")
        foto = conn.execute("SELECT MAX(fecha) FROM ejecuciones").fetchone()[0]
        if not foto:
            raise ValueError("la base no tiene ninguna foto")
        return foto
    finally:
        conn.close()


def actualizar_si_procede(
    ruta: Path,
    *,
    cliente: httpx.Client | None = None,
    ahora: datetime | None = None,
    registrar: Callable[[str], None] = _log,
) -> str:
    """Trae la última Release de datos si es más reciente. Devuelve qué ha pasado.

    Nunca lanza excepciones: cualquier problema se registra y se sigue.
    """
    if os.environ.get("CNMC_SIN_ACTUALIZAR") == "1":
        return "desactivada"
    ahora = ahora or datetime.now(UTC)
    estado_ruta = ruta.parent / NOMBRE_ESTADO
    primera_vez = not ruta.exists()
    try:
        estado = json.loads(estado_ruta.read_text("utf-8")) if estado_ruta.exists() else {}
        ultima = estado.get("comprobado_en")
        if not primera_vez and ultima and ahora - datetime.fromisoformat(ultima) < CADA:
            return "comprobada hace menos de un día"

        # Sin base hay que esperar a la descarga; con base, el arranque no espera más de unos segundos.
        espera = httpx.Timeout(60.0 if primera_vez else 6.0, connect=4.0)
        cliente = cliente or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )
        r = cliente.get(
            f"https://api.github.com/repos/{REPO}/releases", params={"per_page": 30}, timeout=espera
        )
        r.raise_for_status()
        candidatas = [
            (m.group(1), rel)
            for rel in r.json()
            if (m := _ETIQUETA.fullmatch(rel.get("tag_name", "")))
        ]
        if not candidatas:
            return "no hay ninguna Release de datos publicada"
        fecha, release = max(candidatas, key=lambda c: c[0])
        local = None if primera_vez else foto_de(ruta)
        ruta.parent.mkdir(parents=True, exist_ok=True)

        def anotar() -> None:
            estado_ruta.write_text(
                json.dumps({"comprobado_en": ahora.isoformat(), "release": release["tag_name"]})
            )

        if local is not None and fecha <= local:
            anotar()
            return f"al día (foto {local})"

        activos = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
        if NOMBRE_BD not in activos or f"{NOMBRE_BD}.sha256" not in activos:
            raise ValueError(f"la Release {release['tag_name']} no trae la base o su sha256")
        esperado = cliente.get(activos[f"{NOMBRE_BD}.sha256"], timeout=espera)
        esperado.raise_for_status()
        datos = cliente.get(activos[NOMBRE_BD], timeout=espera)
        datos.raise_for_status()
        if hashlib.sha256(datos.content).hexdigest() != esperado.text.split()[0]:
            raise ValueError("el sha256 de la base descargada no coincide")

        # El temporal va en la misma carpeta: solo así el rename es atómico.
        descriptor, temporal = tempfile.mkstemp(dir=ruta.parent, prefix=".descarga-", suffix=".db")
        try:
            with os.fdopen(descriptor, "wb") as fh:
                fh.write(datos.content)
            nueva = foto_de(Path(temporal))
            if local is not None and nueva <= local:
                raise ValueError(f"la base descargada ({nueva}) no es más reciente que la local")
            os.replace(temporal, ruta)
        finally:
            Path(temporal).unlink(missing_ok=True)
        anotar()
        registrar(f"base actualizada a la foto {nueva} ({release['tag_name']})")
        return f"actualizada a la foto {nueva}"
    except Exception as exc:
        que = (
            "No se pudo descargar la base"
            if primera_vez
            else "No se pudo comprobar si hay datos nuevos"
        )
        registrar(f"{que}: {type(exc).__name__}: {exc}. Se sigue con lo que hay.")
        return f"error: {type(exc).__name__}"
