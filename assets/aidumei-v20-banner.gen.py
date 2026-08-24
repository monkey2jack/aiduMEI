# -*- coding: utf-8 -*-
"""aiduMEI v20.0 横幅生成器 —— 图是可复现的，不是一张来历不明的二进制。

跑 `python3 assets/aidumei-v20-banner.gen.py` 会**逐字节**重新生成
`assets/aidumei-v20-banner.svg`（`random.seed()` 写死，所以同一份代码永远出同一张图）。

■ 背景 —— 爱嘟品牌标准六边形场，几何与配色**照抄品牌站点，不自创**：
    HEX_COLORS = ["#1f4e79", "#525252", "#000000"]   深蓝 / 中灰 / 黑
    polygon points = "50,0 100,28.87 100,86.6 50,115.47 0,86.6 0,28.87"（viewBox 0 0 100 115.47）
    fill:none 空心 · size 15–85px 随机 · rotate ±30° · stroke-width 0.3–0.8 · opacity 0.3

■ 前景 —— 一次跨越，用品牌元素自己讲，不借外来符号：
    左侧一团**未分化**的小六边形（v19 之前：记忆躺在一个不分域的池子里）
      → 沿抛物线逐级变大、逐级成形（虚线 = 迁移路径，additive，不是断裂）
      → 右侧收束为一个**实心**的大六边形 + 两圈紫环（v20：按域成型、可治理、留痕）

■ 为什么是手写 SVG 而不是位图：
    33KB 矢量 vs 141KB 位图；任意尺寸都锐；能 diff、能 code review；
    改一行参数就能重出 —— 而位图只能"再生成一张碰运气"。
■ 为什么图上没有一个字：
    横幅是门面，文字信息交给 README 正文。图只负责一件事 —— 让人一眼看到"跨了一大步"。
"""
import random, io

W, H = 1600, 400
random.seed(2000)                      # 固定种子：同一份代码永远出同一张图（可复现）

BLUE, GRAY, INK, PURPLE = "#1f4e79", "#525252", "#000000", "#7030a0"
HEX_COLORS = [BLUE, GRAY, INK]         # 站点 HEX_COLORS 原样
HEX_PTS = "50,0 100,28.87 100,86.6 50,115.47 0,86.6 0,28.87"

def hexagon(cx, cy, size, rot, stroke, sw, op, fill="none"):
    h = size * 1.1547
    return (f'<g transform="translate({cx-size/2:.1f},{cy-h/2:.1f}) rotate({rot},{size/2:.1f},{h/2:.1f})" opacity="{op}">'
            f'<svg x="0" y="0" width="{size:.1f}" height="{h:.1f}" viewBox="0 0 100 115.47" overflow="visible">'
            f'<polygon points="{HEX_PTS}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/></svg></g>')

# ── ① 背景场：照站点规范随机铺 ──
# 站点按视口面积定数量；1600x400 折算约 90 个
N = 78
pal = []
per = N // len(HEX_COLORS)
for c in HEX_COLORS:
    pal += [c] * per
while len(pal) < N:
    pal.append(HEX_COLORS[len(pal) % len(HEX_COLORS)])
random.shuffle(pal)

bg = []
for i in range(N):
    size = 15 + random.random() * 70          # 站点 minSize=15 maxSize=85
    bg.append(hexagon(random.random()*W, random.random()*H, size,
                      random.randint(-30, 30), pal[i],
                      f"{0.3 + random.random()*0.5:.1f}", 0.3))

# ── ② 前景：跨越意象 ──
# 读法：左侧一片散碎的小六边形（v19 之前：记忆散在一个池子里）
#       → 沿一条上升弧线逐级变大、逐级成形
#       → 右侧收成一个实心的大六边形（v20：按域成型、可治理）
# 六边形本身就是品牌元素，所以「升级」不靠外来符号，靠品牌元素自己长大。
import math
arc = []
STEPS = 7
x0, y0, x1, y1 = 320.0, 292.0, 1338.0, 152.0
for k in range(STEPS):
    t = k / (STEPS - 1)
    x = x0 + (x1 - x0) * t
    y = y0 + (y1 - y0) * t - 132 * math.sin(math.pi * t) * 0.86   # 抛物线拱起 = 跨越
    size = 30 + 74 * (t ** 1.45)                                   # 越靠后越大
    op   = 0.52 + 0.44 * t
    sw   = 1.5 + 1.4 * t
    last = (k == STEPS - 1)
    arc.append(hexagon(x, y, size, 0, BLUE if not last else BLUE, f"{sw:.1f}", round(op,2),
                       fill="none" if not last else BLUE))
    if last:   # 终点加一圈光环，强调「到位了」
        arc.append(hexagon(x, y, size*1.42, 0, PURPLE, "1.4", 0.42))
        arc.append(hexagon(x, y, size*1.92, 0, PURPLE, "0.9", 0.20))

# 起点那侧：一堆**未分化**的小六边形（v19 之前 = 一个不分域的池子）
# 上一版这里用的是 8-21px、opacity 0.5 的"碎片"，实图上完全溶进背景 ——
# 故事的开头读不出来。现在加大、加深、收拢成一个看得见的团。
frag = []
random.seed(77)
for _ in range(26):
    a = random.random() * 6.2832
    r = (random.random() ** 0.62) * 118
    fx = 232 + math.cos(a) * r * 1.28
    fy = 258 + math.sin(a) * r * 0.82
    frag.append(hexagon(fx, fy, 15 + random.random()*15, random.randint(-30, 30),
                        GRAY, "1.1", round(0.55 + random.random()*0.28, 2)))

# 弧线本身（虚线 = 迁移路径，additive 不是断裂）
pts = []
for k in range(41):
    t = k/40
    px = x0 + (x1-x0)*t
    py = y0 + (y1-y0)*t - 132*math.sin(math.pi*t)*0.86
    pts.append(f"{px:.1f},{py:.1f}")
path = (f'<polyline points="{" ".join(pts)}" fill="none" stroke="{BLUE}" '
        f'stroke-width="2.0" stroke-dasharray="8 9" opacity="0.46" stroke-linecap="round"/>')

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}"
     role="img" aria-label="aiduMEI v20.0 —— 爱嘟品牌六边形场上的一次跨越：散碎的记忆沿弧线逐级成形，收束为一个成型的域">
  <rect width="{W}" height="{H}" fill="#ffffff"/>
  <g>{"".join(bg)}</g>
  {path}
  <g>{"".join(frag)}</g>
  <g>{"".join(arc)}</g>
</svg>
'''
io.open("/tmp/banner/hex.svg", "w", encoding="utf-8").write(svg)
print(f"生成 {len(svg)} bytes；背景六边形 {N} 个，前景阶梯 {STEPS} 级")
