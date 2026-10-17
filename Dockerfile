# since-cutoff from PyPI, for CI outside GitHub Actions and for MCP directories (Glama).
# Published as ghcr.io/mohammadhijjawi97/since-cutoff:X.Y.Z on each release (from 0.7.0), and
# as :latest when it is the newest.
#
#   docker run --rm -i IMAGE              starts the MCP server over stdio (no API key or network
#                                         is needed to start it or to answer tools/list)
#   docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/src" IMAGE scan --model anthropic:claude-sonnet-4-5
#
# The release pins the version (scripts/check_versions.py checks it); there is no [run] extra.
# From 0.6.0, basedpyright and its Node.js come with that extra only (they serve
# `since-cutoff run`, which also needs a model); a build of 0.5.x still installs them.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ARG SINCE_CUTOFF_VERSION=0.5.0

RUN pip install --no-cache-dir --disable-pip-version-check "since-cutoff==${SINCE_CUTOFF_VERSION}" \
    && useradd --create-home --uid 10001 since-cutoff \
    && mkdir -m 1777 /cache /src

# The cache can be a volume; any user (`docker run -u`) can write to it.
ENV SINCE_CUTOFF_CACHE=/cache \
    PYTHONUNBUFFERED=1

USER 10001
WORKDIR /src
ENTRYPOINT ["since-cutoff"]
CMD ["mcp"]
