# aiduMEI 无损秒级平滑升级指南 (Fast-Update SOP)

## 为什么需要 Fast-Update？

在 aiduMEI v18.2 及以前版本，因缺乏细粒度的 schema 变更控制，升级往往要求“全量备份数据库” + “应用层脚本重写”，动辄耗时过久。
从 v18.3.0 开始，我们引入了基于 `user_version` 的增量补丁机制（Schema Versioning）。

代码更新与数据重构彻底解耦：
- **纯逻辑更新**：直接重启生效，0 延迟。
- **表结构变更**（如增加列、建索引）：利用 SQLite O(1) 复杂度的 `ALTER TABLE ADD COLUMN`，在 API 启动的毫秒级瞬间完成，且**绝对不破坏已有数据**。

## 适用条件

1. 小版本日常更新（增加字段、补全索引、增加功能接口）
2. 跨度连续的次版本升级（例如 v18.2.0 -> v18.3.0）

## 运维升级步骤 (Standard Operating Procedure)

1. **拉取新代码**
   ```bash
   cd /path/to/aiduMEI
   git pull origin main
   ```

2. **更新依赖** (如有 requirement 变动，通常无变动直接跳过)
   ```bash
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **秒级重启 API 服务**
   ```bash
   # 如果是用 systemd 部署（服务名替换为你自己的 service 名）
   systemctl restart aidumei-api.service
   # 或者是直接重启
   # pkill -f api_server.py && python3 api_server.py &
   ```

**就这 3 步！**
在重启的瞬间，`schema_bootstrap.py` 会自动探测当前 sqlite 的版本：
- 如果落后，则按序执行 `apply_migrations()`。
- 只增加字段，不删表，用户端体验为“秒切”。

## 大版本降级回滚方案 (Rollback)

如果 v18.3 运行异常需要切回 v18.2：
1. `git checkout v18.2.x_tag`
2. 重启 API。
3. **数据完全兼容**：因为我们在 v18.3 仅作了“增加 `media_url`”等 ADD COLUMN 操作，老代码 v18.2 在执行 `SELECT *` 和 ORM 映射时，只会忽略多余的列，数据库不需要任何物理降级，数据无损保留！

---
*Powered by aiduMEI Team*
