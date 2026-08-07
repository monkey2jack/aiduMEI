FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . /app

# Install local package after app files are present
RUN pip install --no-cache-dir .

EXPOSE 8767

ENV AIDUMEM_HOST="0.0.0.0"
ENV AIDUMEM_API_PORT="8767"

CMD ["aidumem"]
