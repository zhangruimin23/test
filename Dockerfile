FROM python:3.12-slim

WORKDIR /app
COPY dataeng_cli.py /app/dataeng_cli.py

ENTRYPOINT ["python", "/app/dataeng_cli.py"]
