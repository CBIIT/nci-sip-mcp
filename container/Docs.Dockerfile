FROM public.ecr.aws/amazonlinux/amazonlinux@sha256:5c38fd85b00ce8c094315d9b71f671f880052314a22bd25f1a027821bf61d9d7
RUN dnf -y upgrade --refresh
RUN dnf -y install python3.14
RUN dnf clean all
COPY site /site
COPY scripts /app/scripts
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 65532:65532
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=5s CMD ["python3.14", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).read()"]
ENTRYPOINT ["python3.14", "-m", "scripts.static_server", "--directory", "/site", "--host", "0.0.0.0", "--port", "8080"]
