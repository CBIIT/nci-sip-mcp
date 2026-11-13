# Build a wheel with `pdm build` first. Both stages use AL2023's Python 3.14.
FROM public.ecr.aws/amazonlinux/amazonlinux@sha256:5c6ed3b38335b2aa424a045f1e564c8c508409ead0ff3f8bf5e62c31b94082cb AS base
RUN dnf -y upgrade --refresh
RUN dnf -y install python3.14 libgomp
RUN dnf clean all

FROM base AS dependencies
RUN dnf -y install python3.14-pip
RUN python3.14 -m venv /opt/venv
ARG REQUIREMENTS=container/requirements-amd64.txt
COPY ${REQUIREMENTS} /build/requirements.txt
RUN /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-compile --no-cache-dir -r /build/requirements.txt
COPY dist/*.whl /build/
RUN /opt/venv/bin/python -m pip install --no-deps --no-index --no-compile /build/*.whl

FROM base AS runtime
ARG VERSION
ARG REVISION
ARG SOURCE
LABEL org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION \
      org.opencontainers.image.source=$SOURCE
COPY --from=dependencies /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/model-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    NCI_SI_DATA_DIR=/data \
    NCI_SI_EMBEDDING_PROVIDER=sentence-transformers \
    NCI_SI_TRANSPORT=streamable-http \
    NCI_SI_HTTP_HOST=0.0.0.0 \
    NCI_SI_HTTP_AUTH_MODE=required \
    NCI_SI_HTTP_SESSIONS=stateless \
    NCI_SI_HTTP_REQUIRE_INDEX=1
USER 65532:65532
EXPOSE 8000
STOPSIGNAL SIGTERM
# Allow 180s for the external model/index cold start, matching the smoke startup bound.
# Thereafter three failures at 30s intervals mark unhealthy; each request has a 3s bound.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('NCI_SI_HTTP_PORT', '8000') + '/health', timeout=3).close()"]
ENTRYPOINT ["/opt/venv/bin/python", "-m", "nci_si_mcp.container_entry"]
