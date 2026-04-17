FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY openaitoclaude.py ./

EXPOSE 4000

CMD ["python", "-m", "uvicorn", "openaitoclaude:app", "--host", "0.0.0.0", "--port", "4000", "--workers", "1"]
