FROM python:3.11-slim
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml main.py ./

RUN uv pip install --system --no-cache \
    "fastapi>=0.100.0" \
    "uvicorn[standard]>=0.20.0" \
    "google-adk[gcp]>=2.0.0,<3.0.0" \
    "google-cloud-aiplatform[agent-engines]>=1.156.0" \
    "python-dotenv>=1.0.0"

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
