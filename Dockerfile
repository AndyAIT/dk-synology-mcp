FROM python:3.12-slim

LABEL maintainer="Dima <drivebossllc@gmail.com>"
LABEL description="Synology MCP Server — comprehensive NAS management via MCP"

WORKDIR /app

# Install build deps (none needed for pure-Python, but keep layer for future use)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ src/

# Install the package
RUN pip install --no-cache-dir .

# The MCP server communicates over stdio by default.
# For HTTP, set MCP_TRANSPORT=streamable-http and MCP_AUTH_TOKEN (listens on MCP_HTTP_PORT, default 8000)
EXPOSE 8000

ENTRYPOINT ["synology-mcp"]
