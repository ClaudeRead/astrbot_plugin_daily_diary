"""QQ 空间联动模块：通过 astrbot_plugin_qzone 插件发布日记。

依赖：需在 AstrBot 中安装并启用 astrbot_plugin_qzone（Zhalslar 开发），
并在该插件中完成 QQ 空间登录/Cookie 配置。本插件不直接管理 Cookie。

发布入口：
    ok, detail = await publish_diary(context, text)
"""

from astrbot.api import logger

QZONE_PLUGIN_NAME = "astrbot_plugin_qzone"


def get_qzone_plugin(context):
    """获取 astrbot_plugin_qzone 的插件实例；未安装/未启用/版本不兼容时返回 None。"""
    metadata = context.get_registered_star(QZONE_PLUGIN_NAME)
    star_cls = getattr(metadata, "star_cls", None) if metadata else None
    if star_cls is not None and hasattr(star_cls, "service"):
        return star_cls
    return None


async def publish_diary(context, text: str):
    """通过 astrbot_plugin_qzone 发布一篇说说。返回 (是否成功, 说明)。"""
    plugin = get_qzone_plugin(context)
    if plugin is None:
        return False, f"未找到已启用的 {QZONE_PLUGIN_NAME}，请先安装并配置该插件"

    try:
        post = await plugin.service.publish_post(text=text)
        tid = getattr(post, "tid", "") or ""
        return True, "已发布为新说说" + (f"（tid={tid}）" if tid else "")
    except Exception as e:
        logger.error(f"[每日一记] astrbot_plugin_qzone 发布失败: {e}")
        return False, str(e)[:200]
