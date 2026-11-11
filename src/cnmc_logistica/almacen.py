"""Almacén de crudo: raw/AAAA-MM-DD/ + manifiesto.

Decisiones:
- Se guardan siempre los bytes tal cual llegan. La decodificación ocurre al
  leer, así un cambio de codificación del portal nunca corrompe el histórico.
- El manifiesto es JSON Lines de solo añadir: una línea por recurso. Si la
  ejecución se corta, lo ya descargado queda registrado y se puede reanudar.
- Cada recurso tiene (tipo, clave), que fija su ruta: el crudo de dos fechas
  distintas es comparable fichero a fichero.
- raw/ es inmutable: nunca se sobrescribe un fichero. Si el mismo recurso se
  vuelve a descargar el mismo día y el contenido cambia, se guarda como
  «nombre.v2.ext», «nombre.v3.ext»… La versión válida es siempre la última
  línea correcta del manifiesto para ese (tipo, clave); las anteriores se
  conservan en disco y en el manifiesto.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

NOMBRE_MANIFIESTO = "manifiesto.jsonl"
_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}")

# tipo -> (subdirectorio, extensión). Los tipos no listados van a la raíz como .html
_DISPOSICION = {
    "robots": ("", ".txt"),
    "portada": ("", ".html"),
    "mapa": ("mapas", ".html"),
    "buscador": ("buscador", ".html"),
    "ficha": ("fichas", ".html"),
    "detalle": ("detalles", ".html"),
    "origen_destino": ("origen_destino", ".html"),
    "pdf": ("pdf", ".pdf"),
}
# Cabeceras que no aportan al histórico (cookies de sesión).
_CABECERAS_EXCLUIDAS = {"set-cookie"}


@dataclass(frozen=True)
class Recurso:
    """Algo que se descarga: su tipo, su clave estable y su URL."""

    tipo: str
    clave: str
    url: str

    @property
    def tipo_clave(self) -> tuple[str, str]:
        return self.tipo, self.clave

    @property
    def ruta(self) -> str:
        if self.tipo not in _DISPOSICION:
            raise ValueError(f"tipo de recurso desconocido: {self.tipo}")
        if not re.fullmatch(r"[\w.-]+", self.clave):
            raise ValueError(f"clave no válida para un nombre de fichero: {self.clave!r}")
        subdir, ext = _DISPOSICION[self.tipo]
        return f"{subdir}/{self.clave}{ext}" if subdir else f"{self.clave}{ext}"


@dataclass(frozen=True)
class Entrada:
    """Una línea del manifiesto."""

    tipo: str
    clave: str
    url: str
    url_final: str
    estado_http: int | None
    content_type: str | None
    cabeceras: dict[str, str] | None
    nombre_archivo: str | None
    ruta: str | None
    bytes: int | None
    sha256: str | None
    descargado_en: str
    origen: str = "descarga"
    error: str | None = None
    version: int = 1  # 1 = primera descarga del día; >1 = nueva descarga con otro contenido
    motivo: str | None = None  # por qué existe una versión nueva o se anuló una anterior

    @property
    def ok(self) -> bool:
        return self.error is None and self.ruta is not None and self.estado_http == 200


def decodificar(datos: bytes, content_type: str | None) -> tuple[str, str, str | None]:
    """Devuelve (texto, codificación usada, aviso).

    Orden: charset de la cabecera HTTP → UTF-8 estricto → ISO-8859-1.
    El portal declara ISO-8859-1 en el <meta> pero sirve UTF-8, así que el
    <meta> no se consulta. ISO-8859-1 nunca falla: por eso va el último y con
    aviso, para que un cambio real de codificación quede registrado.
    """
    declarado = None
    if content_type and (m := re.search(r"charset=([\w-]+)", content_type, re.I)):
        declarado = m.group(1).lower()
        try:
            return datos.decode(declarado), declarado, None
        except (UnicodeDecodeError, LookupError):
            pass
    try:
        texto = datos.decode("utf-8")
    except UnicodeDecodeError:
        return (
            datos.decode("iso-8859-1"),
            "iso-8859-1",
            f"no es UTF-8 válido (cabecera: {declarado or 'sin charset'}); leído como ISO-8859-1",
        )
    aviso = f"la cabecera declara {declarado} pero el contenido es UTF-8" if declarado else None
    return texto, "utf-8", aviso


def nombre_de_content_disposition(valor: str | None) -> str | None:
    if not valor:
        return None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', valor, re.I)
    return m.group(1).strip() if m else None


class AlmacenCrudo:
    """Una foto diaria del portal en disco."""

    def __init__(self, dir_raw: Path, fecha: str):
        if not _FECHA.fullmatch(fecha):
            raise ValueError(f"fecha no válida: {fecha!r} (se espera AAAA-MM-DD)")
        self.fecha = fecha
        self.dir = dir_raw / fecha
        self._indice: dict[tuple[str, str], Entrada] | None = None

    @staticmethod
    def fechas(dir_raw: Path) -> list[str]:
        """Fotos disponibles, de la más antigua a la más reciente."""
        if not dir_raw.is_dir():
            return []
        return sorted(
            p.name
            for p in dir_raw.iterdir()
            if p.is_dir() and _FECHA.fullmatch(p.name) and (p / NOMBRE_MANIFIESTO).exists()
        )

    @property
    def manifiesto(self) -> Path:
        return self.dir / NOMBRE_MANIFIESTO

    def entradas(self) -> list[Entrada]:
        if not self.manifiesto.exists():
            return []
        with self.manifiesto.open(encoding="utf-8") as fh:
            return [Entrada(**json.loads(linea)) for linea in fh if linea.strip()]

    def _cargar_indice(self) -> dict[tuple[str, str], Entrada]:
        if self._indice is None:
            # Gana la última línea correcta de cada recurso.
            self._indice = {(e.tipo, e.clave): e for e in self.entradas() if e.ok}
        return self._indice

    def buscar(self, tipo: str, clave: str) -> Entrada | None:
        """Última descarga correcta de ese recurso en esta foto, si la hay."""
        return self._cargar_indice().get((tipo, str(clave)))

    def de_tipo(self, tipo: str) -> list[Entrada]:
        return [e for e in self._cargar_indice().values() if e.tipo == tipo]

    def _anotar(self, entrada: Entrada) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.manifiesto.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entrada), ensure_ascii=False) + "\n")
        if entrada.ok:
            self._cargar_indice()[(entrada.tipo, entrada.clave)] = entrada

    def guardar(
        self,
        recurso: Recurso,
        contenido: bytes,
        *,
        url_final: str,
        estado_http: int,
        cabeceras: dict[str, str],
        motivo: str | None = None,
    ) -> Entrada:
        cabeceras = {k.lower(): v for k, v in cabeceras.items()}
        cabeceras = {k: v for k, v in cabeceras.items() if k not in _CABECERAS_EXCLUIDAS}
        sha = hashlib.sha256(contenido).hexdigest()
        ruta, version = self._ruta_libre(recurso, sha)
        destino = self.dir / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        if not destino.exists():
            # Modo «x»: si el fichero existiera, falla en vez de sobrescribir.
            with destino.open("xb") as fh:
                fh.write(contenido)
        entrada = Entrada(
            tipo=recurso.tipo,
            clave=recurso.clave,
            url=recurso.url,
            url_final=url_final,
            estado_http=estado_http,
            content_type=cabeceras.get("content-type"),
            cabeceras=cabeceras,
            nombre_archivo=nombre_de_content_disposition(cabeceras.get("content-disposition")),
            ruta=ruta,
            bytes=len(contenido),
            sha256=sha,
            descargado_en=_ahora(),
            version=version,
            motivo=motivo,
        )
        self._anotar(entrada)
        return entrada

    def _ruta_libre(self, recurso: Recurso, sha: str) -> tuple[str, int]:
        """Ruta donde guardar este contenido sin pisar nada, y su número de versión.

        Si ya hay una versión con exactamente los mismos bytes se reutiliza su
        fichero (p. ej. robots.txt, que se consulta en cada ejecución).
        """
        base = Path(recurso.ruta)
        version = 1
        while True:
            ruta = base if version == 1 else base.with_name(f"{base.stem}.v{version}{base.suffix}")
            destino = self.dir / ruta
            if not destino.exists() or hashlib.sha256(destino.read_bytes()).hexdigest() == sha:
                return ruta.as_posix(), version
            version += 1

    def registrar_fallo(self, recurso: Recurso, estado_http: int | None, error: str) -> Entrada:
        entrada = Entrada(
            tipo=recurso.tipo,
            clave=recurso.clave,
            url=recurso.url,
            url_final=recurso.url,
            estado_http=estado_http,
            content_type=None,
            cabeceras=None,
            nombre_archivo=None,
            ruta=None,
            bytes=None,
            sha256=None,
            descargado_en=_ahora(),
            error=error,
        )
        self._anotar(entrada)
        return entrada

    def leer_bytes(self, entrada: Entrada) -> bytes:
        if entrada.ruta is None:
            raise FileNotFoundError(f"{entrada.tipo}/{entrada.clave} no tiene crudo guardado")
        datos = (self.dir / entrada.ruta).read_bytes()
        if entrada.sha256 and hashlib.sha256(datos).hexdigest() != entrada.sha256:
            raise OSError(f"sha256 no coincide con el manifiesto: {entrada.ruta}")
        return datos

    def leer_texto(self, entrada: Entrada) -> tuple[str, str | None]:
        """(texto, aviso de codificación si lo hay)."""
        texto, _, aviso = decodificar(self.leer_bytes(entrada), entrada.content_type)
        return texto, aviso


def _ahora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
