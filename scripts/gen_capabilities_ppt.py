"""生成 biscuitbot「能力地图」PPTX 演示文稿。

布局对标 output/index.html（灯塔AI 技能地图）的视觉风格：
  深色金色调英雄页 + 类别概览 + 分布条形图 + 能力目录分页。

用法：python scripts/gen_capabilities_ppt.py
输出：output/biscuitbot-capabilities-map.pptx
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.util import Emu, Inches, Pt, Pt as Pt_

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "biscuitbot-capabilities-map.pptx"

# 配色（对标 HTML 的 ivory / gold / dark 风格）
INK = RGBColor(0x17, 0x17, 0x14)          # 主文字
MUTED = RGBColor(0x6F, 0x6C, 0x63)        # 次要文字
IVORY = RGBColor(0xF6, 0xF2, 0xE8)        # 背景
PAPER = RGBColor(0xFF, 0xFD, 0xF7)        # 卡片背景
GOLD = RGBColor(0xB4, 0x93, 0x58)         # 主金
GOLD2 = RGBColor(0xD6, 0xBF, 0x8B)        # 次金
LINE = RGBColor(0xDE, 0xD7, 0xC6)         # 分隔线
DARK = RGBColor(0x24, 0x23, 0x1F)         # 深色背景
DARKER = RGBColor(0x14, 0x14, 0x0E)       # 更深背景
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
GREEN = RGBColor(0x48, 0x68, 0x5B)

# runtime → 展示名
RUNTIME_LABEL = {"prompt": "技能", "process": "应用", "mcp": "MCP"}

# category → 中文名 / 图标 / 短描述
CATEGORY_META: dict[str, tuple[str, str, str]] = {
    "ai": ("人工智能", "🤖", "通用 AI 能力：问答、分析、内容生成。"),
    "web": ("网络", "🌐", "HTTP、抓取、API 对接等联网处理。"),
    "video": ("视频", "🎬", "视频生成、剪辑、处理。"),
    "image": ("图像", "🖼️", "图像生成、分析与处理。"),
    "audio": ("音频", "🎧", "音频转写、合成与处理。"),
    "graphics": ("图形", "🎨", "渲染、可视化与图形能力。"),
    "office": ("办公", "📄", "文档、表格、演示文稿等办公场景。"),
    "docs": ("文档", "📝", "Markdown / 文本 / 知识文档处理。"),
    "design": ("设计", "🎨", "设计规范、视觉与品牌。"),
    "devops": ("DevOps", "🛠️", "构建、发布、环境与运维。"),
    "database": ("数据库", "🗄️", "数据存储、查询与建模。"),
    "automation": ("自动化", "⚙️", "流程自动化、任务编排。"),
    "search": ("搜索", "🔍", "网页/知识检索与落地。"),
    "knowledge": ("知识", "📚", "知识库、检索与问答。"),
    "3d": ("3D", "🧊", "三维建模、渲染与生成。"),
    "gamedev": ("游戏开发", "🎮", "游戏资产、剧情与开发。"),
    "game": ("游戏", "🎮", "游戏相关能力。"),
    "communication": ("沟通", "💬", "消息、协同与沟通场景。"),
    "browser": ("浏览器", "🧭", "浏览器自动化与页面理解。"),
    "debugging": ("调试", "🐞", "排障、日志与错误诊断。"),
    "testing": ("测试", "🧪", "测试执行与质量保障。"),
    "science": ("科学", "🔬", "科研、计算与数据分析。"),
    "finance": ("金融", "💰", "财务、金融分析与处理。"),
    "osint": ("开源情报", "🕵️", "公开来源情报与信息收集。"),
    "music": ("音乐", "🎵", "音乐生成与处理。"),
    "streaming": ("流媒体", "📡", "流媒体推流与采集。"),
    "generation": ("生成", "✨", "内容与资产生成。"),
    "network": ("网络", "🌐", "网络通信与连接。"),
    "diagrams": ("图表", "📈", "流程图、架构图等可视化。"),
    "storage": ("存储", "💾", "文件与数据存储。"),
    "code": ("代码", "💻", "代码生成与工程能力。"),
    "api": ("接口", "🔌", "API 对接与集成。"),
    "project-management": ("项目管理", "📌", "任务、规划与协作管理。"),
    "skill": ("技能", "🧩", "本项目自带的技能。"),
    "mcp": ("MCP", "🔗", "外部 MCP 工具集。"),
    "uncategorized": ("未分类", "🏷️", "尚未归类的能力。"),
}

# 每页能力卡片数量（3 列 × 4 行 = 12）
PER_PAGE = 12
# 每页类别卡片数量（4 列 × 3 行 = 12）
CAT_PER_PAGE = 12


def _humanize_category(category: str) -> tuple[str, str, str]:
    if category in CATEGORY_META:
        return CATEGORY_META[category]
    return category, "🏷️", f"属于「{category}」类别的能力集合。"


def _add_bg(slide, color):
    """铺满幻灯片背景色。"""
    left = top = 0
    width = slide.prst_width if hasattr(slide, "prst_width") else Inches(13.333)
    height = slide.prst_height if hasattr(slide, "prst_height") else Inches(7.5)
    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = color
    bg.line.fill.background()
    bg.shadow.inherit = False
    # 移到最底层
    spTree = bg._element.getparent()
    spTree.remove(bg._element)
    spTree.insert(2, bg._element)
    return bg


def _add_text(slide, text, left, top, width, height, *, font_size=18,
              color=INK, bold=False, align=PP_ALIGN.LEFT, font_name="Microsoft YaHei",
              anchor=MSO_ANCHOR.TOP):
    """添加文本框并返回 shape。"""
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.name = font_name
    run.font.size = Pt(font_size)
    run.font.bold = bold
    run.font.color.rgb = color
    return box


def _add_rect(slide, left, top, width, height, fill_color, line_color=None):
    """添加矩形（卡片背景）。"""
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill_color
    if line_color is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line_color
        shp.line.width = Pt(0.75)
    shp.shadow.inherit = False
    return shp


def _add_rounded(slide, left, top, width, height, fill_color, line_color=None):
    """添加圆角矩形。"""
    shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height)
    shp.fill.solid()
    shp.fill.fore_color.rgb = fill_color
    if line_color is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line_color
        shp.line.width = Pt(0.75)
    shp.shadow.inherit = False
    # 调整圆角半径
    try:
        shp.adjustments[0] = 0.08
    except Exception:
        pass
    return shp


# ---- 各页布局 -----------------------------------------------------------

def build_hero_slide(prs, caps, categories, runtimes):
    """英雄页：深色金色调，展示统计数字。"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    _add_bg(slide, DARKER)

    # 装饰圆环（仿 HTML 的 .hero:before）
    circle = slide.shapes.add_shape(MSO_SHAPE.OVAL,
        Inches(10.0), Inches(-1.5), Inches(5.5), Inches(5.5))
    circle.fill.background()
    circle.line.color.rgb = RGBColor(0xD6, 0xBF, 0x8B)
    circle.line.width = Pt(1.2)
    circle.shadow.inherit = False

    # kicker
    _add_text(slide, "BISCUITBOT · CAPABILITY MAP",
        Inches(0.7), Inches(0.55), Inches(8), Inches(0.3),
        font_size=12, color=GOLD2, bold=True)

    # 主标题
    _add_text(slide, f"biscuitbot {len(caps)}+ 能力地图",
        Inches(0.7), Inches(1.0), Inches(11), Inches(1.2),
        font_size=48, color=WHITE, bold=True)

    # 副标题
    _add_text(slide,
        "这是一张可搜索、可筛选的能力全景图。它不仅告诉你「有哪些能力」，"
        "还回答每一类能力属于什么执行方式（技能 / 应用 / MCP）、来源如何，"
        "以及在运行时如何找到并调用。",
        Inches(0.7), Inches(2.25), Inches(9.5), Inches(1.2),
        font_size=15, color=RGBColor(0xDE, 0xDB, 0xD2))

    # 提示
    note_box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
        Inches(0.7), Inches(3.55), Inches(9.3), Inches(0.7))
    note_box.fill.solid()
    note_box.fill.fore_color.rgb = DARKER
    note_box.line.fill.background()
    note_box.shadow.inherit = False
    # 左侧金条
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
        Inches(0.7), Inches(3.55), Inches(0.06), Inches(0.7))
    bar.fill.solid()
    bar.fill.fore_color.rgb = GOLD2
    bar.line.fill.background()
    bar.shadow.inherit = False
    _add_text(slide,
        "数据由 CapabilityRegistry 读时聚合：技能、CLI 应用、MCP 预设。"
        "为避免全部说明同时占用上下文，本图用于「列表可见、按需显式调用」。",
        Inches(0.9), Inches(3.65), Inches(9.0), Inches(0.5),
        font_size=11, color=RGBColor(0xC9, 0xC4, 0xB8))

    # 统计卡片
    unavailable = sum(1 for c in caps if str(c.get("status")) != "available")
    stats = [
        (f"{len(caps):,}", "已注册能力"),
        (f"{len(categories):,}", "能力类别"),
        (f"{len(runtimes):,}", "执行方式"),
        (f"{unavailable:,}", "缺失 / 不可用"),
    ]
    stat_w = Inches(2.4)
    stat_h = Inches(1.2)
    stat_y = Inches(4.65)
    for i, (num, label) in enumerate(stats):
        x = Inches(0.7 + i * 2.55)
        # 顶部金线
        top_line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
            x, stat_y, stat_w, Inches(0.04))
        top_line.fill.solid()
        top_line.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        top_line.line.fill.background()
        top_line.shadow.inherit = False
        _add_text(slide, num, x, stat_y + Inches(0.12), stat_w, Inches(0.5),
            font_size=30, color=WHITE, bold=True)
        _add_text(slide, label, x, stat_y + Inches(0.72), stat_w, Inches(0.3),
            font_size=12, color=RGBColor(0xBB, 0xB7, 0xAE))

    # 底部信息
    _add_text(slide,
        f"生成时间：{datetime.now().strftime('%Y年%m月%d日 %H:%M')}  ·  "
        f"biscuitbot 能力地图  ·  可运行 python scripts/gen_capabilities_map.py 重新生成",
        Inches(0.7), Inches(6.85), Inches(12), Inches(0.3),
        font_size=10, color=RGBColor(0x9A, 0x95, 0x89))


def build_categories_overview_slide(prs, categories, page_idx, total_pages):
    """类别概览页：4 列网格的类别卡片。"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_bg(slide, IVORY)

    # 页眉
    _add_text(slide, "CAPABILITY ARCHITECTURE",
        Inches(0.7), Inches(0.4), Inches(8), Inches(0.3),
        font_size=11, color=GOLD, bold=True)
    _add_text(slide, "你拥有的能力类别",
        Inches(0.7), Inches(0.7), Inches(8), Inches(0.6),
        font_size=28, color=INK, bold=True)
    if total_pages > 1:
        _add_text(slide, f"第 {page_idx + 1} / {total_pages} 页",
            Inches(11.0), Inches(0.85), Inches(2.0), Inches(0.4),
            font_size=12, color=MUTED, align=PP_ALIGN.RIGHT)

    start = page_idx * CAT_PER_PAGE
    items = categories[start:start + CAT_PER_PAGE]
    # 4 列 × 3 行
    cols, rows = 4, 3
    margin_x = Inches(0.7)
    margin_y = Inches(1.55)
    grid_w = Inches(12.0)
    gap = Inches(0.18)
    card_w = Inches((12.0 - 0.18 * 3) / 4)
    card_h = Inches(1.75)
    for i, cat in enumerate(items):
        r, c = divmod(i, cols)
        x = margin_x + (card_w + gap) * c
        y = margin_y + (card_h + gap) * r
        # 卡片背景
        _add_rounded(slide, x, y, card_w, card_h, PAPER, LINE)
        # 图标 + 计数
        _add_text(slide, cat["icon"],
            x + Inches(0.2), y + Inches(0.18), Inches(0.5), Inches(0.4),
            font_size=20, color=INK)
        # 计数 chip
        chip = _add_rounded(slide,
            x + card_w - Inches(1.05), y + Inches(0.22),
            Inches(0.85), Inches(0.32),
            RGBColor(0xED, 0xE5, 0xD4))
        _add_text(slide, f"{cat['count']} 个",
            x + card_w - Inches(1.0), y + Inches(0.24),
            Inches(0.8), Inches(0.28),
            font_size=10, color=RGBColor(0x4E, 0x4B, 0x43),
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        # 标题
        _add_text(slide, cat["name"],
            x + Inches(0.2), y + Inches(0.6), card_w - Inches(0.4), Inches(0.35),
            font_size=15, color=INK, bold=True)
        # 描述
        _add_text(slide, cat["description"],
            x + Inches(0.2), y + Inches(0.95), card_w - Inches(0.4), Inches(0.7),
            font_size=10, color=MUTED)


def build_distribution_slide(prs, categories, runtimes):
    """分布页：条形图 + 执行方式词云。"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_bg(slide, IVORY)

    _add_text(slide, "DISTRIBUTION",
        Inches(0.7), Inches(0.4), Inches(8), Inches(0.3),
        font_size=11, color=GOLD, bold=True)
    _add_text(slide, "能力分布与执行方式",
        Inches(0.7), Inches(0.7), Inches(10), Inches(0.6),
        font_size=28, color=INK, bold=True)

    # 左侧：条形图
    panel_x = Inches(0.7)
    panel_y = Inches(1.6)
    panel_w = Inches(7.5)
    panel_h = Inches(5.5)
    _add_rounded(slide, panel_x, panel_y, panel_w, panel_h, PAPER, LINE)
    _add_text(slide, "能力分布（按数量排序）",
        panel_x + Inches(0.3), panel_y + Inches(0.25),
        Inches(6), Inches(0.4),
        font_size=15, color=INK, bold=True)

    top_cats = sorted(categories, key=lambda c: c["count"], reverse=True)[:12]
    max_count = top_cats[0]["count"] if top_cats else 1
    bar_y_start = panel_y + Inches(0.85)
    bar_h = Inches(0.35)
    bar_gap = Inches(0.1)
    for i, cat in enumerate(top_cats):
        y = bar_y_start + (bar_h + bar_gap) * i
        # 类别名
        _add_text(slide, cat["name"],
            panel_x + Inches(0.3), y, Inches(1.4), bar_h,
            font_size=11, color=INK, anchor=MSO_ANCHOR.MIDDLE)
        # 条形图轨道
        track_x = panel_x + Inches(1.8)
        track_w = Inches(4.5)
        _add_rounded(slide, track_x, y + Inches(0.07),
            track_w, Inches(0.22), RGBColor(0xEE, 0xE8, 0xDC))
        # 填充
        fill_w = Inches(4.5 * max(0.02, cat["count"] / max_count))
        _add_rounded(slide, track_x, y + Inches(0.07),
            fill_w, Inches(0.22), GOLD)
        # 数值
        _add_text(slide, str(cat["count"]),
            track_x + track_w + Inches(0.15), y,
            Inches(0.6), bar_h,
            font_size=11, color=INK, bold=True, anchor=MSO_ANCHOR.MIDDLE)

    # 右侧：执行方式
    rp_x = Inches(8.5)
    rp_y = Inches(1.6)
    rp_w = Inches(4.2)
    rp_h = Inches(5.5)
    _add_rounded(slide, rp_x, rp_y, rp_w, rp_h, PAPER, LINE)
    _add_text(slide, "执行方式（能力如何被调用）",
        rp_x + Inches(0.3), rp_y + Inches(0.25),
        Inches(4), Inches(0.4),
        font_size=15, color=INK, bold=True)

    # 大圆形 + 标签
    total = sum(r["count"] for r in runtimes) or 1
    cy = rp_y + Inches(2.2)
    for i, rt in enumerate(runtimes):
        # chip
        cx = rp_x + Inches(0.4 + i * 1.25)
        _add_rounded(slide, cx, cy, Inches(1.0), Inches(1.0),
            GOLD if i == 0 else (GOLD2 if i == 1 else GREEN))
        _add_text(slide, str(rt["count"]),
            cx, cy + Inches(0.2), Inches(1.0), Inches(0.5),
            font_size=26, color=WHITE, bold=True,
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        _add_text(slide, rt["name"],
            cx, cy + Inches(0.65), Inches(1.0), Inches(0.3),
            font_size=11, color=WHITE,
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
        # 百分比
        pct = rt["count"] / total * 100
        _add_text(slide, f"{pct:.1f}%",
            cx, cy + Inches(1.1), Inches(1.0), Inches(0.3),
            font_size=10, color=MUTED,
            align=PP_ALIGN.CENTER)


def build_catalog_slide(prs, caps, page_idx, total_pages, per_page=PER_PAGE):
    """能力目录分页：3 列 × 4 行卡片。"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_bg(slide, IVORY)

    # 页眉
    _add_text(slide, "SEARCHABLE CATALOG",
        Inches(0.7), Inches(0.4), Inches(8), Inches(0.3),
        font_size=11, color=GOLD, bold=True)
    _add_text(slide, "全部能力目录",
        Inches(0.7), Inches(0.7), Inches(8), Inches(0.6),
        font_size=28, color=INK, bold=True)
    _add_text(slide, f"第 {page_idx + 1} / {total_pages} 页  ·  每页 {per_page} 条",
        Inches(9.0), Inches(0.85), Inches(3.6), Inches(0.4),
        font_size=12, color=MUTED, align=PP_ALIGN.RIGHT)

    start = page_idx * per_page
    items = caps[start:start + per_page]
    cols, rows = 3, 4
    margin_x = Inches(0.7)
    margin_y = Inches(1.55)
    gap_x = Inches(0.2)
    gap_y = Inches(0.18)
    card_w = Inches((12.0 - 0.2 * 2) / 3)
    card_h = Inches(1.35)

    for i, cap in enumerate(items):
        r, c = divmod(i, cols)
        x = margin_x + (card_w + gap_x) * c
        y = margin_y + (card_h + gap_y) * r

        name = str(cap.get("display_name") or cap.get("name") or "")
        desc = str(cap.get("description") or "").strip()
        cat = str(cap.get("category") or "uncategorized")
        cat_name, cat_icon, _ = _humanize_category(cat)
        runtime = str(cap.get("runtime") or "prompt")
        runtime_label = RUNTIME_LABEL.get(runtime, runtime)
        source = str(cap.get("source") or "")
        source_map = {
            "builtin": "内置", "workspace": "工作区",
            "harness": "默认", "preset": "预设",
            "mcp-preset": "预设", "cli-anything": "外部应用",
            "generated": "生成",
        }
        source_label = source_map.get(source, source or "—")

        # 卡片背景
        _add_rect(slide, x, y, card_w, card_h, PAPER, LINE)

        # badges
        _add_rounded(slide, x + Inches(0.15), y + Inches(0.15),
            Inches(0.9), Inches(0.25), RGBColor(0xEE, 0xE7, 0xD8))
        _add_text(slide, cat_name[:4],
            x + Inches(0.15), y + Inches(0.16),
            Inches(0.9), Inches(0.25),
            font_size=9, color=RGBColor(0x5D, 0x52, 0x3E),
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

        _add_rounded(slide, x + Inches(1.1), y + Inches(0.15),
            Inches(0.7), Inches(0.25), RGBColor(0xE8, 0xEE, 0xEA))
        _add_text(slide, runtime_label,
            x + Inches(1.1), y + Inches(0.16),
            Inches(0.7), Inches(0.25),
            font_size=9, color=RGBColor(0x3F, 0x5E, 0x52),
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

        # 标题
        _add_text(slide, name,
            x + Inches(0.15), y + Inches(0.45),
            card_w - Inches(0.3), Inches(0.3),
            font_size=13, color=INK, bold=True)

        # 描述（截断）
        desc_short = desc[:55] + ("…" if len(desc) > 55 else "")
        _add_text(slide, desc_short,
            x + Inches(0.15), y + Inches(0.75),
            card_w - Inches(0.3), Inches(0.35),
            font_size=9, color=MUTED)

        # 调用方式
        if runtime == "prompt":
            invoke = f"${name}"
        elif runtime == "process":
            invoke = f"应用·{name}"
        else:
            invoke = f"MCP·{name}"
        inv_box = _add_rounded(slide,
            x + Inches(0.15), y + Inches(1.05),
            card_w - Inches(0.3), Inches(0.22),
            RGBColor(0x29, 0x28, 0x21))
        _add_text(slide, invoke,
            x + Inches(0.2), y + Inches(1.06),
            card_w - Inches(0.4), Inches(0.22),
            font_size=8, color=RGBColor(0xEE, 0xE9, 0xDF),
            font_name="Consolas", anchor=MSO_ANCHOR.MIDDLE)


def build_category_detail_slide(prs, caps, cat_id, cat_meta, page_idx, total_pages):
    """单类别详情页：列出该类别下所有能力。"""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_bg(slide, IVORY)

    cat_name, cat_icon, cat_desc = cat_meta
    items = [c for c in caps if str(c.get("category") or "uncategorized") == cat_id]

    _add_text(slide, cat_icon + "  " + cat_name.upper(),
        Inches(0.7), Inches(0.4), Inches(10), Inches(0.4),
        font_size=11, color=GOLD, bold=True)
    _add_text(slide, f"{cat_name}（共 {len(items)} 个能力）",
        Inches(0.7), Inches(0.75), Inches(10), Inches(0.6),
        font_size=28, color=INK, bold=True)
    _add_text(slide, cat_desc,
        Inches(0.7), Inches(1.35), Inches(11), Inches(0.4),
        font_size=13, color=MUTED)
    _add_text(slide, f"第 {page_idx + 1} / {total_pages} 页",
        Inches(10.5), Inches(0.85), Inches(2.1), Inches(0.4),
        font_size=12, color=MUTED, align=PP_ALIGN.RIGHT)

    # 列表（每页最多 12 条，3 列 × 4 行）
    per_page = 12
    start = page_idx * per_page
    page_items = items[start:start + per_page]

    cols, rows = 3, 4
    margin_x = Inches(0.7)
    margin_y = Inches(1.9)
    gap_x = Inches(0.2)
    gap_y = Inches(0.18)
    card_w = Inches((12.0 - 0.2 * 2) / 3)
    card_h = Inches(1.25)

    for i, cap in enumerate(page_items):
        r, c = divmod(i, cols)
        x = margin_x + (card_w + gap_x) * c
        y = margin_y + (card_h + gap_y) * r

        name = str(cap.get("display_name") or cap.get("name") or "")
        desc = str(cap.get("description") or "").strip()
        runtime = str(cap.get("runtime") or "prompt")
        runtime_label = RUNTIME_LABEL.get(runtime, runtime)

        _add_rect(slide, x, y, card_w, card_h, PAPER, LINE)

        # runtime chip
        _add_rounded(slide, x + Inches(0.15), y + Inches(0.15),
            Inches(0.7), Inches(0.22), RGBColor(0xE8, 0xEE, 0xEA))
        _add_text(slide, runtime_label,
            x + Inches(0.15), y + Inches(0.16),
            Inches(0.7), Inches(0.22),
            font_size=9, color=RGBColor(0x3F, 0x5E, 0x52),
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

        _add_text(slide, name,
            x + Inches(0.15), y + Inches(0.42),
            card_w - Inches(0.3), Inches(0.3),
            font_size=13, color=INK, bold=True)

        desc_short = desc[:60] + ("…" if len(desc) > 60 else "")
        _add_text(slide, desc_short,
            x + Inches(0.15), y + Inches(0.72),
            card_w - Inches(0.3), Inches(0.4),
            font_size=9, color=MUTED)


def main() -> None:
    from biscuitbot.capabilities.registry import CapabilityRegistry
    from biscuitbot.config.loader import load_config

    cfg = load_config()
    caps = CapabilityRegistry(cfg.workspace_path).list()

    # 构建统一格式
    skills_data = []
    for cap in caps:
        name = str(cap.get("display_name") or cap.get("name") or "")
        cat = str(cap.get("category") or "uncategorized")
        runtime = str(cap.get("runtime") or "prompt")
        skills_data.append({
            "name": name,
            "display_name": name,
            "description": str(cap.get("description") or "").strip(),
            "category": cat,
            "runtime": runtime,
            "source": str(cap.get("source") or ""),
        })

    # 类别汇总
    grouped: dict[str, list[dict]] = {}
    for s in skills_data:
        grouped.setdefault(s["category"], []).append(s)
    categories_data = []
    for cat, items in grouped.items():
        name, icon, desc = _humanize_category(cat)
        categories_data.append({
            "id": cat,
            "name": name,
            "icon": icon,
            "description": desc,
            "count": len(items),
        })

    # 执行方式汇总
    rt_counts = Counter(s["runtime"] for s in skills_data)
    order = {"prompt": 0, "process": 1, "mcp": 2}
    runtimes_data = [
        {"id": rt, "name": RUNTIME_LABEL.get(rt, rt), "count": rt_counts[rt]}
        for rt in sorted(rt_counts, key=lambda r: order.get(r, 9))
    ]

    # 创建 16:9 演示文稿
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # 1. 英雄页
    build_hero_slide(prs, caps, categories_data, runtimes_data)

    # 2. 类别概览页（每页 12 个，至少 1 页）
    cat_pages = max(1, (len(categories_data) + CAT_PER_PAGE - 1) // CAT_PER_PAGE)
    for i in range(cat_pages):
        build_categories_overview_slide(prs, categories_data, i, cat_pages)

    # 3. 分布页
    build_distribution_slide(prs, categories_data, runtimes_data)

    # 4. 能力目录页（每页 12 个）
    total_catalog_pages = max(1, (len(skills_data) + PER_PAGE - 1) // PER_PAGE)
    for i in range(total_catalog_pages):
        build_catalog_slide(prs, skills_data, i, total_catalog_pages, PER_PAGE)

    # 5. 每个主要类别（≥3 个能力）的详情页
    major_cats = [c for c in categories_data if c["count"] >= 3]
    for cat in major_cats:
        items = [s for s in skills_data if s["category"] == cat["id"]]
        cat_total = max(1, (len(items) + 11) // 12)
        for i in range(cat_total):
            build_category_detail_slide(
                prs, skills_data, cat["id"],
                _humanize_category(cat["id"]),
                i, cat_total)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(OUT))
    print(f"已生成：{OUT}  （{len(skills_data)} 个能力 / {len(categories_data)} 个类别 / "
          f"{len(runtimes_data)} 种执行方式 / {len(prs.slides)} 张幻灯片）")


if __name__ == "__main__":
    main()
