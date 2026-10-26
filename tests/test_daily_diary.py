"""每日一记离线自测（不联网、不依赖 AstrBot 运行时）。

运行：python .ops/tests/test_daily_diary.py
"""

import asyncio
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 兼容两种布局：仓库内（tests/ 与插件文件同级）与工作副本（.ops/tests 与 .ops/plugins）
PLUGIN_DIR = (
    _HERE.parent
    if (_HERE.parent / "diary_generator.py").exists()
    else _HERE.parent / "plugins" / "astrbot_plugin_daily_diary"
)


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
    LEGACY_DEFAULT_PROMPT, PERSONA_CHECK_PROMPT, SYSTEM_PROMPT, DiaryGenerator,
    build_extra, build_persona_check_prompt, build_user_prompt, check_violations,
    clean_text, count_words, parse_persona_check, truncate_words,
)
from dd.living_memory import build_memory_text, parse_topics  # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))


PERSONA_TEXT = "我叫张蕾，是一位住在杭州的插画师，说话温柔、喜欢用短句和比喻。"
PERSONA_NAME = "张蕾"

# ---------------------------------------------------------------- 提示词结构
check("系统提示词无占位符", "{" not in SYSTEM_PROMPT and "}" not in SYSTEM_PROMPT)
check("系统提示词不含日期", "年" not in SYSTEM_PROMPT)
check("系统提示词含人格优先条款", "人格设定优先级最高" in SYSTEM_PROMPT)
check("系统提示词禁止写作风格改身份", "不允许改变人格设定里的身份" in SYSTEM_PROMPT)
check("系统提示词允许引用也可不用记忆", "可以用，也可以不用" in SYSTEM_PROMPT)
check("系统提示词允许写自己的生活", "也可以写完全属于自己的生活" in SYSTEM_PROMPT)

p1 = build_user_prompt("2026年10月26日 星期一", 200, 400, ["政治", "毒品"], "温柔", "多写点夜景",
                       PERSONA_NAME, PERSONA_TEXT)
p2 = build_user_prompt("2027年03月01日 星期四", 200, 400, ["政治", "毒品"], "温柔", "多写点夜景",
                       PERSONA_NAME, PERSONA_TEXT)
head1, head2 = p1.split("【日期】")[0], p2.split("【日期】")[0]
check("跨日期前缀完全一致", head1 == head2, head1[:60])
check("日期放在可变段", "【日期】2026年10月26日 星期一" in p1)
check(
    "前缀顺序 人格→字数→禁止→风格→补充→日期",
    p1.index("【人格设定】") < p1.index("【字数】") < p1.index("【禁止出现】")
    < p1.index("【写作风格】") < p1.index("【补充要求】") < p1.index("【日期】"),
)
check("人格名称与正文都注入", f"【人格名称】{PERSONA_NAME}" in p1 and PERSONA_TEXT in p1)
check("禁止内容已拼接", "【禁止出现】政治、毒品。" in p1)
check("无禁止内容时给出兜底文案", "无特别限制" in build_user_prompt("2026年10月26日", 200, 400, []))
check("写作风格为空则不输出该行", "【写作风格】" not in build_user_prompt("2026年10月26日", 1, 2, [], ""))
check("补充要求可注入",
      "【补充要求】多写点夜景" in build_user_prompt("2026年10月26日", 1, 2, [], "", "多写点夜景"))
check("无人格时不出现人格段", "【人格设定】" not in build_user_prompt("2026年10月26日", 1, 2))

check("旧版默认模板视为未填写", build_extra(LEGACY_DEFAULT_PROMPT, "2026年10月26日", 200, 400, []) == "")
check("占位符被替换", build_extra("写 {date} 的日记，{min_words}~{max_words} 字，禁止：{banned_words}",
                                "2026年10月26日", 200, 400, ["政治"]) ==
      "写 2026年10月26日 的日记，200~400 字，禁止：政治")

# ---------------------------------------------------------------- 人格自检
check("自检提示词是静态的", "{" in PERSONA_CHECK_PROMPT and "日记" in PERSONA_CHECK_PROMPT)
cp = build_persona_check_prompt(PERSONA_NAME, PERSONA_TEXT, "今天很好。")
check("自检消息含人格与日记", PERSONA_TEXT in cp and "【日记】\n今天很好。" in cp)
check("自检通过解析", parse_persona_check('{"ok": true, "reason": ""}') == (True, ""))
check("自检不通过解析", parse_persona_check('前面的解释 {"ok": false, "reason": "语气不符"} 尾巴')
      == (False, "语气不符"))
check("自检非法 JSON 按通过", parse_persona_check("我看不懂")[0] is True)

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


class _FakePersonaManager:
    def __init__(self, personas):
        self.personas = personas
        self.default_id = "default"

    def get_persona_v3_by_id(self, pid):
        return self.personas.get(pid)

    async def get_persona(self, pid):
        return None

    async def get_default_persona_v3(self, umo=None):
        return self.personas.get(self.default_id)


class _FakeContext:
    def __init__(self, provider, personas=None):
        self.provider = provider
        self.persona_manager = _FakePersonaManager(personas or {"default": {
            "name": "default", "prompt": "You are a helpful and friendly assistant."}})

    def get_provider_by_id(self, pid):
        return self.provider

    def get_using_provider(self):
        return self.provider

    def get_all_providers(self):
        return [self.provider]


GOOD = "今天风很轻，我去河边走了走。"          # 15 字，落在 10~20 字容差区间内
GOOD2 = "晚风很软，我在窗边喝了一杯茶。"      # 15 字

# 场景 1：字数超长 → 重试一次后通过（无自定义人格）
config = {
    "llm": {"provider_id": "fake/provider", "word_count_min": 10, "word_count_max": 20},
    "content_rules": {"banned_words": ""},
}
provider = _FakeProvider(["太长了" * 50, GOOD])
gen = DiaryGenerator(_FakeContext(provider), config)
result = asyncio.run(gen.generate())

check("首次超长触发重试", result.retried and len(provider.calls) == 2, str(len(provider.calls)))
check("重试沿用同一系统提示词",
      provider.calls[0]["system_prompt"] == provider.calls[1]["system_prompt"] == SYSTEM_PROMPT)
check("重试只在用户消息尾部追加", provider.calls[1]["prompt"].startswith(provider.calls[0]["prompt"]))
check("重试带反馈文案", "【重试】" in provider.calls[1]["prompt"])
check("最终结果通过校验", result.length_ok and not result.violations and not result.persona_issue,
      f"{result.word_count}")
check("模型参数留空时为 None", provider.calls[0]["model"] is None)
check("重试次数被限制为 1", provider.calls[0]["request_max_retries"] == 1)
check("内置 default 人格视为未指定", result.persona_name == "", result.persona_name)

# 场景 2：人格自检先不通过 → 带原因重写 → 通过
personas = {"pp": {"name": PERSONA_NAME, "prompt": PERSONA_TEXT},
            "default": {"name": "default", "prompt": "You are a helpful assistant."}}
config2 = {
    "llm": {"provider_id": "fake/provider", "word_count_min": 10, "word_count_max": 20,
            "persona_id": "pp", "persona_check": True},
    "content_rules": {"banned_words": ""},
}
provider2 = _FakeProvider([
    GOOD, '{"ok": false, "reason": "语气不符"}',
    GOOD2, '{"ok": true, "reason": ""}',
])
result2 = asyncio.run(DiaryGenerator(_FakeContext(provider2, personas), config2).generate())

check("使用选定人格", result2.persona_name == PERSONA_NAME, result2.persona_name)
check("人格正文进入正文提示词", PERSONA_TEXT in provider2.calls[0]["prompt"])
check("自检用独立静态提示词", provider2.calls[1]["system_prompt"] == PERSONA_CHECK_PROMPT)
check("自检消息含日记正文", GOOD in provider2.calls[1]["prompt"])
check("自检不通过触发重写", len(provider2.calls) == 4, str(len(provider2.calls)))
check("重写提示词保留原前缀",
      provider2.calls[2]["prompt"].startswith(provider2.calls[0]["prompt"]))
check("重写提示词带人格原因", "不符合人格设定（语气不符）" in provider2.calls[2]["prompt"])
check("重写后通过自检", result2.persona_issue == "" and result2.retried)

# 场景 3：两次都不符合人格 → 返回 persona_issue（由主流程拦截发布）
provider3 = _FakeProvider([
    GOOD, '{"ok": false, "reason": "人设不可能说这种话"}',
    GOOD2, '{"ok": false, "reason": "人设不可能说这种话"}',
])
result3 = asyncio.run(DiaryGenerator(_FakeContext(provider3, personas), config2).generate())
check("持续不符人格时给出原因", "人设不可能说这种话" in result3.persona_issue, result3.persona_issue)

# 场景 4：关闭自检时不产生额外调用
config4 = dict(config2)
config4["llm"] = dict(config2["llm"], persona_check=False)
provider4 = _FakeProvider([GOOD])
result4 = asyncio.run(DiaryGenerator(_FakeContext(provider4, personas), config4).generate())
check("关闭自检只调用一次", len(provider4.calls) == 1, str(len(provider4.calls)))
check("关闭自检仍使用人格", result4.persona_name == PERSONA_NAME and PERSONA_TEXT in provider4.calls[0]["prompt"])

# ---------------------------------------------------------------- 结果
failed = [c for c in checks if not c[1]]
for name, ok, detail in checks:
    print(f"{'[OK]' if ok else '[FAIL]'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
sys.exit(1 if failed else 0)
