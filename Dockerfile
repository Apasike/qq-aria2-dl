FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENV APP_HOST=0.0.0.0
EXPOSE 8080

CMD ["qq-aria2-dl"]