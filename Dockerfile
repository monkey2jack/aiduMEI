FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . /app

# Install local package after app files are present
RUN pip install --no-cache-dir .

# ══ 降权（v20.0 P3-8）══
# 在此之前本镜像以 root 跑。容器里的 root 不是「隔离的 root」——
# 它和宿主机 uid 0 是同一个 uid，一旦 runtime 逃逸或挂载配置有误，
# 拿到的就是宿主机的 root。
#
# ★ uid 写成固定的 10001（不是让 useradd 自选），因为 docker-compose.yml
#   把宿主机的 ./data 和 ./logs bind-mount 进来。bind mount 不做 uid 映射：
#   容器里的写入以容器内 uid 落到宿主机文件上。uid 不固定，
#   镜像重建后可能换号，宿主机上那批文件就突然写不进了 ——
#   而症状是「读得到、写不进」，不是启动失败，很难联想到 uid。
#   宿主机侧对应要做：  sudo chown -R 10001:10001 ./data ./logs
RUN groupadd --gid 10001 aidumem \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin aidumem \
 && mkdir -p /app/data /app/logs \
 && chown -R aidumem:aidumem /app/data /app/logs

# ★ 只交接 data/ 与 logs/，**不** chown 整个 /app：
#   代码目录保持 root 属主、进程只读，等于免费拿到「运行期改不了自己代码」。
#   代价是 __pycache__ 写不进 —— 只是每次启动多花几百毫秒重新编译字节码，
#   不影响功能（Python 写不进 pycache 时静默降级，不报错）。
USER aidumem

EXPOSE 8767

# 默认只监听容器内回环，避免公网裸奔。
# 需要对外暴露时，请通过 docker run -e AIDUMEM_HOST=0.0.0.0 覆盖，
# 并务必同时设置 AIDUMEM_API_TOKEN 与 AIDUMEM_UI_PASSWORD，前置 TLS 反代。
ENV AIDUMEM_HOST="127.0.0.1"
ENV AIDUMEM_API_PORT="8767"

CMD ["aidumem"]
