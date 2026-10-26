"""每日一记离线自测（不联网、不依赖 AstrBot 运行时）。

运行：python .ops/tests/test_daily_diary.py
"""

import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class _SilentLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _SilentLogger()
    api.AstrBotConfig = dict
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api


def _install_pkg():
    pkg = types.ModuleType("dd")
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules["dd"] = pkg


_install_stubs()
_install_pkg()

from dd.diary_generator import (  # noqa: E402
    LEGACY_DEFAULT_PROMPT, SYSTEM_PROMPT, DiaryGenerator, build_extra,
    build_user_prompt, check_violations, clean_text, count_words, truncate_words,
)
from dd.living_memory import build_memory_text, parse_topics  # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))


# ---------------------------------------------------------------- 提示词
check("系统提示词无占位符", "{" not in SYSTEM_PROMPT and "}" not in SYSTEM_PROMPT)
check("系统提示词不含日期", "年" not in SYSTEM_PROMPT)

p1 = build_user_prompt("2026年10月26日 星期一", 200, 400, ["政治", "毒品"], "温柔", "")
p2 = build_user_prompt("2027年03月01日 星期四", 200, 400, ["政治", "毒品"], "温柔", "")
head1, head2 = p1.split("【日期】")[0], p2.split("【日期】")[0]
check("跨日期前缀完全一致", head1 == head2, head1)
check("日期放在可变段", "【日期】2026年10月26日 星期一" in p1)
check("前缀顺序为 字数→禁止→风格", p1.index("【字数】") < p1.index("【禁止出现】") < p1.index("【风格设定】"))
check("禁止内容已拼接", "【禁止出现】政治、毒品。" in p1)
check("无禁止内容时给出兜底文案", "无特别限制" in build_user_prompt("2026年10月26日", 200, 400, []))
check("风格为空则不输出该行", "【风格设定】" not in build_user_prompt("2026年10月26日", 1, 2, [], ""))
check("补充要求可注入", "【补充要求】多写点夜景" in build_user_prompt("2026年10月26日", 1, 2, [], "", "多写点夜景"))

check("旧版默认模板视为未填写", build_extra(LEGACY_DEFAULT_PROMPT, "2026年10月26日", 200, 400, []) == "")
check("占位符被替换", build_extra("写 {date} 的日记，{min_words}~{max_words} 字，禁止：{banned_words}",
                                "2026年10月26日", 200, 400, ["政治"]) ==
      "写 2026年10月26日 的日记，200~400 字，禁止：政治")

# ---------------------------------------------------------------- 文本处理
check("代码围栏被去掉", clean_text("```text\n今天很好。\n```") == "今天很好。")
check("字数忽略空白", count_words("今天 很\t好。") == 5)
long_text = "第一句话很好。第二句话也不错。第三句话有点长。"
trunc = truncate_words(long_text, 8)
check("按句子截断不超限", count_words(trunc) <= 8 and trunc.endswith("。"), trunc)
check("正则违禁命中", check_violations("这里出现了政治", ["政治"]) == ["政治"])
check("非法正则回退为包含匹配", check_violations("包含[奇怪 文本", ["[奇怪"]) == ["[奇怪"])
check("无命中返回空", check_violations("正常内容", ["政治"]) == [])

# ---------------------------------------------------------------- 记忆
mem = build_memory_text("2026年10月26日 星期一", "今天去了海边。")
check("记忆文本带日期前缀", mem == "2026年10月26日 星期一 的日记：今天去了海边。", mem)
long_mem = build_memory_text("2026年10月26日", "啊" * 1000, limit=100)
check("超长记忆被截断", len(long_mem) < 200 and long_mem.endswith("……"), long_mem[-6:])
check("空日记不产生记忆", build_memory_text("2026年10月26日", "   ") == "")

topics = parse_topics("日记，心情\n城市", "2026年10月26日 星期一")
check("主题拆分去重", topics[:3] == ["日记", "心情", "城市"], str(topics))
check("主题自动补日期", "2026年10月26日 星期一" in topics)
check("主题最多 5 个", len(parse_topics("a,b,c,d,e,f,g", "日期")) <= 5)
check("主题为空时兜底", parse_topics("", "") == ["日记"])

# ---------------------------------------------------------------- 生成流程
class _FakeResp:
    def __init__(self, text):
        self.result_chain = types.SimpleNamespace(get_plain_text=lambda: text)


class _FakeProvider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def meta(self):
        return types.SimpleNamespace(id="fake/provider", provider_type="chat_completion", model="m")

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResp(self.outputs.pop(0))


class _FakeContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, pid):
        return self.provider

    def get_using_provider(self):
        return self.provider

    def get_all_providers(self):
        return [self.provider]


config = {
    "llm": {"provider_id": "fake/provider", "word_count_min": 10, "word_count_max": 20},
    "content_rules": {"banned_words": ""},
}
provider = _FakeProvider(["太长了" * 50, "今天风很轻。我去河边走了走，看见柳树发了新芽。"])
gen = DiaryGenerator(_FakeContext(provider), config)
result = __import__("asyncio").run(gen.generate())

check("首次超长触发重试", result.retried and len(provider.calls) == 2, str(len(provider.calls)))
check("重试沿用同一系统提示词", provider.calls[0]["system_prompt"] == provider.calls[1]["system_prompt"] == SYSTEM_PROMPT)
check("重试只在用户消息尾部追加", provider.calls[1]["prompt"].startswith(provider.calls[0]["prompt"]))
check("重试带反馈文案", "【重试】" in provider.calls[1]["prompt"])
check("最终结果通过校验", result.length_ok and not result.violations, f"{result.word_count}")
check("模型参数留空时为 None", provider.calls[0]["model"] is None)
check("重试次数被限制为 1", provider.calls[0]["request_max_retries"] == 1)

# ---------------------------------------------------------------- 结果
failed = [c for c in checks if not c[1]]
for name, ok, detail in checks:
    print(f"{'[OK]' if ok else '[FAIL]'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
sys.exit(1 if failed else 0)
