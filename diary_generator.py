"""日记生成模块：提示词组装、LLM 调用、字数与违禁内容校验。

提示词结构（为了最大化供应商端前缀缓存命中）：
    system   : SYSTEM_PROMPT，完全静态，所有日记共用同一段前缀
    user     : 先放稳定配置项（字数/禁止内容/风格/补充要求），最后才放会变的日期
               重试时只在 user 末尾追加反馈，system 与 user 前缀都不动

二次开发可关注的调参点：
    - SYSTEM_PROMPT : 内置写作要求（静态，勿放入日期等可变内容）
    - WORD_TOLERANCE: 字数浮动容差，超出容差才判定字数不达标并触发重试
    - MAX_RETRY     : 校验不过时的最大重试次数
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from astrbot.api import logger

WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

WORD_TOLERANCE = 0.3  # 字数允许 ±30% 浮动，超出才算不达标
MAX_RETRY = 1         # 字数/违禁校验不过时最多重试次数

# 完全静态的系统提示词：不含日期、字数等可变信息，保证每次请求的前缀一致
SYSTEM_PROMPT = (
    "你是一位热爱生活、观察细腻的日记作者，用第一人称写当天的日记。\n"
    "写作要求：\n"
    "1. 内容真诚自然，有细节、有画面感，避免空洞套话\n"
    "2. 不出现真实他人的隐私信息\n"
    "3. 直接输出日记正文，不要标题、日期行、前言、总结，也不要 Markdown 代码块\n"
    "4. 严格遵守用户消息中给出的字数范围与禁止内容要求"
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
                      persona="", extra="") -> str:
    """组装用户消息：稳定信息在前，日期这种每天都变的信息放最后。

    前缀越稳定，供应商端能命中的缓存就越多；重试时只在末尾追加反馈，
    前面的内容保持不变，缓存依然有效。
    """
    banned = "、".join(banned_rules) if banned_rules else "无特别限制，但不要涉及他人隐私"
    parts = [
        f"【字数】{min_words}~{max_words} 字（不含空白字符）。",
        f"【禁止出现】{banned}。",
    ]
    if str(persona or "").strip():
        parts.append(f"【风格设定】{str(persona).strip()}")
    if str(extra or "").strip():
        parts.append(f"【补充要求】{str(extra).strip()}")
    parts.append(f"【日期】{pretty_date}")
    parts.append("请写今天的日记。")
    return "\n".join(parts)


class DiaryGenerator:
    """调用 LLM 生成日记，并完成字数与违禁内容校验。"""

    def __init__(self, context, config):
        self.context = context
        self.config = config  # 插件完整配置（AstrBotConfig）
        # 本轮进程内已确认可用的兜底提供商：一旦主提供商超时过一次，后续请求直接走它，
        # 避免每次重试都为同一个坏提供商反复等满超时时间。
        self._sticky_id = ""

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

    def build_prompts(self) -> tuple:
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
            persona=str(llm_cfg.get("persona") or ""),
            extra=extra,
        )
        return SYSTEM_PROMPT, user_prompt

    async def generate(self) -> DiaryResult:
        """生成一篇日记。

        校验（违禁内容 / 字数容差）不过时，带上针对性反馈重试一次；
        重试后违禁仍未通过则由调用方拦截发布与入库。
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

        system_prompt, base_user = self.build_prompts()
        result = DiaryResult()
        user_prompt = base_user
        for attempt in range(MAX_RETRY + 1):
            result.text = clean_text(await self._chat(system_prompt, user_prompt))
            result.word_count = count_words(result.text)
            result.violations = check_violations(result.text, banned_rules)
            result.length_ok = lower <= result.word_count <= upper

            if not result.violations and result.length_ok:
                return result
            if attempt == MAX_RETRY:
                break

            # 带着失败原因重试：只在用户消息末尾追加，前面（含 system）保持不动
            result.retried = True
            feedback = []
            if result.violations:
                feedback.append(f"包含禁止内容（{'、'.join(result.violations)}）")
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
