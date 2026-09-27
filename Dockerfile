# Stage 1: official HelixDB server image (binary only, distroless)
FROM ghcr.io/helixdb/helixdb:v0.0.6 AS helixdb

# Stage 2: Python API layer + helix-server binary
FROM python:3.12-slim

COPY --from=helixdb /bin/helix-server /usr/local/bin/helix-server

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY hq.py app.py loader.py supervisor.py ./
COPY graph.jsonl manifest.json ./

EXPOSE 8000
ENV HELIX_DATA_DIR=/data
CMD ["python3", "/srv/supervisor.py"]
