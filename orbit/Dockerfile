# Сборка из корня репозитория: расчётное ядро src/ лежит рядом с orbit/.
#   docker build -f orbit/Dockerfile -t orbita .
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY orbit ./orbit
RUN useradd -m orbita && mkdir -p /app/orbit/var && chown -R orbita:orbita /app
USER orbita
WORKDIR /app/orbit
# Network access: HTTPS reverse proxy + ORBIT_COOKIE_SECURE=1.
# Bind the published port to loopback (-p 127.0.0.1:8000:8000), or use a
# private proxy network without a public port. Never expose plain HTTP directly.
EXPOSE 8000
CMD ["python", "app.py", "--host", "0.0.0.0"]
