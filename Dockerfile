FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLASK_APP=wsgi.py
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m vanessa && chown -R vanessa /app
USER vanessa
EXPOSE 8000
# Açılışta bekleyen migration'lar uygulanır, ardından uygulama başlar.
CMD ["sh", "-c", "flask init-db && gunicorn -w 3 -b 0.0.0.0:${PORT:-8000} --access-logfile - wsgi:app"]
