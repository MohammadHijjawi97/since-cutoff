# Starts the since-cutoff MCP server over stdio for MCP directories (Glama).
# No API key or network is needed to start or to answer tools/list.
FROM python:3.12-slim
RUN pip install --no-cache-dir "since-cutoff>=0.2.0"
ENTRYPOINT ["since-cutoff", "mcp"]
