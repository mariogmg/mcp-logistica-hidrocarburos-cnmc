# Despliegue en un VPS

El despliegue es opcional y no hay ninguna instancia pública. Consta de una aplicación (MCP en
`/mcp`, API REST en `/api/v1`, documentación en `/docs` y comprobación en `/salud`) detrás de
Caddy, que gestiona el HTTPS con Let's Encrypt. El servidor no hace peticiones al portal de la
CNMC: una vez al mes copia la base de datos que GitHub Actions publica en el repositorio.

```
Internet ──443──► Caddy (HTTPS, límite por IP) ──► app (uvicorn :8000, solo lectura)
                                                      └── /datos/cnmc_logistica.db  ◄── timer mensual
```

## 1. Requisitos

- VPS con Linux y systemd. Las órdenes de esta guía son para Ubuntu o Debian. El arranque con
  `docker compose` se prueba en el CI de GitHub Actions; la guía completa no se ha ejecutado en
  un VPS.
- Docker Engine con el plugin `docker compose`, `git`, `curl` y `python3`.
- Un dominio o subdominio cuyo registro **A** (y **AAAA** si hay IPv6) apunte a la IP del VPS.
  Caddy no puede obtener el certificado hasta que el DNS resuelva.

## 2. Usuario y cortafuegos

```bash
# Usuario sin privilegios para el servicio (los datos y el repo son suyos)
sudo adduser --system --group --home /opt/cnmc cnmc
sudo usermod -aG docker cnmc

# Cortafuegos: solo SSH, HTTP (para el reto de Let's Encrypt y redirigir) y HTTPS
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

SSH solo con clave. En `/etc/ssh/sshd_config` (o un fichero en `sshd_config.d/`):

```
PasswordAuthentication no
PermitRootLogin no
```

Antes de aplicar el cambio (`sudo systemctl reload ssh`), comprobar desde otra terminal que el
acceso con clave funciona. En caso contrario se pierde el acceso al servidor.

## 3. Primer arranque

```bash
sudo -u cnmc -H bash
cd /opt/cnmc
git clone https://github.com/mariogmg/logistica-hidrocarburos-cnmc.git repo
ln -s repo/despliegue despliegue

# Configuración (no se versiona)
cd repo
cp .env.example .env
nano .env        # DOMINIO=tu.dominio  ·  DIR_DATOS=/opt/cnmc/datos  ·  CNMC_MCP_TOKEN (opcional)

# Base de datos: primera copia desde el repo, comprobada antes de ponerla en servicio
DIR_DATOS=/opt/cnmc/datos despliegue/actualizar_datos.sh --forzar

# Arranque (construye las dos imágenes la primera vez)
docker compose up -d --build --wait
docker compose ps
```

Comprobaciones:

```bash
curl -s https://TU.DOMINIO/salud
curl -s "https://TU.DOMINIO/api/v1/precios?categoria=almacenamiento&limit=5"
# En el navegador: https://TU.DOMINIO/docs
```

URL del conector MCP: `https://TU.DOMINIO/mcp`.

## 4. Actualización mensual de los datos

```bash
sudo cp /opt/cnmc/despliegue/cnmc-datos.service /opt/cnmc/despliegue/cnmc-datos.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cnmc-datos.timer
systemctl list-timers cnmc-datos.timer

# Prueba en seco (descarga y comprueba, no sustituye)
sudo -u cnmc DIR_DATOS=/opt/cnmc/datos /opt/cnmc/despliegue/actualizar_datos.sh --en-seco
```

El día 3 de cada mes el script descarga `data/cnmc_logistica.db` del repositorio, ejecuta
`PRAGMA integrity_check`, comprueba que su última foto es más reciente que la que está en
servicio y la sustituye con un `rename` atómico. No requiere reinicio: la aplicación abre
la base en cada petición. Si la base nueva tiene la misma fecha (p. ej. una reconstrucción),
el script debe ejecutarse a mano con `--forzar`.

## 5. Actualizar el código

```bash
sudo -u cnmc -H bash -c 'cd /opt/cnmc/repo && git pull --ff-only && docker compose up -d --build --wait'
```

## 6. Registros y diagnóstico

```bash
cd /opt/cnmc/repo
docker compose ps                      # estado y salud de los contenedores
docker compose logs -f --tail 100 app  # peticiones a la aplicación
docker compose logs -f --tail 100 caddy  # certificados y accesos
journalctl -u cnmc-datos.service -n 50 # última actualización de datos
```

## Decisiones de seguridad

- La aplicación corre sin root, con el sistema de ficheros en solo lectura y sin capacidades;
  la carpeta de datos se monta en solo lectura. Solo Caddy publica puertos.
- Caddy limita las peticiones por IP: 120 por minuto a `/api` y 60 a `/mcp`.
- El MCP solo acepta las cabeceras `Host` de `HOSTS_MCP` y, si se define `CNMC_MCP_TOKEN`,
  exige ese token. La API REST es pública y de solo lectura; no admite SQL libre.
- Ningún secreto va al repositorio: `.env` está en `.gitignore`.
