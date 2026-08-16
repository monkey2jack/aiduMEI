"""
ducky.pipeline.memory_vision — 多模态记忆提取模块 (Phase 2)
负责调用外部 Vision API 解析图片并生成 vision_caption
"""
import logging
import json
import base64
import os
import requests

logger = logging.getLogger("aiduMEM.Vision")

def get_vision_config():
    """读取配置里的 Vision 参数，优先使用独立 vision 配置段，fallback 到 llm 配置段"""
    try:
        from ducky.mem0_runtime import MEM0_CONFIG
        if os.path.exists(MEM0_CONFIG):
            with open(MEM0_CONFIG) as f:
                cfg = json.loads(f.read())
                # 优先读取独立的 vision 配置段
                if "vision" in cfg and "config" in cfg["vision"]:
                    vis_cfg = cfg["vision"]["config"]
                    # 如果 vision 段有独立配置，直接返回
                    if vis_cfg.get("api_key") or vis_cfg.get("openai_base_url"):
                        return vis_cfg
                # fallback 到 llm 配置段（保持向后兼容）
                if "llm" in cfg and "config" in cfg["llm"]:
                    return cfg["llm"]["config"]
    except Exception as e:
        logger.warning(f"读取 Vision Config 失败: {e}")
    return None

def extract_vision_caption(media_url_or_base64: str) -> str:
    """
    通过 OpenAI 兼容 Vision API 提取图片 caption 
    返回一段详细的文本描述，该描述将作为记忆本体被存入数据库
    """
    cfg = get_vision_config()
    if not cfg:
        logger.warning("多模态未配置 API，跳过解析")
        return "图片解析失败：无有效配置"

    api_key = cfg.get("api_key")
    base_url = cfg.get("openai_base_url")
    model = cfg.get("model") or "your-vision-model" # 默认占位，配置中指定

    if not api_key or not base_url:
        return "图片解析失败：未配置 api_key 或 base_url"

    logger.info(f"正在通过 {model} 提取多模态记忆...")
    
    # 构造标准 OpenAI 多模态请求
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # 简单判断是否是纯 base64 (没有 data:image 前缀) 或 URL
    # 如果是本地文件，需要在此之前读取转成 base64
    image_content = media_url_or_base64
    if not image_content.startswith("http") and not image_content.startswith("data:"):
        # 补齐 data 前缀
        image_content = f"data:image/jpeg;base64,{image_content}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这是一张需要被记忆库记住的图片。请详细描述：1. 画面主体是什么 2. 图中的 OCR 文字或显著特征 3. 图片的情景氛围。"},
                    {"type": "image_url", "image_url": {"url": image_content}}
                ]
            }
        ],
        "max_tokens": 500,
        "temperature": 0.1
    }
    
    try:
        resp = requests.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        res_json = resp.json()
        caption = res_json["choices"][0]["message"]["content"]
        # 记录 Vision 用量
        usage = res_json.get("usage", {})
        try:
            from ducky.mem0_runtime import track_vision_usage
            track_vision_usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
        except Exception as e:
            logger.debug(f"extract_vision_caption: suppressed exception: {e}")
        logger.info("多模态记忆提取完成！")
        return caption
    except Exception as e:
        logger.error(f"Vision API 请求失败: {e}")
        return f"图片解析失败: {e}"
