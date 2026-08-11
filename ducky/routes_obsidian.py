"""
ducky.routes_obsidian — Obsidian 双链同步 (Phase 3)
支持从 Obsidian 本地 Vault 或通过 API 同步 Markdown 卡片，
解析其 Frontmatter 及 [[WikiLink]] 双链语法，映射到 TreeMemory。
"""
import json
import logging
import os
import re
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ducky.mem0_runtime import get_memory, MEM0_CONFIG
from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.Obsidian")


def _is_obsidian_enabled() -> bool:
    """检查 Obsidian 模块是否启用（_features.obsidian，默认 True）"""
    try:
        if os.path.exists(MEM0_CONFIG):
            with open(MEM0_CONFIG) as f:
                cfg = json.loads(f.read())
                features = cfg.get("_features", {})
                return features.get("obsidian", True)
    except Exception as e:
        logger.warning(f"读取 features 配置失败: {e}")
    return True  # 默认开启，保持向后兼容


class ObsidianSyncRequest(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    metadata: dict = {}

def extract_wikilinks(content: str) -> list[str]:
    """提取 Markdown 中的 [[页面名]] 或 [[页面名|别名]] 双链语法"""
    # 匹配 [[...]]
    pattern = r"\[\[(.*?)\]\]"
    links = []
    for match in re.finditer(pattern, content):
        inner = match.group(1)
        # 如果包含 | 别名，取前面那部分
        page_name = inner.split("|")[0].strip()
        # 忽略空或者纯定位如 [[#标题]]
        if page_name and not page_name.startswith("#"):
            links.append(page_name)
    return list(set(links))

def register_obsidian_routes(app: FastAPI) -> None:
    @app.post("/api/obsidian/sync")
    def sync_obsidian_note(req: ObsidianSyncRequest):
        """
        接收 Obsidian 笔记推送，将其打散存入记忆
        并且分析双向链接，存入实体关系图谱 (entities/fact_entities)
        """
        # 检查模块开关
        if not _is_obsidian_enabled():
            raise HTTPException(403, "Obsidian 模块已禁用，请在配置中启用 _features.obsidian")

        try:
            mem = get_memory()

            # 1. 抽取 wikilinks 双链
            wikilinks = extract_wikilinks(req.content)

            # 2. 组装备忘录正文
            # 将 Obsidian 的标题和内容融合成事实
            text = f"# {req.title}\n{req.content}"

            meta = req.metadata.copy()
            meta["source"] = "obsidian"
            meta["category"] = "obsidian_vault"
            meta["obsidian_tags"] = req.tags
            meta["wikilinks"] = wikilinks

            # 3. 落库（user_id 从请求 metadata 读取，缺省 default，保持通用）
            user_id = req.metadata.get("user_id", "default")
            add_result = mem.add(text, user_id=user_id, metadata=meta)

            # 4. 把双链词直接作为 Entities 存入 SQLite，喂给 TreeMemory
            if wikilinks:
                try:
                    conn = get_facts_conn()
                    # 把页面标题自己也当成一个 Entity
                    entities_to_upsert = [req.title] + wikilinks

                    for ent in entities_to_upsert:
                        # 如果没有这个实体，插入
                        existed = conn.execute("SELECT entity_id FROM entities WHERE name = ?", (ent,)).fetchone()
                        if not existed:
                            conn.execute("INSERT INTO entities (name, entity_type) VALUES (?, ?)", (ent, "obsidian_node"))
                    conn.commit()
                    logger.info(f"成功将 {len(wikilinks)} 个双向链接同步至图谱节点。")
                except Exception as db_err:
                    logger.warning(f"实体图谱双链写入失败: {db_err}")

            return {
                "status": "ok",
                "message": "Obsidian 笔记及双链同步成功",
                "extracted_links": wikilinks
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Obsidian sync failed: {e}")
            raise HTTPException(500, str(e))
