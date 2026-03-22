FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (required for PyMuPDF PDF processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libmupdf-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8891

CMD ["python", "server.py"]
