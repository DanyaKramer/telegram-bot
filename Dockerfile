FROM python:3.13-slim AS builder 
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install --user -r requirements.txt
RUN mkdir -p /app/logs && chmod 777 /app/logs


FROM python:3.13-slim
WORKDIR /code

COPY --from=builder /root/.local /root/.local
COPY ./src .

VOLUME ["/app/logs"]

ENV PATH=/root/.local:$PATH
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "./main.py"]
