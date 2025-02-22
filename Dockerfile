FROM python:3.13-slim AS builder 
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install --user -r requirements.txt

FROM python:3.13-slim
WORKDIR /code

COPY --from=builder /root/.local /root/.local
COPY ./src .

ENV PATH=/root/.local:$PATH

CMD ["python", "-u", "./main.py"]
