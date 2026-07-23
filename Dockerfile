FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["sh", "-c", "sac serve --host 0.0.0.0 --port ${PORT:-8080}"]
