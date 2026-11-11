# Ejemplos de configuración

## Claude Desktop (transporte stdio)

Copia el bloque de `claude_desktop_config.json` en el fichero de configuración de
Claude Desktop y cambia `/RUTA/A/...` por la ruta donde hayas clonado el repo:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Reinicia Claude Desktop. El servidor arranca bajo demanda y solo lee
`data/cnmc_logistica.db`; no hace ninguna petición a la CNMC.

## Servidor HTTP (streamable-http)

```bash
# Local, sin token:
uv run cnmc-logistica servir --transporte streamable-http --puerto 8000

# Expuesto en red: declara los Host admitidos y exige un token Bearer.
export CNMC_MCP_HOSTS_PERMITIDOS="mcp.midominio.example"
export CNMC_MCP_TOKEN="$(openssl rand -hex 32)"
uv run cnmc-logistica servir --transporte streamable-http --host 0.0.0.0 --puerto 8000
```

El punto de entrada es `http://HOST:PUERTO/mcp`. El token nunca se guarda en el
repositorio: se pasa por variable de entorno.
