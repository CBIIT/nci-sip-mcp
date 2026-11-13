FROM public.ecr.aws/amazonlinux/amazonlinux@sha256:5c6ed3b38335b2aa424a045f1e564c8c508409ead0ff3f8bf5e62c31b94082cb AS base
RUN dnf -y upgrade --refresh
RUN dnf -y install python3.14 libgomp
RUN dnf clean all
FROM base AS dependencies
RUN dnf -y install python3.14-pip
RUN python3.14 -m venv /opt/venv
COPY requirements.txt /build/requirements.txt
RUN /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: --no-compile --no-cache-dir -r /build/requirements.txt
COPY wheels/*.whl /build/
RUN /opt/venv/bin/python -m pip install --no-deps --no-index --no-compile /build/*.whl
# Package installation belongs to the build stage, not the running administrator.
RUN /opt/venv/bin/python -m pip uninstall --yes pip
FROM base AS runtime
COPY --from=dependencies /opt/venv /opt/venv
COPY app /app
COPY bundle/operator-source.* /app/
RUN mkdir /state && chown 65532:65532 /state
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src:/app/acceptance/src:/app HOME=/tmp
WORKDIR /app
USER 65532:65532
EXPOSE 8081
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=3).read()"]
ENTRYPOINT ["python", "-m", "scripts.companion_entry"]
