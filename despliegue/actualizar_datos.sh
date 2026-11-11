#!/usr/bin/env bash
# Trae al servidor la última base de datos publicada en el repositorio.
#
# El servidor NO descarga nada de la CNMC: el scraping lo hace GitHub Actions una
# vez al mes y deja la base en el repo. Este script solo la copia, la comprueba y
# la pone en su sitio de forma atómica (fichero temporal + rename), de modo que la
# aplicación nunca ve una base a medias y no hay que reiniciarla.
#
# Uso: actualizar_datos.sh [--en-seco] [--forzar]
#   --en-seco  descarga y comprueba, pero no sustituye nada
#   --forzar   sustituye aunque la foto no sea más reciente (primer arranque o
#              base reconstruida con la misma fecha)
# Variables: DIR_DATOS (por defecto /opt/cnmc/datos) y URL_BD.
set -euo pipefail

DIR_DATOS="${DIR_DATOS:-/opt/cnmc/datos}"
URL_BD="${URL_BD:-https://raw.githubusercontent.com/mariogmg/mcp-logistica-hidrocarburos-cnmc/main/data/cnmc_logistica.db}"
DESTINO="$DIR_DATOS/cnmc_logistica.db"
EN_SECO=0
FORZAR=0
for opcion in "$@"; do
  case "$opcion" in
    --en-seco) EN_SECO=1 ;;
    --forzar) FORZAR=1 ;;
    *) echo "Opción desconocida: $opcion" >&2; exit 2 ;;
  esac
done

mkdir -p "$DIR_DATOS"
# El temporal va en la MISMA carpeta: solo así el rename es atómico.
NUEVA="$(mktemp "$DIR_DATOS/.cnmc_logistica.db.XXXXXX")"
trap 'rm -f "$NUEVA"' EXIT

echo "Descargando $URL_BD"
curl --fail --silent --show-error --location --retry 3 --output "$NUEVA" "$URL_BD"

# Integridad y última foto de una base (usa el sqlite3 de la biblioteca estándar de Python).
foto_de() {
  python3 - "$1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
estado = conn.execute("PRAGMA integrity_check").fetchone()[0]
if estado != "ok":
    sys.exit(f"integrity_check: {estado}")
foto = conn.execute("SELECT MAX(fecha) FROM ejecuciones").fetchone()[0]
if not foto or conn.execute("SELECT COUNT(*) FROM detalles").fetchone()[0] == 0:
    sys.exit("la base no tiene datos")
print(foto)
PY
}

FOTO_NUEVA="$(foto_de "$NUEVA")"
FOTO_ACTUAL="(ninguna)"
if [ -f "$DESTINO" ]; then
  FOTO_ACTUAL="$(foto_de "$DESTINO")"
fi
echo "Foto descargada: $FOTO_NUEVA · foto en servicio: $FOTO_ACTUAL"

if [ -f "$DESTINO" ] && [ "$FORZAR" -eq 0 ] && [ ! "$FOTO_NUEVA" \> "$FOTO_ACTUAL" ]; then
  echo "La base descargada no es más reciente: no se cambia nada."
  exit 0
fi
if [ "$EN_SECO" -eq 1 ]; then
  echo "En seco: la base es válida y se habría sustituido. No se cambia nada."
  exit 0
fi

chmod 644 "$NUEVA"
mv -f "$NUEVA" "$DESTINO"   # rename atómico: la siguiente petición ya lee la base nueva
trap - EXIT
echo "Base sustituida: ahora en servicio la foto $FOTO_NUEVA"
