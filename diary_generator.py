"""日记生成模块：人格与提示词组装、LLM 调用、字数/违禁/人格一致性校验。

提示词结构（为了最大化供应商端前缀缓存命中）：
    system   : SYSTEM_PROMPT，完全静态，所有日记共用同一段前缀
    user     : 先放稳定配置项（人格设定/字数/禁止内容/写作风格/补充要求），
               最后才放会变的日期；重试只在 user 末尾追加反馈，system 与
               user 前缀都不动

人格与写作风格是两件事：
    - 人格设定（persona_id）从 AstrBot 已配置的人格里选，决定"我是谁"，
      优先级最高，任何输出都不能违背它；
    - 写作风格（style）是用户自定义的文字风格，只影响表达方式，不能改身份。

二次开发可关注的调参点：
    - SYSTEM_PROMPT        : 内置写作要求（静态，勿放入日期等可变内容）
    - PERSONA_CHECK_PROMPT : 人格一致性自检用的静态系统提示词
    - WORD_TOLERANCE       : 字数浮动容差，超出容差才判定字数不达标并触发重试
    - MAX_RETRY            : 校验不过时的最大重试次数
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from astrbot.api import logger

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

WORD_TOLERANCE = 0.3  # 字数允许 ±30% 浮动，超出才算不达标
MAX_RETRY = 1         # 字数/违禁/人格校验不过时最多重试次数

# AstrBot 内置默认人格的 id：它的内容是通用的英文助手设定，不适合直接写进日记提示词
SYSTEM_DEFAULT_PERSONA_ID = "default"

# 完全静态的系统提示词：不含日期、字数等可变信息，保证每次请求的前缀一致
SYSTEM_PROMPT = (
    "你是一位热爱生活、观察细腻的日记作者，用第一人称写当天的日记。\n"
    "写作要求：\n"
    "1. 内容真诚自然，有细节、有画面感，避免空洞套话\n"
    "2. 不出现真实他人的隐私信息\n"
    "3. 直接输出日记正文，不要标题、日期行、前言、总结，也不要 Markdown 代码块\n"
    "4. 严格遵守用户消息中给出的字数范围与禁止内容要求\n"
    "5. 若用户消息给出了【人格设定】，必须严格按该人格的身份、口吻、语气、价值观与"
    "说话习惯来写。人格设定优先级最高，与其它任何要求冲突时，一律以人格设定为准\n"
    "6. 【写作风格】只影响文字风格，不允许改变人格设定里的身份、口吻与立场\n"
    "7. 记忆与聊天内容只是可选素材：可以用，也可以不用；也可以写完全属于自己的生活、"
    "见闻与想象。但无论写什么，都不能与人格设定冲突\n"
    "8. 如果某段内容与人设冲突、或这个人格不可能这么说/这么做，就不要写这段内容"
)

# 人格一致性自检用的静态系统提示词（与正文生成分开，两段前缀都能各自复用缓存）
PERSONA_CHECK_PROMPT = (
    "你是一位严格的人格一致性审核员。用户消息会给出【人格设定】与一篇【日记】。\n"
    "请判断这篇日记是否符合该人格设定：身份、口吻、语气、用词习惯、价值观、"
    "会做与不会做的事，是否都与人格一致。\n"
    "只要出现身份错位、语气明显不符、价值观冲突，或这个人物不可能说出/做出日记里的"
    "内容，就判为不符合。\n"
    "只输出一个 JSON 对象，不要任何其它文字：\n"
    '{"ok": true, "reason": "一句话理由"}'
)

# 旧版本的默认提示词：老配置里若仍是这段文本，视为未填写（避免与系统提示词重复）
LEGACY_DEFAULT_PROMPT = (
    "你是一位热爱生活、观察细腻的日记作者。请以第一人称写一篇 {date} 的日记，"
    "主题自选，可以记录生活、见闻、思考或心情。要求：\n"
    "1. 内容真诚自然，有细节、有画面感，避免空洞套话\n"
    "2. 不要出现真实他人的隐私信息\n"
    "3. 直接输出日记正文，不要标题、日期行、前言或总结"
)

RETRY_SUFFIX = "【重试】上一次输出{feedback}，请严格修正后重新输出完整日记。"


@dataclass
class DiaryResult:
    """一次日记生成的结果。"""

    text: str = ""                                   # 清洗后的日记正文
    word_count: int = 0                              # 字数（去除空白字符后）
    violations: list = field(default_factory=list)   # 命中的违禁规则，非空则拦截发布
    persona_issue: str = ""                          # 人格一致性未通过的原因，非空则拦截发布
    persona_name: str = ""                           # 实际使用的人格名称（无则空）
    length_ok: bool = True                           # 字数是否在容差内（仅提示，不拦截）
    retried: bool = False                            # 是否发生过重试


def today_info() -> tuple:
    """返回 (标准日期 2026-10-24, 展示日期 2026年10月24日 星期六)。"""
    now = datetime.now()
    pretty = f"{now.year}年{now.month}月{now.day}日 {WEEKDAYS[now.weekday()]}"
    return now.strftime("%Y-%m-%d"), pretty


def count_words(text: str) -> int:
    """字数 = 去除所有空白字符后的字符数（符合中文写作习惯）。"""
    return len(re.sub(r"\s+", "", text))


# 句子切分：到句末标点为止（含标点），末尾无标点的残句单独成句
_SENTENCE_RE = re.compile(r"[^。！？!?~…\n]*[。！？!?~…\n]|[^。！？!?~…\n]+$")


def truncate_words(text: str, limit: int) -> str:
    """按句子累加截断，使去空白字数不超过 limit（保底策略，避免下游分块/发布过载）。"""
    parts, count = [], 0
    for match in _SENTENCE_RE.finditer(text):
        sentence = match.group(0)
        words = count_words(sentence)
        if parts and count + words > limit:
            break
        parts.append(sentence)
        count += words
        if count >= limit:
            break
    return "".join(parts).strip() or text[:limit]


def clean_text(text: str) -> str:
    """去除首尾空白与模型偶发输出的 Markdown 代码围栏。"""
    t = text.strip()
    if t.startswith("```"):
        first_line_end = t.find("\n")
        if first_line_end != -1:
            t = t[first_line_end + 1 :]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def check_violations(text: str, rules) -> list:
    """逐条检查违禁规则。

    每条规则优先按正则表达式匹配；正则语法非法时退回按普通文本包含匹配。
    返回命中的规则列表（保持配置中的顺序）。
    """
    hits = []
    for rule in rules:
        rule = rule.strip()
        if not rule:
            continue
        try:
            if re.search(rule, text):
                hits.append(rule)
        except re.error:
            if rule in text:
                hits.append(rule)
    return hits


def build_extra(template, pretty_date, min_words, max_words, banned_rules) -> str:
    """把用户自定义的补充要求里的占位符替换掉；仍是旧版默认模板时视为未填写。

    用逐个 replace 而非 str.format，避免用户模板中的其他花括号导致 KeyError。
    """
    text = str(template or "").strip()
    if not text or text == LEGACY_DEFAULT_PROMPT:
        return ""
    banned_desc = "、".join(banned_rules) if banned_rules else "无"
    for key, value in (
        ("{date}", pretty_date),
        ("{min_words}", str(min_words)),
        ("{max_words}", str(max_words)),
        ("{banned_words}", banned_desc),
    ):
        text = text.replace(key, value)
    return text


def build_user_prompt(pretty_date, min_words, max_words, banned_rules=(),
                      style="", extra="", persona_name="",
                      persona_text="") -> str:
    """组装用户消息：稳定信息在前，日期这种每天都变的信息放最后。

    前缀越稳定，供应商端能命中的缓存就越多；重试时只在末尾追加反馈，
    前面的内容保持不变，缓存依然有效。
    人格设定排在最前：它是最高的约束，也是所有日记共用的一段稳定前缀。
    """
    banned = "、".join(banned_rules) if banned_rules else "无特别限制，但不要涉及他人隐私"
    parts = []
    persona_text = str(persona_text or "").strip()
    if persona_text:
        name = str(persona_name or "").strip()
        if name:
            parts.append(f"【人格名称】{name}")
        parts.append(f"【人格设定】\n{persona_text}")
    parts.append(f"【字数】{min_words}~{max_words} 字（不含空白字符）。")
    parts.append(f"【禁止出现】{banned}。")
    if str(style or "").strip():
        parts.append(f"【写作风格】{str(style).strip()}")
    if str(extra or "").strip():
        parts.append(f"【补充要求】{str(extra).strip()}")
    parts.append(f"【日期】{pretty_date}")
    parts.append("请写今天的日记。")
    return "\n".join(parts)


def build_persona_check_prompt(persona_name, persona_text, diary_text) -> str:
    """组装人格一致性自检的用户消息（人格在前、日记在后，前缀可复用）。"""
    name = str(persona_name or "").strip()
    head = f"【人格名称】{name}\n" if name else ""
    return (
        f"{head}【人格设定】\n{str(persona_text or '').strip()}\n\n"
        f"【日记】\n{str(diary_text or '').strip()}"
    )


def parse_persona_check(text: str) -> tuple:
    """解析自检输出，返回 (是否通过, 理由)。

    模型偶尔会带解释文字，这里取第一个 '{' 到最后一个 '}' 的内容；
    实在解析不出时按通过处理（不因自检本身的格式问题误杀日记），只记日志。
    """
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        logger.warning(f"[每日一记] 人格自检返回无法解析，按通过处理：{raw[:80]}")
        return True, ""
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        logger.warning(f"[每日一记] 人格自检 JSON 解析失败，按通过处理：{raw[:80]}")
        return True, ""
    ok = bool(data.get("ok", True))
    reason = str(data.get("reason") or "").strip()
    return ok, reason


class DiaryGenerator:
    """调用 LLM 生成日记，并完成字数、违禁内容与人格一致性校验。"""

    def __init__(self, context, config):
        self.context = context
        self.config = config  # 插件完整配置（AstrBotConfig）
        # 本轮进程内已确认可用的兜底提供商：一旦主提供商超时过一次，后续请求直接走它，
        # 避免每次重试都为同一个坏提供商反复等满超时时间。
        self._sticky_id = ""

    # ------------------------------------------------------------------
    # 人格设定
    # ------------------------------------------------------------------

    def persona_config(self) -> tuple:
        """返回配置里选定的人格 (persona_id, 是否启用人格一致性自检)。"""
        llm_cfg = self.config.get("llm", {}) or {}
        pid = str(llm_cfg.get("persona_id") or "").strip()
        check = bool(llm_cfg.get("persona_check", True))
        return pid, check

    async def resolve_persona(self) -> tuple:
        """解析实际使用的人格，返回 (名称, 人格正文)；没有可用人格时返回 ("", "")。

        - 配置留空：使用 AstrBot 的默认人格；
        - 默认人格仍是内置的 "default"（通用英文助手设定）时视为"未指定"，
          避免把 "You are a helpful and friendly assistant." 写进日记提示词。
        """
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return "", ""
        pid, _ = self.persona_config()
        persona = None
        try:
            if pid:
                persona = manager.get_persona_v3_by_id(pid)
                if persona is None:
                    # 退回数据库里按 id 查（v3 列表只覆盖已加载的人格）
                    persona = await manager.get_persona(pid)
            else:
                persona = await manager.get_default_persona_v3()
        except Exception as e:
            logger.warning(f"[每日一记] 读取人格设定失败（{pid or '默认'}）：{e}")
            return "", ""

        if persona is None:
            if pid:
                logger.warning(f"[每日一记] 未找到人格「{pid}」，本次不使用人格设定")
            return "", ""
        if isinstance(persona, dict):
            name = str(persona.get("name") or pid or "")
            text = str(persona.get("prompt") or "")
        else:
            name = str(getattr(persona, "persona_id", "") or pid or "")
            text = str(getattr(persona, "system_prompt", "") or "")
        text = text.strip()
        if not text or name == SYSTEM_DEFAULT_PERSONA_ID:
            return "", ""
        return name, text

    # ------------------------------------------------------------------
    # 提供商
    # ------------------------------------------------------------------

    def _sticky_provider(self):
        """返回本轮已确认可用的兜底提供商，未确认或已失效时返回 None。"""
        if not self._sticky_id:
            return None
        try:
            provider = self.context.get_provider_by_id(self._sticky_id)
        except Exception:
            return None
        if provider is None or not self._is_chat_provider(provider):
            return None
        return provider

    def resolve_provider(self):
        """解析实际使用的服务提供商：优先插件配置指定，否则回退全局默认。"""
        llm_cfg = self.config.get("llm", {}) or {}
        pid = str(llm_cfg.get("provider_id") or "").strip()
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider is None:
                raise RuntimeError(f"未找到服务提供商「{pid}」，请检查插件配置")
            return provider
        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("AstrBot 尚未配置任何大模型服务提供商")
        return provider

    async def _chat(self, system_prompt: str, user_prompt: str) -> str:
        """调用一次 LLM，返回纯文本结果。

        system 走 system_prompt（静态前缀，利于缓存命中），可变内容全部放在 prompt。
        夜间更容易遇到接口超时：这里把 AstrBot 内部的自动重试压到很少次数，
        快速失败后换一个提供商兜底，避免一次超时把整个定时任务拖死。
        """
        llm_cfg = self.config.get("llm", {}) or {}
        provider = self.resolve_provider()
        sticky = self._sticky_provider()
        if sticky is not None and self._provider_id(sticky) != self._provider_id(provider):
            provider = sticky
        # model 为 None 时使用该提供商配置的默认模型
        model = str(llm_cfg.get("model") or "").strip() or None
        retries = max(1, int(llm_cfg.get("request_max_retries", 1) or 1))
        try:
            return await self._request(provider, system_prompt, user_prompt, model, retries)
        except Exception as e:
            fallback = self._fallback_provider(provider)
            if fallback is None:
                raise
            logger.warning(
                f"[每日一记] 提供商 {self._provider_id(provider)} 调用失败（{e}），"
                f"改用 {self._provider_id(fallback)} 兜底"
            )
            text = await self._request(fallback, system_prompt, user_prompt, None, retries)
            self._sticky_id = self._provider_id(fallback)
            return text

    async def _request(self, provider, system_prompt: str, user_prompt: str,
                       model, retries: int) -> str:
        """真正发起一次请求，并把空返回视为失败。"""
        resp = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model,
            request_max_retries=retries,
        )
        text = resp.result_chain.get_plain_text() if resp.result_chain else ""
        if not text.strip():
            raise RuntimeError("模型返回了空内容")
        return text

    @staticmethod
    def _provider_id(provider) -> str:
        try:
            return str(provider.meta().id)
        except Exception:
            return type(provider).__name__

    @staticmethod
    def _is_chat_provider(provider) -> bool:
        try:
            ptype = provider.meta().provider_type
        except Exception:
            return True
        return getattr(ptype, "value", str(ptype)) == "chat_completion"

    def _fallback_provider(self, current):
        """找一个可兜底的聊天提供商：优先全局默认，其次任意其它聊天提供商。"""
        candidates = []
        try:
            candidates.append(self.context.get_using_provider())
        except Exception:
            pass
        try:
            candidates.extend(self.context.get_all_providers())
        except Exception:
            pass
        current_id = self._provider_id(current)
        for candidate in candidates:
            if candidate is None or not self._is_chat_provider(candidate):
                continue
            if self._provider_id(candidate) != current_id:
                return candidate
        return None

    # ------------------------------------------------------------------
    # 提示词与生成
    # ------------------------------------------------------------------

    def build_prompts(self, persona_name="", persona_text="") -> tuple:
        """返回 (system_prompt, user_prompt)；system 永远是同一段静态文本。"""
        llm_cfg = self.config.get("llm", {}) or {}
        rules_cfg = self.config.get("content_rules", {}) or {}
        _, pretty_date = today_info()

        banned_rules = [
            r for r in str(rules_cfg.get("banned_words") or "").splitlines() if r.strip()
        ]
        min_words = max(1, int(llm_cfg.get("word_count_min", 200) or 200))
        max_words = max(min_words, int(llm_cfg.get("word_count_max", 400) or 400))

        extra = build_extra(
            llm_cfg.get("prompt"), pretty_date, min_words, max_words, banned_rules
        )
        user_prompt = build_user_prompt(
            pretty_date=pretty_date,
            min_words=min_words,
            max_words=max_words,
            banned_rules=banned_rules,
            style=str(llm_cfg.get("style") or ""),
            extra=extra,
            persona_name=persona_name,
            persona_text=persona_text,
        )
        return SYSTEM_PROMPT, user_prompt

    async def _check_persona(self, persona_name: str, persona_text: str,
                            diary_text: str) -> tuple:
        """人格一致性自检，返回 (是否通过, 理由)；未配置人格时直接通过。"""
        pid, check = self.persona_config()
        if not check or not persona_text.strip():
            return True, ""
        try:
            raw = await self._chat(
                PERSONA_CHECK_PROMPT,
                build_persona_check_prompt(persona_name, persona_text, diary_text),
            )
        except Exception as e:
            logger.warning(f"[每日一记] 人格一致性自检失败（{e}），本次跳过自检")
            return True, ""
        return parse_persona_check(raw)

    async def generate(self) -> DiaryResult:
        """生成一篇日记。

        校验（违禁内容 / 字数容差 / 人格一致性）不过时，带上针对性反馈重试一次；
        重试后仍未通过则由调用方拦截发布与入库。
        """
        llm_cfg = self.config.get("llm", {}) or {}
        min_words = max(1, int(llm_cfg.get("word_count_min", 200) or 200))
        max_words = max(min_words, int(llm_cfg.get("word_count_max", 400) or 400))
        banned_rules = [
            r for r in str((self.config.get("content_rules", {}) or {}).get("banned_words") or "").splitlines()
            if r.strip()
        ]
        lower = max(1, int(min_words * (1 - WORD_TOLERANCE)))
        upper = int(max_words * (1 + WORD_TOLERANCE))

        persona_name, persona_text = await self.resolve_persona()
        if persona_text:
            logger.info(f"[每日一记] 使用人格设定「{persona_name}」")
        system_prompt, base_user = self.build_prompts(persona_name, persona_text)

        result = DiaryResult(persona_name=persona_name)
        user_prompt = base_user
        for attempt in range(MAX_RETRY + 1):
            result.text = clean_text(await self._chat(system_prompt, user_prompt))
            result.word_count = count_words(result.text)
            result.violations = check_violations(result.text, banned_rules)
            result.length_ok = lower <= result.word_count <= upper
            persona_ok, reason = await self._check_persona(
                persona_name, persona_text, result.text
            )
            result.persona_issue = "" if persona_ok else (reason or "未通过人格一致性检查")

            if not result.violations and not result.persona_issue and result.length_ok:
                return result
            if attempt == MAX_RETRY:
                break

            # 带着失败原因重试：只在用户消息末尾追加，前面（含 system）保持不动
            result.retried = True
            feedback = []
            if result.violations:
                feedback.append(f"包含禁止内容（{'、'.join(result.violations)}）")
            if result.persona_issue:
                feedback.append(f"不符合人格设定（{result.persona_issue}）")
            if not result.length_ok:
                feedback.append(
                    f"字数 {result.word_count} 偏离 {min_words}~{max_words} 字要求"
                )
            logger.info(f"[每日一记] 校验未过（{'，'.join(feedback)}），正在重试")
            user_prompt = (
                f"{base_user}\n\n" + RETRY_SUFFIX.format(feedback='，'.join(feedback))
            )

        # 兜底：重试后仍超上限时按句子截断，保证下游（知识库分块/说说发布）不因超长失败
        if result.word_count > max_words:
            logger.warning(
                f"[每日一记] 重试后仍超长（{result.word_count} 字），截断至 {max_words} 字以内"
            )
            result.text = truncate_words(result.text, max_words)
            result.word_count = count_words(result.text)
            result.length_ok = False
        return result
