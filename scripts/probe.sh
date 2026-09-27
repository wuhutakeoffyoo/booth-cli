#!/bin/bash
# booth 部署探针（钩子：PROXY_DEPLOYMENT.md 待实测清单的第一步）
# 用法: 在目标服务器上 bash scripts/probe.sh [测试图片路径]
# 输出各项连通性指标，用于决定 booth 出口放在哪台服务器。
set -u
IMG="${1:-}"
echo "=== [1/3] booth.pm JSON TTFB x3 ==="
for i in 1 2 3; do
  curl -sS -o /dev/null -w "ttfb=%{time_starttransfer}s total=%{time_total}s\n" \
    --max-time 15 "https://booth.pm/ja/items/3368697.json" || echo "(失败)"
done

echo "=== [2/3] booth.pximg.net 缩略图（需 Referer）==="
curl -sS -o /dev/null -e "https://booth.pm/" \
  -w "ttfb=%{time_starttransfer}s total=%{time_total}s size=%{size_download}\n" \
  --max-time 15 \
  "https://booth.pximg.net/c/300x300_a2_g5/d398d5cc-41ab-4a3a-b7fe-eec9a3d94a3b/i/3368697/0e0c702d-150b-4532-8399-0a40e33ec90e_base_resized.jpg" \
  || echo "(失败: 国内直连预期挂掉)"

echo "=== [3/3] Bing HTTP 图搜快路径（成功=无需浏览器）==="
CLI_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -n "$IMG" ] && [ -f "$IMG" ]; then
  (cd "$CLI_DIR" && python3 -X utf8 -c "
from reverse_search import bing_search_http
import sys
try:
    ids, d, _ = bing_search_http(sys.argv[1])
    print('HTTP 快路径可用, 候选', len(ids))
except Exception as e:
    print('HTTP 快路径不可用:', str(e)[:100])
" "$IMG")
else
  echo "(跳过: 传入本地图片路径可测此项, 如 bash scripts/probe.sh /path/to/img.jpg)"
  echo "退而测 bing.com 可达性:"
  curl -sS -o /dev/null -w "ttfb=%{time_starttransfer}s total=%{time_total}s\n" \
    --max-time 15 "https://www.bing.com/images" || echo "(失败)"
fi
echo "=== 完成。判读见 PROXY_DEPLOYMENT.md ==="
