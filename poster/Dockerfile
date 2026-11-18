FROM python:3.11-slim
WORKDIR /app
# Only what SupoClip's social providers import.
RUN pip install --no-cache-dir requests python-dotenv cryptography "sqlalchemy[asyncio]"
COPY backend/src ./src
COPY poster/poster.py ./poster.py
ENV PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
CMD ["python", "poster.py"]
