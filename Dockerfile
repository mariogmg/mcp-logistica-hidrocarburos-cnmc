# Imagen del servidor MCP (solo lectura). No incluye el ETL en ejecución: sirve
# la base de datos que haya en data/ en el momento de construir la imagen.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

# Primero las dependencias (capa que se reutiliza si solo cambia el código).
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev
COPY data/cnmc_logistica.db ./data/cnmc_logistica.db

# Usuario sin privilegios; la base queda en solo lectura.
RUN useradd --system --uid 10001 mcp && chmod 444 data/cnmc_logistica.db
USER mcp

EXPOSE 8000
# Fuera de localhost hay que declarar los Host admitidos y conviene un token:
#   docker run -p 8000:8000 -e CNMC_MCP_HOSTS_PERMITIDOS=mi.dominio:8000 -e CNMC_MCP_TOKEN=... imagen
ENV CNMC_MCP_HOSTS_PERMITIDOS="localhost:8000,127.0.0.1:8000"
CMD ["cnmc-logistica", "servir", "--transporte", "http", "--host", "0.0.0.0", "--puerto", "8000"]
