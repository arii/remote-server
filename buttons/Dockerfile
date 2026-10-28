# buttons — allowlisted action runner for the S5 phone UI.
# Built on the S5 at deploy time (docker compose up -d --build buttons);
# no GHCR push — the image is tiny (docker CLI + stdlib Python).
# Runs as root: it needs the Docker socket (root-equivalent, same trust
# level as the terminal container). Never publish its port — ingress is
# via oauth2-proxy -> caddy only.
FROM docker:27-cli
RUN apk add --no-cache python3 git docker-cli-compose
COPY server.py /app/server.py
EXPOSE 8000
CMD ["python3", "/app/server.py"]
