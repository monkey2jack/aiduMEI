FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

# Copy application files
COPY . /app

EXPOSE 8767

ENV AIDUMEM_HOST="0.0.0.0"
ENV AIDUMEM_API_PORT="8767"

CMD ["aidumem"]
