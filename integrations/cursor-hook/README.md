# aiduMEM × Cursor Hook

自动把 Cursor 中的代码变更存入 aiduMEM 的 Raw Drawer（原味抽屉），让 AI 能记住你写过的代码。

## 安装方式

### 方式 A：Cursor Rules（推荐）

在项目根目录创建 `.cursorrules`，添加以下内容：

```
After each significant code change, call:
POST http://127.0.0.1:8767/add/raw
Body: {"content": "<code snippet>", "source": "cursor", "user_id": "default"}

This stores your code into aiduMEM's Raw Drawer for long-term memory.
```

或把 `cursor-aidumem.mdc` 复制到 `.cursor/rules/` 目录。

### 方式 B：脚本钩子

把 `on_save_hook.sh` 注册到你的编辑器 on-save 事件：

```bash
cp on_save_hook.sh ~/.local/bin/aidumem-on-save
chmod +x ~/.local/bin/aidumem-on-save
```

触发方式（Cursor Terminal 手动）：
```bash
aidumem-on-save /path/to/changed_file.py
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AIDUMEM_URL` | `http://127.0.0.1:8767` | aiduMEM API 地址 |
| `AIDUMEM_USER_ID` | `default` | 记忆命名空间 |
