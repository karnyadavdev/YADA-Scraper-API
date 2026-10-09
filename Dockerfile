FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# Refuse to start a network-exposed container unless the operator supplies
# YADA_API_KEY.
ENV YADA_REQUIRE_API_KEY=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Playwright base already ships browsers, system deps, and the unprivileged
# pwuser account; /app/scratch stays writable so the browser service never
# needs root.
COPY --chown=pwuser:pwuser . /app
RUN mkdir -p /app/scratch && chown pwuser:pwuser /app/scratch

USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status==200 else 1)" || exit 1

CMD ["bash", "-lc", "uvicorn yada_scraper:app --host 0.0.0.0 --port ${PORT:-8000}"]
