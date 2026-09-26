---
name: booth
description: 用 booth CLI 搜索/查询 Booth.pm（VRChat 素材、同人市场）商品信息。当用户想找 VRChat 模型/衣装/配件/工具、查价格、看某商店、找 Booth 商品链接时使用。
---

# Booth.pm 搜索（booth CLI）

已安装 `booth` 命令（~/bin/booth → C:\Users\NyakoWW\Desktop\booth-cli\booth.py，零依赖 Python，git 仓库在桌面 booth-cli/）。
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

适用：用户给一张商品图（转卖图/截图）要找原商品。前提：当前 agent 具备看图能力；纯文字终端无法做视觉比对，只能走第 1、2 步给出候选。

1. **读图提词**：优先提取图内文字——商品名（英文/片假名）、"Original Avatar" 类字样、店铺水印、活动 logo，这是最强关键词；同时记下外观特征（发型发色/瞳色/服装配色/配饰/构图）供最终比对。
2. **CLI 搜索**：片假名与罗马字各搜一次并取交集，同名子串噪音大时加分类过滤：
   ```bash
   booth search "イフ" --category "3Dキャラクター" --sort popularity --limit 10 --json
   booth search "Ifu" --category "3Dキャラクター" --limit 10 --json
   ```
   两轴都出现且名字精确吻合的即为头号候选。
3. **下载商品图视觉比对**（闭环确认，防重名误判）：
   ```bash
   curl -sSL -A "Mozilla/5.0" -e "https://booth.pm/" -o out.jpg "<单品JSON images 里的URL>"
   ```
   两个已验证的坑：
   - `booth.pximg.net` **必须带 `-e https://booth.pm/`**（Referer），否则返回拒绝页；
   - 单品 JSON 里的 `original` 图 URL 可能已失效（404），把 URL 改写为
     `https://booth.pximg.net/c/300x300_a2_g5/<其余路径不变>_base_resized.jpg` 即可下到缩略图。
   下载后直接看图比对：发型发色/瞳色/服装/配饰/画面排版与原图一致才算命中。
4. **纯截图无任何文字**：只能按外观特征猜日语关键词（如 绿贝雷帽→"ベレー 緑 アバター"）多轮碰运气，成功率低，须如实告知是尽力搜索而非精确匹配。

## 注意

- `--sort`: new / popularity / liked / price_asc / price_desc；`--type`: all / digital / physical；`--adult`: exclude(默认) / include / only。
- `--category` 的 slug 必须是日语原文：`3Dキャラクター`、`3D衣装`、`3D小道具`、`3D装飾品`、`3Dテクスチャ`、`3D髪型`、`3D靴`、`VRoid` 等。
- 多页用 `--pages N`（每次间隔约 1.2s，自动限速），配合 `--limit` 控制总量。
- `shop` 命令多数店铺只能取到最新几件（列表由前端渲染）；找某店商品改用 `search "店铺名"`。
- 个别店铺（如 vrcalphabet）开 Cloudflare 盾无法抓取，工具会明确报错。
- 搜日文关键词效果最好；标题/标签是日语原文，可原样展示给用户。
