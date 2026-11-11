# Caddy con el módulo de límite de peticiones (no viene en la imagen oficial).
FROM caddy:2-builder AS construccion
RUN xcaddy build --with github.com/mholt/caddy-ratelimit

FROM caddy:2
COPY --from=construccion /usr/bin/caddy /usr/bin/caddy
