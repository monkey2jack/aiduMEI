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

# 默认只监听容器内回环，避免公网裸奔。
# 需要对外暴露时，请通过 docker run -e AIDUMEM_HOST=0.0.0.0 覆盖，
# 并务必同时设置 AIDUMEM_API_TOKEN 与 AIDUMEM_UI_PASSWORD，前置 TLS 反代。
ENV AIDUMEM_HOST="127.0.0.1"
ENV AIDUMEM_API_PORT="8767"

CMD ["aidumem"]
