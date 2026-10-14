FROM python:3.12-slim-bookworm
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
ENV HOST=0.0.0.0

CMD ["python", "run.py"]
