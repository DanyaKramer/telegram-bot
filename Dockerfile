FROM python:3.13-slim AS builder
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install --user -r requirements.txt


FROM python:3.13-slim
WORKDIR /code

COPY --from=builder /root/.local /root/.local
COPY ./src .

# Логи в /app/logs (resolve_log_file_path проверяет этот путь первым)
RUN mkdir -p /app/logs
VOLUME ["/app/logs"]

# Для сохранения users.json и cache_data.json при перезапуске:
#   docker run -v bot-logs:/app/logs -v bot-data:/code ...

ENV PATH=/root/.local:$PATH
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "./main.py"]
