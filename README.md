# booth-cli

Booth.pm（BOOTH 同人/VRChat 素材市场）的命令行搜索工具，为 AI agent 设计。
核心以 Python 标准库实现，唯一依赖 pykakasi（假名读音变体，`smart` 使用），
已安装为 `booth` 命令（`~/bin/booth` → 本目录 `booth.py`）。

运作原理、分层兜底思路、参考的开源项目与盲测方法论，见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 特性

- **智能搜索（VRC 对口，`booth smart`）**：中文/需求式描述直接入口——AI 产出单词级
  日语关键词 + 说明文核实词，分词多路合并搜索，「适用于XX素体的服装」类需求按
  商品**说明文**（対応素体/仕様 段落）匹配置顶；知名商品回忆与网络检索（DDG/Exa）
  兜底。策略与 [vrc-booth-bot](https://github.com/wuhutakeoffyoo/vrc-booth-bot) 同源。
  AI 读环境变量 `VISION_API_KEY/VISION_BASE_URL/VISION_MODEL`（与 bot 同名，一份 .env
  两边通用），缺省自动降级为分词+读音变体直搜。
- **关键词搜索 / 商品详情 / 商店查询**：结构化 `--json` 输出，为 AI agent 调用设计。
  search/smart **默认收窄 VRChat 圈**（自动 `--tag VRChat`，`--no-vrc` 搜全站）；
  popularity 排序翻页时自动切新着并标注（Booth 站点忽略 popularity 下的 page 参数）。
- **bot 接入钩子**：`booth bot` JSON 信封接口（子进程进出、退出码恒 0、永不抛栈），
  NoneBot/Koishi/Yunzai 等框架均可直接接，见 [QQBOT.md](QQBOT.md)；
  配套 NoneBot 实现：[vrc-booth-bot](https://github.com/wuhutakeoffyoo/vrc-booth-bot)（VRC 对口）。
- **以图找品（imgsearch）**：Bing 视觉搜索**纯 HTTP 快路径优先**（协议逆向自开源库
  [kitUIN/PicImageSearch](https://github.com/kitUIN/PicImageSearch)，约 2 秒、无浏览器），
  被区域风控拒绝时自动回落 playwright 浏览器引擎（有头 + 持久 profile 过 Cloudflare），
  另有 ascii2d 备援；引擎派生词（图内文字）自动合并进关键词搜索。
- **磁盘缓存**：sqlite 实现（借鉴 [requests-cache](https://github.com/requests-cache/requests-cache)），
  商品 6 小时 / 搜索页 10 分钟，重复查询瞬时返回；`--no-cache` 强制最新。
- **限流退避**：重试遵循 `Retry-After` 响应头，无头时指数退避 + 抖动
  （借鉴 [tenacity](https://github.com/jd/tenacity)），对 Booth 限流更友好。
- **安全边界**：仅允许 `https://*.booth.pm` 官方域名（重定向逐跳校验），Bing 端点主机白名单 +
  DNS 解析私网地址阻断（防 SSRF/DNS rebinding）。
- **测试**：`python -m unittest discover -s tests`，覆盖解析器/安全边界/缓存/退避/bot 钩子，不联网。

## 为什么自己写

Booth 无官方公开 API；调研过的开源项目（boothmate / BoothPM-SDK / gallery-dl / Booth2RSS / booth-manager）
都是给人写代码用的库或 RSS/下载工具，没有现成"给 AI agent 用的搜索 CLI/MCP"，故基于其验证过的
页面结构（搜索页 `li.item-card` data 属性、单品 `/ja/items/{id}.json`）实现。

## 数据来源（非官方接口）

| 用途 | 端点 |
|---|---|
| 搜索 | `https://booth.pm/ja/search/{query}` 或 `/ja/browse/{分类}` （HTML 内嵌商品卡片） |
| 单品 | `https://booth.pm/ja/items/{id}.json` |
| 商店 | `https://{sub}.booth.pm/items` （仅少量服务端渲染，见限制） |

年龄门用 cookie `adult=t` 绕过；R-18 由 `adult` 参数控制，默认 `include` **联合搜索**（Booth 的 R-18 为一元标记，情色与怪诞/R18G 类同旗、无独立过滤），结果里每件商品带 `is_adult` 标记。

## 用法

```bash
booth help                          # 帮助

# 智能搜索（VRC 对口推荐入口：中文/需求式描述 → AI 关键词 + 说明文核实 + 分词合并）
booth smart "适用于Rexouium素体的服装" --json
booth smart "尾巴" --no-ai --json    # 跳过 AI（分词+读音变体直搜）

# 搜索（默认收窄 VRChat 圈、新着序、联合 R-18）
booth search "VRChat アバター" --limit 10 --json
booth search "衣装" --no-vrc --json  # 搜全站

# 组合过滤
booth search "衣装" --sort popularity --min-price 1000 --max-price 5000 \
    --category "3D衣装" --in-stock --exclude "VRoid" --json

booth search "" --tag VRChat --tag アバター --adult only   # 标签/成人过滤

# 商品详情（含收藏数、标签、规格、简介、图片）
booth item 5813187 --json
booth item https://booth.pm/ja/items/5813187

# 商店信息与最新商品
booth shop mukumi --json

# 以图找品（Bing 纯HTTP优先 → 浏览器备援 → ascii2d；派生词自动合并）
booth imgsearch 商品图.jpg --json
booth imgsearch "https://booth.pximg.net/..." --engine ascii2d --headless --json
```

所有命令支持 `--no-cache` 跳过磁盘缓存强制重新请求。

### search 选项

| 选项 | 说明 |
|---|---|
| `--sort` | `new`(新着) `popularity`(人气) `liked`(收藏) `price_asc` `price_desc` |
| `--type` | `all` / `digital`(下载品) / `physical`(实体) |
| `--adult` | `include`(默认，联合搜索) / `exclude`(仅全年齢) / `only`(仅R-18) |
| `--tag NAME` | 标签过滤，可多次；默认已自动收窄 VRChat（`--no-vrc` 搜全站） |
| `--or-word W` / `--exclude W` | OR 词 / 排除词，可多次 |
| `--min-price` / `--max-price` | 价格区间（日元） |
| `--in-stock` | 仅在售 |
| `--category SLUG` | 分类，需日语原文，如 `3Dキャラクター` `3D衣装` `3D小道具` `3D装飾品` `3Dテクスチャ` `3D髪型` `3D靴` `VRoid` |
| `--page N` / `--pages N` / `--limit N` | 翻页与条数（页间隔约 1.2s） |
| `--json` | 结构化 JSON 输出（AI 推荐） |

## 已知限制

- **无官方 API**，依赖页面结构，Booth 改版可能需要更新解析器。
- **imgsearch 的 Bing HTTP 快路径受区域影响**：部分网络环境下 Bing 会把视觉搜索结果页
  弹回首页（HTTP 路径拿不到结果，约 0.6 秒失败），此时自动回落 playwright 浏览器引擎；
  宽松网络环境下 HTTP 路径直接出结果且无需浏览器。
- `imgsearch` 浏览器备援依赖 playwright + 本机 Chrome；持久 profile
  `~/.booth-cli/pw_profile` 保存了通过验证的 cookie，**勿删除**（删除后首次运行需重新过验证）。
- `shop` 命令：多数商店的商品列表由前端 JS 加载，服务端只能渲染最新几件；
  找某店商品建议用 `search "店铺名"` 或已知商品 ID。
- 个别店铺（如 vrcalphabet）开了 Cloudflare 盾，无法抓取，会报错提示。
- 未登录功能（收藏/购物车/已购下载）不在此工具范围。
- 请遵守 Booth 利用条款：请求间隔 ≥1 秒，勿高并发抓取。

## 环境要求

Python 3.8+；安装依赖：`pip install -r requirements.txt`（pykakasi，假名读音变体，必装）。
Windows（Git Bash / CMD）与 Linux/macOS 均可运行。

## AI Agent 技能文档

[skill/SKILL.md](skill/SKILL.md) 是给 AI agent 用的技能说明，沉淀了三套实战验证的流程：

1. **基础用法**——搜索/详情/商店的推荐调用方式（一律 `--json`）
2. **多商品对比**——三轴关键词探测 + 缺席检查 + 五层维度对比（事实/能力/成本/信号/结论）
3. **以图找品**——读图提词 → 两轴搜索取交集 → 下载商品图视觉比对（含 pximg Referer 与缩略图 URL 改写技巧）

安装方法：把 `skill/` 目录复制为各 agent 的技能目录下的 `booth/`（如 `~/.zcode/skills/booth`、`~/.agents/skills/booth`、`~/.codex/skills/booth`）。

## 许可证

MIT — 见 [LICENSE](LICENSE)。


