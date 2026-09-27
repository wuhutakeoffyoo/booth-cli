---
name: booth
description: 用 booth CLI 搜索/查询 Booth.pm（VRChat 素材、同人市场）商品信息。当用户想找 VRChat 模型/衣装/配件/工具、查价格、看某商店、找 Booth 商品链接时使用。
---

# Booth.pm 搜索（booth CLI）

已安装 `booth` 命令（~/bin/booth → D:\boothcli\booth.py，零依赖 Python，git 仓库在 D:\boothcli）。
**作为 AI 调用时一律加 `--json`** 获取结构化输出。输出编码已处理，可直接在 Windows Git Bash 运行。

## 常用调用

```bash
# 搜索（默认按新着、排除 R-18）。返回 total/count/items[{id,name,price,url,shop,category,tags,is_adult,image}]
booth search "VRChat アバター" --vrc --limit 10 --json

# 人气排序 + 价格区间 + 分类 + 仅在售
booth search "衣装" --sort popularity --min-price 1000 --max-price 5000 \
    --category "3D衣装" --in-stock --json

# 标签（可多次）/ 排除词 / OR 词
booth search "" --tag VRChat --tag アバター --exclude VRoid --json

# 商品详情：收藏数、标签、规格变体、简介（纯文本）、图片直链
booth item 5813187 --json        # 也可传完整 URL

# 商店信息 + 服务端渲染的最新商品
booth shop mukumi --json
```

**缓存**：内置 sqlite 磁盘缓存（`~/.booth-cli/cache.sqlite3`；商品 6 小时、搜索/商店页 10 分钟），
重复查询瞬时返回。需要强制最新（刚上架、价格变动核对）时加 `--no-cache`。

## 多商品对比流程（"帮我对比 / 帮我选 / 哪个好"类需求）

1. **关键词探测**（漏检的主因是只搜用途动词——简介极短的商品只能被标题词命中，所以**物品名词轴必须查**）：
   把需求翻译成日语后，按三条轴构造查询：
   - 用途动词轴：録画 / 撮影 / 動画
   - 物品名词轴：カメラ / ギミック / カメラギミック / プレハブ / ツール / システム
   - 平台轴：VRChat / VRC（商品名里的缩写）

   优先用 `--or-word` 把同义名词合并进主查询、一次覆盖多条轴（实测语义：主词 AND (or1 OR or2 …)）：
   ```bash
   booth search "360度" --or-word カメラ --or-word ギミック --or-word 録画 --sort popularity --limit 10 --json
   ```
   再补 1~2 组动词轴查询；`--limit` 至少 10。用户点名的商品名要原样再搜一次。
   候选集定稿前做**缺席检查**：把各轴命中的 id 取并集去重，人气商品（收藏数异常高的）必须核对它出现在哪条轴，若只在单轴出现则再补搜它标题里的关键词，确认没有同类漏网。
2. **补齐数据**：对每个候选执行 `booth item <id> --json`（每次间隔 ≥1.2s），
   取价格 / 收藏(wish_lists_count) / 上架时间(published_at) / 分类 / 标签 / 规格 / 简介。
3. **分层对比**（固定五层，表格维度从这五层取）：
   - **事实层**（CLI 直接可得）：价格、收藏数（=热度）、上架时间（新旧≈维护活跃度）、分类、店铺
   - **能力层**（从简介/标题提炼）：能做什么（如 360°全景 / 立体 / 普通视频）、输出与回放方式、附加功能（好友可用、自动触发等）
   - **成本层**：价格 + 依赖要求（MA、OBS、分辨率等，简介里找）+ 上手难度
   - **信号层**：收藏数、VRChat 官方标签、**简介完整度**（简介过短=说明在图片/视频里，对比表中标注"信息缺口"，提醒用户买前确认）
   - **结论层**：按使用场景（预算/需求侧重）给推荐排序，不给单点"最好"
4. **输出**：markdown 对比表（对口商品一组，功能不相干的收录为对照组一行带过）+ 场景化结论。
5. **已知坑**：`past_purchase_count` 恒为 null（仅店主可见），不要用作销量维度；
   软件类商品价格可能带 "~"（按规格浮动），照原样展示。

## 以图找品（拿到商品图反查 Booth 商品）

**首选 `booth imgsearch`**（Bing 视觉搜索 + 派生词关键词自动合并）。
引擎架构：**纯 HTTP 快路径优先**（无浏览器、约 2 秒；协议借鉴 kitUIN/PicImageSearch），
被 Bing 区域风控拒绝时**自动回落 playwright 浏览器引擎**（弹浏览器窗口属正常现象，
依赖本机 Chrome 与 `~/.booth-cli/pw_profile` 持久 profile，**勿删除该 profile**）；
`--engine ascii2d` 可追加 ascii2d 备援；`--headless` 让浏览器备援无头运行（默认有头）。

```bash
booth imgsearch 图片.jpg --json                                    # 本地图片
booth imgsearch "https://booth.pximg.net/..." --json               # booth 官方图床 URL 也可以
booth imgsearch 图片.jpg --engine ascii2d --json                   # 指定备援引擎
```

返回 `derived_query`（Bing 从图里读出的文字，常就是商品名）与 matches（视觉候选前 2 + 派生词关键词命中）。
实测：有名字的图 Top1 率极高；无字图也能救回一部分（水中ソックス、P♡P.Hair 04 均为 imgsearch Top1）。

以下手动流程在 imgsearch 无果或候选可疑时使用（前提：当前 agent 具备看图能力；纯文字终端无法做视觉比对，只能走第 1、2 步给出候选）。

1. **读图提词**：优先提取图内文字——商品名（英文/片假名）、"Original Avatar" 类字样、店铺水印、活动 logo，这是最强关键词；同时记下外观特征（发型发色/瞳色/服装配色/配饰/构图）供最终比对。
2. **CLI 搜索**：片假名与罗马字各搜一次并取交集，同名子串噪音大时加分类过滤：
   ```bash
   booth search "イフ" --category "3Dキャラクター" --sort popularity --limit 10 --json
   booth search "Ifu" --category "3Dキャラクター" --limit 10 --json
   ```
   两轴都出现且名字精确吻合的即为头号候选。
   **名字搜索全落空时的机械重试清单**（实测救回 Wing Sync / Octalux Array，仍败于 P♡P.Hair 的教训）：
   - 分词变体：连写↔拆开（OctaluxArray ↔ Octalux Array）
   - 近似拼写：图内文字差一个字母很常见（SYNTH↔Sync、Bellta↔Belita）
   - 记号剥离：♡☆等符号会切断匹配（P♡P.Hair 搜不到 → 换店名/系列名/去符号），也可搜图中作者名
   - 图文语言切换：图是英文标题常是日语（Bob short hair → かわいいボブヘア），反之亦然——双语轴对每个名字都必须执行
3. **下载商品图视觉比对**（闭环确认，防重名误判）：
   ```bash
   curl -sSL -A "Mozilla/5.0" -e "https://booth.pm/" -o out.jpg "<单品JSON images 里的URL>"
   ```
   两个已验证的坑：
   - `booth.pximg.net` **必须带 `-e https://booth.pm/`**（Referer），否则返回拒绝页；
   - 单品 JSON 里的 `original` 图 URL 可能已失效（404），把 URL 改写为
     `https://booth.pximg.net/c/300x300_a2_g5/<其余路径不变>_base_resized.jpg` 即可下到缩略图。
   下载后直接看图比对：发型发色/瞳色/服装/配饰/画面排版与原图一致才算命中。
4. **图内无商品名时，先判定"卖的是什么"再选词**（实测三大误判源：发型演示图被当成 avatar 立绘、punk 风被当成面包主题、名字读错一个字母）：
   - **同角色多角度/头部特写** → 大概率是 **ヘア/髪型** 商品：搜 ヘア + 特征（ツインテール/お団子/三つ編み/ボサボサ等），英文命名也常见（如 Twin Bun Braids）
   - **人物身上带特殊装备/特效** → 商品可能是那个物件：补配件轴（着せ替え / ギミック / テクスチャ / 素材 + ソックス/ブーツ/帽子等图内部品词）
   - **画面强调透明/水体/发光等渲染质感** → 可能是 shader 演示，直接搜 lilToon 常能命中
   - **细节即线索**：安全钉=パンク、ツッカケ=??——把图内物件风格词加进查询（例：パンク 猫耳 キャスケット），并且**扫描结果全列表**，别因为预设主题排除正确答案
   - **名字搜索全落空时**：试拼写变体（差一个字母：Bellta↔Belita）、全角/半角、平/片假名互换
   - shader 角标（lilToon 等）只是依赖提示≠商品名
   - 实在无线索才纯外观猜词，如实告知是尽力搜索
5. **判定纪律**：候选须"名字精确吻合"或"下图比对一致"才算命中；假名/罗马字两轴取交集防表记差异；时间盒（无字图最多 4~5 轮查询）后如实标记未命中，不硬凑。

## 注意

- `--sort`: new / popularity / liked / price_asc / price_desc；`--type`: all / digital / physical；`--adult`: **include(默认，联合搜索)** / exclude(仅全年齢) / only(仅R-18)。
- **R-18 说明**：Booth 的 R-18 是一元标记（情色与怪诞/R18G 类同旗，平台无独立 R18G 过滤），默认联合搜索，结果每件带 `is_adult`；展示给用户时对 adult 商品标注 R-18；要收窄怪诞向只能靠自由标签（如 `--tag グロ`，覆盖不全）。
- `--category` 的 slug 必须是日语原文：`3Dキャラクター`、`3D衣装`、`3D小道具`、`3D装飾品`、`3Dテクスチャ`、`3D髪型`、`3D靴`、`VRoid` 等。
- 多页用 `--pages N`（每次间隔约 1.2s，自动限速），配合 `--limit` 控制总量。
- `shop` 命令多数店铺只能取到最新几件（列表由前端渲染）；找某店商品改用 `search "店铺名"`。
- 个别店铺（如 vrcalphabet）开 Cloudflare 盾无法抓取，工具会明确报错。
- 搜日文关键词效果最好；标题/标签是日语原文，可原样展示给用户。
