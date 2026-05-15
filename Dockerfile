FROM python:3.11-slim
WORKDIR /app

COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ .
COPY api/ ./api/
RUN touch api/__init__.py

# Railway injects PORT automatically; we read it in bot.py
EXPOSE 8080

CMD ["python", "bot.py"]
