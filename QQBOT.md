# QQ Bot 框架接入指南（CLI 侧钩子）

CLI 提供一个**稳定的 JSON 信封接口** `booth bot`：子进程调用、stdout 进出、
退出码恒为 0、永不抛栈，任何能 spawn 子进程或读管道的 bot 框架都能接入。

## 接口契约

**调用**（两种等价方式）：

```bash
booth bot '{"action":"search","params":{"query":"VRChat アバター","limit":5}}'
echo '{"action":"item","params":{"id":3368697}}' | booth bot
```

**请求格式**：

| 字段 | 说明 |
|---|---|
| `action` | `search` / `item` / `shop` / `imgsearch` / `smart` / `version` |
| `params` | 与 CLI 旗标同名的 snake_case 参数（`--or-word` → `"or_word"`，`--no-cache` → `"no_cache": true`） |
| 平铺写法 | 也接受 `{"action":"search","query":"...","limit":5}` 直接把参数平铺在顶层 |
| `context` | 1.4.0 起可选：request_id、max_requests、deadline，多个子进程共享同次查询的出站上限 |

各 action 的 params：

- `search`：`query`（字符串或数组）、`sort`、`type`、`adult`（include/exclude/only）、
  `tag[]`、`or_word[]`、`exclude[]`、`min_price`、`max_price`、`in_stock`、`vrc`、`no_vrc`、
  `category`、`event`、`lang`、`page`、`pages`、`limit`、`no_cache`。
  **默认收窄 VRChat 圈**（自动 `--tag VRChat`，`"no_vrc": true` 搜全站）
- `smart`：`query`（需求式描述，中文/日文均可）、`sort`、`adult`、`no_vrc`、`page`、
  `limit`、`no_ai`、`no_webfind`、`no_cache`。AI 需求解析读 CLI 进程环境变量
  （`VISION_API_KEY` 等，与 vrc-booth-bot 同名），缺省降级直搜；**耗时约 30-90 秒**
  （AI + 多路搜索 + 详情核实），建议超时给 120s+
- `item`：`id`（数字/字符串/URL）、`desc_len`、`full`、`no_cache`
- `shop`：`shop`（子域名或 URL）、`pages`、`no_cache`
- `imgsearch`：`image`（本地路径；URL 会先下载）、`engine`（默认 bing,ascii2d）、
  `headless`、`wait_s`、`limit`、`no_cache`。**注意慢**：浏览器备援路径可达 1-2 分钟，
  建议给足超时或只用 HTTP 快路径可用的部署环境（见 PROXY_DEPLOYMENT.md）。
  必须在进程环境配置通过检测的多模态 AI；未配置/纯文字/能力未知时在读文件或下载前返回失败信封，所有图片引擎关闭。key 不放在 params 中，详见 [AI_SETUP.md](AI_SETUP.md)。

**响应信封**（stdout，单行 JSON）：

```json
{"ok": true,  "action": "search", "data": {"total": 471, "count": 5, "items": [...]}}
{"ok": false, "action": "item",   "error": "booth item: error: the following arguments are required: id"}
```

`data` 即各命令 `--json` 的原始输出（字段说明见 README / skill/SKILL.md）。

携带 context 时，request_id 是 8–64 位 ASCII 字母/数字/下划线/短横线，max_requests 为 1–100 的整数，deadline 是不超过未来 15 分钟的有限 Unix 时间戳。相同 request_id 的子进程使用同机 SQLite 累计 used；同次查询显式增大 max_requests 可扩展上限，但不清零计数或延长原截止时间。缓存命中不增加 used。

响应顶层增加可选 request_budget 对象（used、maximum），成功和失败均可携带；data 结构保持兼容。version 返回 capabilities 与 semantic_fingerprint，无网络和凭据读取。上下文控制 CLI 出站，框架仍需对整个 AI/消息处理任务设置总超时。

## 各框架接入示例

### NoneBot2（Python）

最简方式是直接 subprocess（推荐 `booth_client.py` 式封装，参考配套项目
[vrc-booth-bot](https://github.com/wuhutakeoffyoo/vrc-booth-bot)（VRC 对口）——基于 NoneBot 的完整实现）：

```python
import json, subprocess, sys

def booth_call(req: dict) -> dict:
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "/path/to/booth.py", "bot", json.dumps(req, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    return json.loads(r.stdout)   # {"ok":..., "data":...}
```

### Koishi / Yunzai（Node.js）

```js
const { execFile } = require('child_process')
execFile('booth', ['bot', JSON.stringify({ action: 'search', params: { query: 'VRChat', limit: 5 } })],
  { timeout: 60_000, maxBuffer: 10 * 1024 * 1024 },
  (err, stdout) => {
    const envelope = JSON.parse(stdout)   // 永远是合法 JSON
    // envelope.ok === false 时用 envelope.error 提示用户
  })
```

### go-cqhttp 插件 / 其他语言

同样的模式：spawn `booth bot '<json>'` → 解析 stdout 单行 JSON → 按 `ok` 分支。
Windows 下若编码异常，用 `python -X utf8 booth.py bot ...` 调用。

## 注意事项

- **超时**：`search`/`item`/`shop` 建议 30-60s（含限流退避重试）；`imgsearch` 建议 90-240s，
  或在海外出口部署使 Bing HTTP 快路径生效（秒级返回、无浏览器）。
- **R-18**：默认 `adult=include` 联合搜索，结果带 `is_adult`；bot 侧展示时请对
  `is_adult=true` 的条目自行加提示或按群策略过滤（`"adult": "exclude"`）。
- **频率**：CLI 内置限速与磁盘缓存，bot 侧无需额外节流；重复查询走缓存瞬时返回。
- **并发**：单机建议串行调用（booth 请求间隔 ≥1s 是对站点的礼貌，也是反风控需要）。
