FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_SYSTEM_PYTHON=1
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --no-cache -r pyproject.toml
COPY . .
RUN uv pip install --no-cache -e .
EXPOSE 8000
CMD ["uvicorn", "pensieve.main:app", "--host", "0.0.0.0", "--port", "8000"]
