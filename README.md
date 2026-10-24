# 每日一记 (astrbot_plugin_daily_diary)

让 Bot 每天用大模型写一篇日记：定时自动生成、自动保存 TXT、自动存入 AstrBot 知识库，还可以由 Bot 自己发布到 QQ 空间。

## 功能特性

- ⏰ **发布时间自由设定**：24 小时制任意时刻（如 `08:00`、`21:30`），修改保存即生效
- ✍️ **提示词自由设定**：日记的写作风格、视角、主题完全由你定义，支持 `{date}`、`{min_words}`、`{max_words}`、`{banned_words}` 占位符
- 📏 **字数范围可设定**：超出设定范围（±30% 容差）会自动重写一次
- 🚫 **禁止内容可设定**：每行一条，支持正则；命中后自动重写，仍命中则拦截发布与入库
- 🤖 **大模型可指定**：可选服务提供商 + 模型 ID；不指定则使用 AstrBot 全局默认
- 📚 **知识库自由指定**：支持多个知识库，多选即可；日记自动分块、向量化入库
- 📄 **TXT 自动存档**：保存至 `data/plugin_data/astrbot_plugin_daily_diary/diaries/`
- 🌐 **QQ 空间自动发布**：写完联动 [astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 插件自动发说说（Cookie/登录由该插件管理）
- 📢 **结果通知**：生成结果自动推送到指定会话（在会话里发一条命令即可绑定）

## 工作流程

```
定时触发（或手动 /diary）
   │
   ▼
组装提示词（模板 + 日期/字数/禁止内容硬性要求）
   │
   ▼
调用 LLM 生成日记 ──► 校验：字数范围 + 违禁内容 ──不过──► 带反馈重写一次
   │                                                    │
   ▼                                                    ▼
保存 TXT 文件                                    仍命中违禁内容？
   │                                              是 → 仅存档，终止
   ▼                                              否 → 继续 ↓
写入指定知识库（可多个）
   │
   ▼
发布到 QQ 空间（可选）
   │
   ▼
向通知会话推送结果摘要
```

## 安装

**方式一：WebUI 上传（推荐）**

1. 将本目录打包为 `astrbot_plugin_daily_diary.zip`
2. WebUI → 插件管理 → 从压缩包安装 → 上传

**方式二：手动放置**

把 `astrbot_plugin_daily_diary` 整个目录放入 `AstrBot/data/plugins/`，然后在 WebUI 插件页重载（或重启 AstrBot）。

> 无第三方依赖（`aiohttp`、`apscheduler` 均为 AstrBot 内置），无需 pip 安装。

## 配置说明

插件配置位于 WebUI → 插件管理 → 每日一记 → 配置。

### ⏰ 定时设置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 启用每日定时生成 | 开 | 关闭后仅可手动触发 |
| 每日生成时间 | `21:30` | 24 小时制 `HH:MM`，保存后即生效 |
| 定时触发所用时区 | `Asia/Shanghai` | IANA 时区名。Docker 部署的容器多为 UTC，保持默认即按北京时间触发，否则会差 8 小时 |

### 🤖 大模型设置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 服务提供商 | 空 | 下拉选择；留空使用 AstrBot 全局默认 |
| 模型 ID | 空 | 留空使用该提供商的默认模型 |
| 日记写作提示词 | 内置模板 | 见下方占位符说明 |
| 字数下限 / 上限 | 200 / 400 | 正文按去除空白后的字符数计 |

提示词占位符（均可用可不用，硬性要求会自动附在末尾）：

| 占位符 | 替换为 |
|---|---|
| `{date}` | 中文日期，如 `2026年10月24日 星期六` |
| `{min_words}` / `{max_words}` | 字数下限 / 上限 |
| `{banned_words}` | 禁止内容列表（顿号分隔，无则替换为「无」） |

### 🚫 内容审核

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 日记中禁止出现的内容 | 空 | 每行一条；可编译为正则的按正则匹配，否则按文本包含匹配 |

审核策略：命中 → 自动重写一次 → 仍命中 → 该篇**不发布、不入知识库**，仅本地存档并在通知中说明原因。

### 💾 存储设置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 存放日记的知识库 | 空 | 多选；需先在 WebUI「知识库」中创建并配置嵌入模型 |
| 保存 TXT 文件 | 开 | 存至 `data/plugin_data/astrbot_plugin_daily_diary/diaries/diary_日期.txt` |

### 🌐 QQ 空间发布

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 联动 astrbot_plugin_qzone 发布 | 关 | 开启后每次生成成功即发布 |

发布能力由 **astrbot_plugin_qzone** 插件提供，见下节。

### 📢 结果通知

| 配置项 | 默认值 | 说明 |
|---|---|---|
| 接收通知的会话 | 空 | 定时任务完成后推送结果；用 `/diary 推送` 一键绑定/解绑当前会话 |

## 指令

| 指令 | 说明 |
|---|---|
| `/diary` 或 `/每日一记` | 立即生成今天的日记并走完整流程（仅管理员） |
| `/diary 状态` | 查看当前配置摘要、实际生效的模型、下次运行时间 |
| `/diary 推送` | 将当前会话加入/移出结果通知列表 |

> 前缀 `/` 以你的唤醒前缀设置为准。生成可能需要数十秒，请耐心等待回复。

## QQ 空间发布：联动 astrbot_plugin_qzone

本插件不直接管理 QQ 空间登录，发布功能通过 [astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) 插件完成：

1. 在 AstrBot 插件市场安装 **astrbot_plugin_qzone**（QQ空间）并启用
2. 按该插件的说明完成 QQ 空间登录/Cookie 配置（配置都在它那边），并用它的 `/发说说` 命令确认能发
3. 回到本插件配置，打开「联动 astrbot_plugin_qzone 发布」开关
4. 发送 `/diary 状态`，QQ 空间一行显示「已联动 astrbot_plugin_qzone」即就绪

发布失败（Cookie 失效等）会在结果通知与日志中说明原因，更新 Cookie 请在 astrbot_plugin_qzone 的配置中操作。

## 二次开发指南

```
astrbot_plugin_daily_diary/
├── main.py             # 入口：指令注册、定时调度、流程编排（_run_diary 是主流程）
├── diary_generator.py  # 生成：提示词组装、LLM 调用、字数/违禁校验（generate 为主入口）
├── qzone_link.py       # 联动：调用 astrbot_plugin_qzone 发布（publish_diary 为主入口）
├── metadata.yaml       # 插件元数据（名称、版本、兼容版本范围）
├── _conf_schema.json   # 配置模式（WebUI 配置面板由此生成）
├── requirements.txt    # 第三方依赖（当前为空）
└── README.md
```

常见扩展点：

- **换发布渠道**（如 Telegram 频道）：在 `main.py` 的 `_run_diary` 中把 `publish_diary(...)` 换成对应平台的调用即可；参照 `qzone_link.py` 的写法可对接任意其他插件
- **调整生成策略**：`diary_generator.py` 顶部的 `WORD_TOLERANCE`（字数容差）与 `MAX_RETRY`（重试次数）
- **换存档格式**（如 Markdown）：改 `main.py` 的 `_save_txt`
- **增配置项**：改 `_conf_schema.json` 后在代码中 `self.config.get(...)` 读取；AstrBot 会自动为老配置补默认值

核心 API（AstrBot v4.29）参考：

- LLM：`provider.text_chat(prompt=..., model=...)`，`context.get_using_provider()` / `get_provider_by_id()`
- 知识库：`context.kb_manager.get_kb_by_name(name).upload_document(file_name, file_content: bytes, file_type)`
- 主动推送：`context.send_message(unified_msg_origin, MessageChain(chain=[Plain(text)]))`

## 常见问题

**Q：生成失败提示「未找到服务提供商」/「尚未配置任何大模型服务提供商」？**
检查插件配置的提供商 ID 是否正确，或在 AstrBot 全局设置中配置默认提供商。

**Q：知识库写入失败？**
确认知识库已在 WebUI「知识库」中创建，且已选择可用的嵌入模型（Embedding Provider）；用 `/diary 状态` 可查看当前可用的知识库列表。

**Q：QQ 空间发布失败「未找到已启用的 astrbot_plugin_qzone」？**
安装并启用 astrbot_plugin_qzone 插件，且完成其登录配置；用 `/diary 状态` 检查联动是否就绪。

**Q：QQ 空间发布失败提示 Cookie 相关？**
登录态由 astrbot_plugin_qzone 管理，请到该插件的配置中更新 Cookie/重新登录，与本插件无关。

**Q：字数总是不达标？**
字数统计为去除空白后的字符数；模型对字数控制有限，插件在超出 ±30% 容差时会重写一次，仍偏差则接受并在通知中标注。可适当放大设定范围或更换字数控制更好的模型。

**Q：定时到点没有触发？**
用 `/diary 状态` 查看下次运行时间；进程重启后若错过触发点，1 小时内会自动补跑。手动验证可用 `/diary`。

## 兼容性

- AstrBot `>=4.29.0, <5`（API 基于 v4.29.1 源码核对）
- Python 3.10+
- 无额外第三方依赖
