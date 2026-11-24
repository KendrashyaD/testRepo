# One image for SAP BTP (CF Docker / Kyma) and Azure Container Apps.
# ---------- 1) Build the React UI ----------
FROM node:22-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 2) Runtime: Python backend + Node (Playwright MCP) + Chromium ----------
FROM node:22-bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MPLBACKEND=Agg

# fonts-dejavu-core: Unicode glyphs (₹, €, accents) for PDF output
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip tini ca-certificates fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

# Pre-install Playwright MCP + matching Chromium (with OS deps) so containers start fast & offline.
ARG PLAYWRIGHT_MCP_VERSION=latest
RUN npm install -g @playwright/mcp@${PLAYWRIGHT_MCP_VERSION} \
 && "$(npm root -g)/@playwright/mcp/node_modules/.bin/playwright" install --with-deps chromium \
 && chmod -R a+rx /ms-playwright \
 && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend/ backend/
COPY --from=ui /ui/dist frontend/dist

ENV MCP_CONFIG_PATH=mcp_servers.docker.json \
    PW_BROWSER=chromium \
    PW_HEADLESS=true \
    PW_NO_SANDBOX=true \
    FRONTEND_DIST=/app/frontend/dist \
    ARTIFACTS_DIR=/app/backend/artifacts \
    MPLCONFIGDIR=/tmp/matplotlib \
    PORT=8000

RUN useradd -m -u 10001 agent && mkdir -p backend/artifacts && chown -R agent:agent /app
USER 10001
WORKDIR /app/backend
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
