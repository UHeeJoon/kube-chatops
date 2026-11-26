# demo 앞단 웹 방화벽: Caddy + Coraza(OWASP CRS 4 내장). Envoy(Cilium Gateway)는 WASM 확장을 못 붙여 별도 프록시로 둔다.
FROM caddy:2.11.9-builder AS build
RUN xcaddy build v2.11.9 --with github.com/corazawaf/coraza-caddy/v2@v2.6.2

FROM caddy:2.11.9
COPY --from=build /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
ENV XDG_DATA_HOME=/tmp/caddy XDG_CONFIG_HOME=/tmp/caddy
USER 10001:10001
EXPOSE 8080 9180
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
