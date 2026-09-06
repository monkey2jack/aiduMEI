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
 && chown -R aidumem:0 /app/data /app/logs \
 && chmod -R g=u /app/data /app/logs

# ★ 只交接 data/ 与 logs/，**不** chown 整个 /app：
#   代码目录保持 root 属主、进程只读，等于免费拿到「运行期改不了自己代码」。
#   代价是 __pycache__ 写不进 —— 只是每次启动多花几百毫秒重新编译字节码，
#   不影响功能（Python 写不进 pycache 时静默降级，不报错）。
#
# ★ 组给 0、并给组写权限，不是随手放宽 —— 容器 PaaS（Kubernetes 的
#   runAsUser、OpenShift、Dockhold 等托管平台）**不理会镜像里的 USER**，
#   而是分配一个事先不知道的 uid 把进程跑起来，惯例是把它放进 gid 0。
#   目录若是 10001:10001 0755，那个 uid 就只读，症状是启动时
#   `sqlite3.OperationalError: unable to open database file`，
#   且抛在 **import 期**（ducky/salience/__init__.py 建表），
#   日志还没起来，看不出是权限问题。
#   本机复现：docker run --user 12345:0 <image> python -c "import ducky"
#   上面那条取舍不受影响：uid 仍是 10001，/app 仍是 root 属主。
USER aidumem

EXPOSE 8767

# 默认只监听容器内回环，避免公网裸奔。
# 需要对外暴露时，请通过 docker run -e AIDUMEM_HOST=0.0.0.0 覆盖，
# 并务必同时设置 AIDUMEM_API_TOKEN 与 AIDUMEM_UI_PASSWORD，前置 TLS 反代。
ENV AIDUMEM_HOST="127.0.0.1"

# ★ 这里**不设** AIDUMEM_API_PORT。镜像里设了它，`docker run` 传进来的
#   `PORT` 就永远轮不到 —— 端口链是 AIDUMEM_API_PORT → MEM0_API_PORT → PORT，
#   镜像 ENV 让第一环恒非空，容器 PaaS 那一环成了死代码。
#   不设的默认值仍是 8767（api_server.main() 的默认），`docker run` 行为不变；
#   docker-compose.yml 里那条显式的 AIDUMEM_API_PORT=8767 也照旧生效。

# ★ v20.2.5（外审 F-01）：**显式交接运行目录**。
#
#   ducky/utils.py 用 `__file__` 的上两级推导 BASE_DIR，DATA_DIR/LOG_DIR 随之
#   落在**包目录**。源码运行时那恰好就是仓库根，看不出问题；一旦按 wheel 安装，
#   包在 site-packages 里 —— 于是 DATA_DIR = site-packages/data，而这个
#   Dockerfile 上面只 chown 了 /app/data 与 /app/logs。
#
#   后果不是「权限提示不友好」：bind-mount 进来的 ./data 根本不是实际使用的
#   目录，数据落进容器层、重建即丢；site-packages 只读时首次写入直接
#   `attempt to write a readonly database`。**交付形态与源码形态的路径语义不一致。**
#
#   这三个变量把语义钉死，不依赖包装到哪儿。/health 会报出实际打开的库路径，
#   「以为挂载生效了其实没有」只能靠那个发现。
ENV AIDUMEM_HOME="/app"
ENV AIDUMEM_DATA_DIR="/app/data"
ENV AIDUMEM_LOG_DIR="/app/logs"
ENV AIDUMEM_CONFIG_FILE="/app/mem0_config_local.json"

# ★ HOME 必须指向一个**可写**目录（与 deploy/aidumem-api.service 的
#   `StateDirectory` + `Environment=HOME=` 是同一件事，那边已经修过）。
#   `useradd --no-create-home` 之后 $HOME 指向不存在的 /home/aidumem，
#   而 mem0 SDK 在 import 期就要往 $HOME 下写缓存。缺这一行的表现，
#   与生产上那次一模一样：**带着绿灯失能** ——
#       /health   → status=ok
#       但 degraded 里有 vector_backend，向量检索静默零召回
#       日志里只有一行 `mem0 SDK 加载失败: [Errno 13] Permission denied`
#   容器上还多一种走法：托管平台换了 uid 时 $HOME 变成 /，报的是 '/.mem0'。
#   本机复现：docker run <image> python -c "import mem0"

ENV HOME="/app/data"

CMD ["aidumem"]
