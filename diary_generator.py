"""日记生成模块：提示词组装、LLM 调用、字数与违禁内容校验。

二次开发可关注的调参点：
    - DEFAULT_PROMPT : 默认提示词模板（用户可在插件配置中覆盖）
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

DEFAULT_PROMPT = (
    "你是一位热爱生活、观察细腻的日记作者。请以第一人称写一篇 {date} 的日记，"
    "主题自选，可以记录生活、见闻、思考或心情。要求：\n"
    "1. 内容真诚自然，有细节、有画面感，避免空洞套话\n"
    "2. 不要出现真实他人的隐私信息\n"
    "3. 直接输出日记正文，不要标题、日期行、前言或总结"
)


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


def build_prompt(template, pretty_date, min_words, max_words, banned_rules) -> str:
    """组装最终提示词：先替换占位符，再追加硬性字数/禁止要求。

    用逐个 replace 而非 str.format，避免用户模板中的其他花括号导致 KeyError。
    硬性要求始终附加，即使用户自定义模板遗漏了占位符也能生效。
    """
    banned_desc = "、".join(banned_rules) if banned_rules else ""
    prompt = str(template).strip() or DEFAULT_PROMPT
    for key, value in (
        ("{date}", pretty_date),
        ("{min_words}", str(min_words)),
        ("{max_words}", str(max_words)),
        ("{banned_words}", banned_desc or "无"),
    ):
        prompt = prompt.replace(key, value)
    prompt += f"\n\n【硬性要求】字数控制在 {min_words}~{max_words} 字之间（不含空白字符）。"
    if banned_desc:
        prompt += f"日记中禁止出现以下内容：{banned_desc}。"
    return prompt


class DiaryGenerator:
    """调用 LLM 生成日记，并完成字数与违禁内容校验。"""

    def __init__(self, context, config):
        self.context = context
        self.config = config  # 插件完整配置（AstrBotConfig）

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

    async def _chat(self, prompt: str) -> str:
        """调用一次 LLM，返回纯文本结果。"""
        llm_cfg = self.config.get("llm", {}) or {}
        provider = self.resolve_provider()
        # model 为 None 时使用该提供商配置的默认模型
        model = str(llm_cfg.get("model") or "").strip() or None
        resp = await provider.text_chat(prompt=prompt, model=model)
        text = resp.result_chain.get_plain_text() if resp.result_chain else ""
        if not text.strip():
            raise RuntimeError("模型返回了空内容")
        return text

    async def generate(self) -> DiaryResult:
        """生成一篇日记。

        校验（违禁内容 / 字数容差）不过时，带上针对性反馈重试一次；
        重试后违禁仍未通过则由调用方拦截发布与入库。
        """
        llm_cfg = self.config.get("llm", {}) or {}
        rules_cfg = self.config.get("content_rules", {}) or {}
        _, pretty_date = today_info()

        banned_rules = [
            r for r in str(rules_cfg.get("banned_words") or "").splitlines() if r.strip()
        ]
        min_words = max(1, int(llm_cfg.get("word_count_min", 200) or 200))
        max_words = max(min_words, int(llm_cfg.get("word_count_max", 400) or 400))

        base_prompt = build_prompt(
            str(llm_cfg.get("prompt") or ""), pretty_date, min_words, max_words, banned_rules
        )
        lower = max(1, int(min_words * (1 - WORD_TOLERANCE)))
        upper = int(max_words * (1 + WORD_TOLERANCE))

        result = DiaryResult()
        prompt = base_prompt
        for attempt in range(MAX_RETRY + 1):
            result.text = clean_text(await self._chat(prompt))
            result.word_count = count_words(result.text)
            result.violations = check_violations(result.text, banned_rules)
            result.length_ok = lower <= result.word_count <= upper

            if not result.violations and result.length_ok:
                return result
            if attempt == MAX_RETRY:
                break

            # 带着失败原因重试
            result.retried = True
            feedback = []
            if result.violations:
                feedback.append(f"包含禁止内容（{'、'.join(result.violations)}）")
            if not result.length_ok:
                feedback.append(
                    f"字数 {result.word_count} 偏离 {min_words}~{max_words} 字要求"
                )
            logger.info(f"[每日一记] 校验未过（{'，'.join(feedback)}），正在重试")
            prompt = (
                f"{base_prompt}\n\n【重试】上一次输出{'，'.join(feedback)}，"
                f"请严格修正后重新输出完整日记。"
            )

        logger.warning(
            f"[每日一记] 重试后仍未完全达标：violations={result.violations}, "
            f"length_ok={result.length_ok}, words={result.word_count}"
        )
        return result
