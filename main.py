"""AstrBot 插件「每日一记」

每日定时由大模型撰写一篇日记，完成后自动：
    1. 保存为 TXT 文件（data/plugin_data/astrbot_plugin_daily_diary/diaries/）
    2. 写入指定的知识库（支持多个）
    3. 发布到 QQ 空间（可选，Cookie 鉴权）
    4. 向指定会话推送结果通知

模块结构（便于二次开发）：
    main.py             插件入口：指令、定时调度与整体流程编排
    diary_generator.py  日记生成：提示词组装、LLM 调用、字数/违禁校验
    qzone_publisher.py  QQ 空间发布：Web 版说说接口 + Cookie 鉴权
"""

import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Plain

from .diary_generator import DiaryGenerator, DiaryResult, today_info
from .qzone_publisher import publish_diary

JOB_ID = "daily_diary_publish"  # 定时任务 ID
DEFAULT_TIME = "21:30"          # publish_time 为空或非法时的回退时间
DEFAULT_TZ = "Asia/Shanghai"    # 容器常为 UTC，默认按北京时间触发
PLUGIN_DIR_NAME = "astrbot_plugin_daily_diary"


class DailyDiaryPlugin(Star):
    """每日一记插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.generator = DiaryGenerator(context, config)
        # 定时器显式指定时区，避免跟随容器系统时区（多为 UTC）导致触发时间偏移
        self.scheduler = AsyncIOScheduler(timezone=self._resolve_timezone())
        self._lock = asyncio.Lock()  # 防止定时任务与手动触发同时生成

    def _resolve_timezone(self) -> ZoneInfo:
        """解析配置的时区；非法时依次回退到 Asia/Shanghai 与系统本地时区。"""
        schedule_cfg = self.config.get("schedule", {}) or {}
        tz_name = str(schedule_cfg.get("timezone") or DEFAULT_TZ).strip()
        for name in (tz_name, DEFAULT_TZ):
            try:
                return ZoneInfo(name)
            except Exception:
                logger.warning(f"[每日一记] 未知时区 {name!r}，尝试回退")
        return None  # None = APScheduler 跟随系统本地时区

    async def initialize(self) -> None:
        """插件加载/重载时注册定时任务（WebUI 保存配置后会自动重载插件）。"""
        if not self.scheduler.running:
            self.scheduler.start()   # 先启动调度器，add_job 后才能读到下次运行时间
        self._setup_schedule()
        logger.info("[每日一记] 插件已加载")

    async def terminate(self) -> None:
        """插件停用/卸载时关闭调度器。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # 定时调度
    # ------------------------------------------------------------------

    def _setup_schedule(self) -> None:
        """按配置注册/移除每日定时任务。"""
        cfg = self.config.get("schedule", {}) or {}
        if not cfg.get("enable", True):
            if self.scheduler.get_job(JOB_ID):
                self.scheduler.remove_job(JOB_ID)
            logger.info("[每日一记] 定时生成已停用")
            return

        time_str = str(cfg.get("publish_time") or DEFAULT_TIME).strip()
        try:
            hh, mm = time_str.split(":")
            hour, minute = int(hh), int(mm)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            logger.warning(f"[每日一记] 时间格式 {time_str!r} 无效，回退为 {DEFAULT_TIME}")
            hour, minute = 21, 30

        self.scheduler.add_job(
            self._scheduled_job,
            CronTrigger(hour=hour, minute=minute, timezone=self.scheduler.timezone),
            id=JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,  # 进程重启等错过触发点时，1 小时内补跑
            coalesce=True,            # 多次错过只补跑一次
        )
        job = self.scheduler.get_job(JOB_ID)
        next_run = str(job.next_run_time) if job and job.next_run_time else "未知"
        logger.info(
            f"[每日一记] 定时任务已排程：每日 {hour:02d}:{minute:02d}"
            f"（{self.scheduler.timezone}），下次运行：{next_run}"
        )

    async def _scheduled_job(self) -> None:
        """定时任务入口：生成日记并把结果推送到通知会话。"""
        summary = await self._run_diary()
        await self._notify_all(summary)

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("diary", alias={"每日一记"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def diary(self, event: AstrMessageEvent, action: str = ""):
        """每日一记：无参数立即生成今天的日记；「状态」查看配置；「推送」将当前会话加入/移出结果通知"""

        if action in ("状态", "status"):
            yield event.plain_result(await self._status_text())
        elif action in ("推送", "通知", "notify"):
            yield event.plain_result(self._toggle_notify(event.unified_msg_origin))
        else:
            yield event.plain_result(await self._run_diary())

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    async def _run_diary(self) -> str:
        """执行一次完整的日记流程，返回结果摘要文本。

        流程：生成与校验 → 保存 TXT → 违禁拦截 → 知识库入库 → QQ 空间发布。
        """
        if self._lock.locked():
            return "[每日一记] 已有日记生成任务正在进行，请稍候。"

        async with self._lock:
            date_str, pretty_date = today_info()
            lines = [f"📖 每日一记 · {pretty_date}"]

            # 1. 生成（内部已完成字数/违禁校验与一次重试）
            try:
                result = await self.generator.generate()
            except Exception as e:
                logger.error(f"[每日一记] 生成失败: {e}")
                return "\n".join(lines + [f"❌ 生成失败：{e}"])

            # 2. 保存 TXT（即使审核未通过也存档，便于排查）
            file_path = self._save_txt(date_str, pretty_date, result)

            # 3. 违禁拦截：不发布、不入库
            if result.violations:
                lines.append(
                    f"⛔ 审核未通过（命中：{'、'.join(result.violations)}），"
                    "已跳过知识库入库与 QQ 空间发布"
                )
                if file_path:
                    lines.append(f"📄 正文已存档：{file_path.name}")
                return "\n".join(lines)

            # 4. 写入知识库
            lines.extend(await self._store_to_kb(date_str, result.text))

            # 5. 发布 QQ 空间
            qzone = self.config.get("qzone", {}) or {}
            if qzone.get("enable"):
                ok, detail = await publish_diary(
                    result.text,
                    str(qzone.get("uin") or ""),
                    str(qzone.get("cookie") or ""),
                    str(qzone.get("privacy") or "公开"),
                )
                emoji = "🌐" if ok else "⚠️"
                lines.append(f"{emoji} QQ 空间发布{'成功' if ok else '失败'}：{detail}")

            # 6. 汇总
            note = "" if result.length_ok else "（字数偏离设定范围）"
            lines.append(f"✅ 完成，字数 {result.word_count}{note}")
            if file_path:
                lines.append(f"📄 TXT 已保存：{file_path.name}")
            return "\n".join(lines)

    def _save_txt(self, date_str: str, pretty_date: str, result: DiaryResult):
        """把日记保存为 TXT，返回文件路径；关闭保存开关时返回 None。"""
        storage = self.config.get("storage", {}) or {}
        if not storage.get("save_txt", True):
            return None

        out_dir = StarTools.get_data_dir(PLUGIN_DIR_NAME) / "diaries"
        out_dir.mkdir(parents=True, exist_ok=True)
        header = f"《每日一记》{pretty_date}\n"
        if result.violations:
            header += f"[审核未通过：{', '.join(result.violations)}]\n"

        path: Path = out_dir / f"diary_{date_str}.txt"
        try:
            path.write_text(header + "\n" + result.text + "\n", encoding="utf-8")
            return path
        except OSError as e:
            logger.error(f"[每日一记] TXT 保存失败: {e}")
            return None

    async def _store_to_kb(self, date_str: str, text: str) -> list:
        """把日记写入配置的所有知识库，返回结果消息列表。"""
        storage = self.config.get("storage", {}) or {}
        kb_names = [str(n).strip() for n in (storage.get("kb_names") or []) if str(n).strip()]
        if not kb_names:
            return []

        messages = []
        for name in kb_names:
            try:
                kb = await self.context.kb_manager.get_kb_by_name(name)
                if kb is None:
                    messages.append(f"⚠️ 知识库「{name}」不存在，请检查插件配置")
                    continue
                await kb.upload_document(
                    file_name=f"每日一记_{date_str}.txt",
                    file_content=text.encode("utf-8"),
                    file_type="txt",
                )
                messages.append(f"📚 已写入知识库「{name}」")
            except Exception as e:
                logger.error(f"[每日一记] 写入知识库 {name} 失败: {e}")
                messages.append(f"⚠️ 知识库「{name}」写入失败：{e}")
        return messages

    async def _notify_all(self, text: str) -> None:
        """把结果摘要主动推送到配置的所有通知会话。"""
        sessions = (self.config.get("notify", {}) or {}).get("sessions") or []
        for umo in sessions:
            try:
                await self.context.send_message(str(umo), MessageChain(chain=[Plain(text)]))
            except Exception as e:
                logger.warning(f"[每日一记] 推送到 {umo} 失败: {e}")

    # ------------------------------------------------------------------
    # 指令辅助
    # ------------------------------------------------------------------

    def _toggle_notify(self, umo: str) -> str:
        """把会话加入/移出通知列表，并持久化到插件配置。"""
        cfg = self.config.setdefault("notify", {})
        sessions = set(cfg.get("sessions") or [])
        if umo in sessions:
            sessions.remove(umo)
            action = "已移出"
        else:
            sessions.add(umo)
            action = "已加入"
        cfg["sessions"] = list(sessions)
        self.config.save_config()
        return f"[每日一记] 当前会话{action}通知列表（当前共 {len(sessions)} 个）"

    async def _status_text(self) -> str:
        """汇总当前配置为一段状态文本。"""
        schedule = self.config.get("schedule", {}) or {}
        llm = self.config.get("llm", {}) or {}
        storage = self.config.get("storage", {}) or {}
        qzone = self.config.get("qzone", {}) or {}
        notify = self.config.get("notify", {}) or {}
        rules = [
            r for r in str((self.config.get("content_rules", {}) or {}).get("banned_words") or "").splitlines()
            if r.strip()
        ]

        job = self.scheduler.get_job(JOB_ID)
        next_run = str(job.next_run_time) if job and job.next_run_time else "未排程"

        # 解析实际生效的提供商与模型
        provider_desc = "未配置"
        try:
            meta = self.generator.resolve_provider().meta()
            model = str(llm.get("model") or "").strip() or meta.model or "默认"
            provider_desc = f"{meta.id}（{model}）"
        except Exception as e:
            provider_desc = f"解析失败：{e}"

        kb_names = "、".join(storage.get("kb_names") or []) or "未指定"
        lines = [
            "📖 每日一记 · 当前配置",
            f"⏰ 定时：{'启用' if schedule.get('enable', True) else '停用'}，"
            f"每日 {schedule.get('publish_time') or DEFAULT_TIME}"
            f"（{self.scheduler.timezone}，下次：{next_run}）",
            f"🤖 模型：{provider_desc}",
            f"📏 字数：{llm.get('word_count_min', 200)} ~ {llm.get('word_count_max', 400)} 字",
            f"📚 知识库：{kb_names}",
            f"🚫 禁止内容：{len(rules)} 条",
            f"🌐 QQ 空间：{'启用' if qzone.get('enable') else '停用'}"
            + (
                f"（QQ {qzone.get('uin') or '?'}，{qzone.get('privacy') or '公开'}）"
                if qzone.get("enable")
                else ""
            ),
            f"📢 通知会话：{len(notify.get('sessions') or [])} 个",
        ]
        # 附上现有知识库列表，方便对照配置
        try:
            names = [kb.kb_name for kb in await self.context.kb_manager.list_kbs()]
            if names:
                lines.append(f"🗄️ 可用知识库：{'、'.join(names)}")
        except Exception:
            pass
        return "\n".join(lines)
