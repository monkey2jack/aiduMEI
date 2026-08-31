你现在是部署工程师。请在我这台机器上部署 aiduMEI（爱嘟优忆思，智能体通用智慧引擎）。必须一步一步做，每一步做完先自己验证通过，再做下一步；任何一步失败就停下来告诉我原因，不要跳过、不要假装成功。
第 1 步：检查环境。运行 python3 --version 和 git --version。要求 Python 3.10–3.12（推荐 3.12）。缺少就先装好。
第 2 步：取代码。运行 git clone https://github.com/monkey2jack/aiduMEI.git ，然后 cd aiduMEI。
第 3 步：读 AGENTS.md。这是唯一入口文件，包含挡位选择表、安装命令、验证脚本和运维指引。不要读 README.md 主文——AGENTS.md 已经把所有需要的东西整理好了。
第 4 步：装依赖。python3.12 -m venv .venv && source .venv/bin/activate，然后 pip install -r requirements.txt。
第 5 步：选挡位。读 AGENTS.md 的挡位决策表。如果机器没有 API Key，选 local 档（零依赖、零 token），并追加运行 pip install .[local-embed] && python scripts/fetch_local_embed_model.py。如果有 key，选 cloud 或 auto 档。
第 6 步：配配置。cp mem0_config_local.json.example mem0_config_local.json 并填入 LLM 与 Embedding 的 API Key 及 Base URL。同时 cp .env.example .env 并编辑 AIDUMEM_ENTITY_KEYWORDS（填入用户的人名/项目代号）和 AIDUMEM_API_TOKEN。Key 向我索要，不要自己编造。
第 7 步：起服务。运行 python api_server.py 启动服务（默认监听 http://127.0.0.1:8767）。确认 curl -s http://127.0.0.1:8767/health 返回 health_status=ok 且 runtime_paths.data_dir_writable=true。
第 8 步：验证生效。运行 python scripts/e2e_smoke.py --json，确认输出 status=PASS 且 0 failures。这不是只看 /health=ok——e2e 会写入唯一 nonce、跨新会话召回、查看 trace、清理临时租户。WARN 不是 PASS，必须修复到 0 warnings 才算通过。
第 9 步：接入宿主。按 docs/AGENT_INTEGRATION.md 把 aiduMEI 接到我正在用的 AI 宿主（Hermes Agent、Claude Code 等）上，并做一次真实对话验证记忆能被读写。运行 python scripts/agent_integration_check.py 确认全部通过。
第 10 步：初始化维护。运行 bash scripts/update_crontab.sh 安装 9 项定时任务，然后用 bash scripts/update_crontab.sh --list 确认。运行 bash scripts/backup_gate.sh create initial 创建首次备份并用 bash scripts/backup_gate.sh verify <备份目录> 验证。
第 11 步：生成报告。运行 python scripts/report.py --json，把输出原文发给我。报告应包含版本、挡位、健康状态、水位、记忆数量、备份状态和下一步建议。
遇到报错先查 TROUBLESHOOTING.md 和 docs/HEALTH.md；连续两次修不好就停下来把完整报错和日志发给我。
