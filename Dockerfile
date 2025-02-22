FROM python AS builder 
COPY requirements.txt .

RUN pip install --user -r requirements.txt

FROM python
WORKDIR /code

COPY --from=builder /root/.local /root/.local
COPY ./src .

ENV PATH=/root/.local:$PATH

CMD ["python", "-u", "./main.py"]
