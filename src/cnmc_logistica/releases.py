"""Descarga del crudo publicado en las Releases de GitHub.

`raw/` no está en git: cada foto se publica como .tar.gz en una Release
«raw-AAAA-MM-DD». Esta orden las baja todas para poder reconstruir la base
desde cero en cualquier máquina. Qué hace con cada fichero de una Release:

- `raw-FECHA.tar.gz` (principal): se extrae como raw/FECHA/.
- `raw-FECHA.ejecucion-N.tar.gz` (otra descarga del mismo día): se guarda
  íntegra en raw/FECHA/_ejecuciones/N/. No se mezcla con la principal: raw/
  es inmutable y dos descargas del mismo día no tienen por qué ser idénticas.
- `ejecuciones.jsonl` de cualquiera de ellas y los `.jsonl` sueltos: sus
  líneas se añaden a raw/FECHA/ejecuciones.jsonl, sin duplicados.

Si existe un `.sha256` junto a un tarball, se comprueba antes de extraerlo.
Solo habla con GitHub; no hace ninguna petición a la CNMC.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .almacen import NOMBRE_MANIFIESTO
from .config import REPO, USER_AGENT

NOMBRE_REGISTRO = "ejecuciones.jsonl"
_ETIQUETA = re.compile(r"raw-(\d{4}-\d{2}-\d{2})")


class ErrorReleases(Exception):
    pass


@dataclass
class ResumenReleases:
    fotos_nuevas: list[str] = field(default_factory=list)
    fotos_ya_presentes: list[str] = field(default_factory=list)
    descargas_adicionales: list[str] = field(default_factory=list)  # «FECHA/N»
    lineas_de_registro: int = 0  # ejecuciones añadidas a los ejecuciones.jsonl
    ficheros_descargados: int = 0


def _cliente() -> httpx.Client:
    cabeceras = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):  # opcional: sube el límite de peticiones
        cabeceras["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=cabeceras, follow_redirects=True, timeout=120.0)


def listar_releases(cliente: httpx.Client, repo: str) -> list[dict]:
    releases, pagina = [], 1
    while True:
        r = cliente.get(
            f"https://api.github.com/repos/{repo}/releases",
            params={"per_page": 100, "page": pagina},
        )
        if r.status_code != 200:
            raise ErrorReleases(
                f"GitHub respondió {r.status_code} al listar las Releases de {repo}"
            )
        lote = r.json()
        releases += lote
        if len(lote) < 100:
            return releases
        pagina += 1


def _extraer(datos: bytes, fecha: str, destino: Path) -> None:
    """Extrae en `destino` el contenido de la carpeta FECHA/ del tarball, con comprobaciones."""
    with tarfile.open(fileobj=io.BytesIO(datos), mode="r:gz") as tar:
        miembros = []
        for m in tar.getmembers():
            partes = Path(m.name).parts
            if any(p.startswith("._") for p in partes):
                continue  # metadatos de macOS (AppleDouble) que tar añade al empaquetar
            if not partes or partes[0] != fecha or ".." in partes or m.name.startswith("/"):
                raise ErrorReleases(f"ruta inesperada en el tarball de {fecha}: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise ErrorReleases(f"entrada no admitida en el tarball de {fecha}: {m.name}")
            m.name = str(Path(*partes[1:])) if len(partes) > 1 else "."
            miembros.append(m)
        destino.mkdir(parents=True, exist_ok=True)
        tar.extractall(destino, members=miembros, filter="data")


def _anadir_lineas(registro: Path, lineas: list[str]) -> int:
    """Añade al registro las ejecuciones que no tenga (misma línea = misma ejecución)."""
    presentes = set()
    if registro.exists():
        presentes = {json.dumps(json.loads(x), sort_keys=True)
                     for x in registro.read_text("utf-8").splitlines() if x.strip()}  # fmt: skip
    nuevas = []
    for linea in lineas:
        if not linea.strip():
            continue
        canonica = json.dumps(json.loads(linea), sort_keys=True)
        if canonica not in presentes:
            presentes.add(canonica)
            nuevas.append(linea.strip())
    if nuevas:
        registro.parent.mkdir(parents=True, exist_ok=True)
        with registro.open("a", encoding="utf-8") as fh:  # solo añadir, nunca reescribir
            fh.write("".join(x + "\n" for x in nuevas))
    return len(nuevas)


def descargar_releases(
    dir_raw: Path, repo: str = REPO, cliente: httpx.Client | None = None
) -> ResumenReleases:
    cliente = cliente or _cliente()
    resumen = ResumenReleases()

    def bajar(activo: dict) -> bytes:
        r = cliente.get(activo["browser_download_url"])
        if r.status_code != 200:
            raise ErrorReleases(f"no se pudo descargar {activo['name']}: HTTP {r.status_code}")
        resumen.ficheros_descargados += 1
        return r.content

    for release in sorted(listar_releases(cliente, repo), key=lambda r: r.get("tag_name", "")):
        m = _ETIQUETA.fullmatch(release.get("tag_name", ""))
        if not m:
            continue
        fecha, etiqueta = m.group(1), release["tag_name"]
        activos = {a["name"]: a for a in release.get("assets", [])}

        def tarball(nombre: str, activos: dict = activos) -> bytes:
            datos = bajar(activos[nombre])
            if f"{nombre}.sha256" in activos:
                esperado = bajar(activos[f"{nombre}.sha256"]).split()[0].decode()
                if hashlib.sha256(datos).hexdigest() != esperado:
                    raise ErrorReleases(f"sha256 incorrecto en {nombre}")
            return datos

        adicionales = sorted(
            (int(x.group(1)), nombre)
            for nombre in activos
            if (x := re.fullmatch(rf"{re.escape(etiqueta)}\.ejecucion-(\d+)\.tar\.gz", nombre))
        )
        principal = f"{etiqueta}.tar.gz" if f"{etiqueta}.tar.gz" in activos else None
        if principal is None and adicionales:
            principal = adicionales.pop(0)[1]  # sin tarball principal: vale la primera descarga
        foto = dir_raw / fecha
        lineas: list[str] = []

        if (foto / NOMBRE_MANIFIESTO).exists():
            resumen.fotos_ya_presentes.append(fecha)  # no se toca lo que ya hay en disco
        elif principal:
            _extraer(tarball(principal), fecha, foto)
            resumen.fotos_nuevas.append(fecha)
        for n, nombre in adicionales:
            destino = foto / "_ejecuciones" / str(n)
            if not destino.exists():
                _extraer(tarball(nombre), fecha, destino)
                resumen.descargas_adicionales.append(f"{fecha}/{n}")
            registro = destino / NOMBRE_REGISTRO
            if registro.exists():
                lineas += registro.read_text("utf-8").splitlines()
        for nombre, activo in sorted(activos.items()):
            if nombre.endswith(".jsonl"):
                lineas += bajar(activo).decode("utf-8").splitlines()
        resumen.lineas_de_registro += _anadir_lineas(foto / NOMBRE_REGISTRO, lineas)
    return resumen
