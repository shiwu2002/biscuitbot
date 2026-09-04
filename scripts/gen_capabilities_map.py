"""生成 biscuitbot「能力地图」自包含单页 HTML。

数据来源：``biscuitbot.capabilities.registry.CapabilityRegistry.list()``
（读时聚合技能 / CLI 应用 / MCP 预设为统一能力列表）。

设计对标 output/index.html（灯塔AI 技能地图）：
  英雄区 + 能力类别网格 + 能力分布条形图 + 执行方式词云 + 可搜索/筛选/排序/分页目录。

用法：python scripts/gen_capabilities_map.py
输出：output/biscuitbot-capabilities-map.html
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "biscuitbot-capabilities-map.html"

# runtime → 展示名（第二维度：执行方式）
RUNTIME_LABEL = {"prompt": "技能", "process": "应用", "mcp": "MCP"}
# source → 展示名
SOURCE_LABEL = {
    "builtin": "内置",
    "workspace": "工作区",
    "harness": "默认/社区",
    "preset": "预设",
    "mcp-preset": "预设",
    "cli-anything": "外部应用",
    "generated": "生成",
}

# category → 中文名 / 图标 / 短描述（未命中则回退为原始值 + 通用图标）
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
    "knowledge-management": ("知识管理", "📚", "知识沉淀、组织与检索。"),
    "3d": ("3D", "🧊", "三维建模、渲染与生成。"),
    "gamedev": ("游戏开发", "🎮", "游戏资产、剧情与开发。"),
    "game": ("游戏", "🎮", "游戏相关能力。"),
    "communication": ("沟通", "💬", "消息、协同与沟通场景。"),
    "browser": ("浏览器", "🧭", "浏览器自动化与页面理解。"),
    "debugging": ("调试", "🐞", "排障、日志与错误诊断。"),
    "testing": ("测试", "🧪", "测试执行与质量保障。"),
    "science": ("科学", "🔬", "科研、计算与数据分析。"),
    "scientific": ("科学", "🔬", "科研、计算与数据分析。"),
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


def _humanize_category(category: str) -> tuple[str, str, str]:
    if category in CATEGORY_META:
        return CATEGORY_META[category]
    return category, "🏷️", f"属于「{category}」类别的能力集合。"


def build_skills(caps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cap in caps:
        name = str(cap.get("display_name") or cap.get("name") or "")
        cat = str(cap.get("category") or "uncategorized")
        runtime = str(cap.get("runtime") or "prompt")
        source = str(cap.get("source") or "")
        requires = str(cap.get("requires") or "").strip()
        runtime_label = RUNTIME_LABEL.get(runtime, runtime)
        if runtime == "prompt":
            invoke = f"${name}"
        elif runtime == "process":
            invoke = f"应用·{name}"
        else:
            invoke = f"MCP·{name}"
        out.append(
            {
                "id": str(cap.get("id") or name),
                "name": name,
                "display_name": name,
                "description": str(cap.get("description") or "").strip(),
                "category": cat,
                "category_id": cat,
                "runtime": runtime,
                "runtime_id": runtime,
                "runtime_label": runtime_label,
                "source": source,
                "source_label": SOURCE_LABEL.get(source, source or "—"),
                "invoke": invoke,
                "requires": requires,
                "tier": str(cap.get("tier") or "").strip(),
            }
        )
    return out


def build_categories(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for s in skills:
        grouped.setdefault(s["category_id"], []).append(s)
    out: list[dict[str, Any]] = []
    for cat, items in grouped.items():
        name, icon, desc = _humanize_category(cat)
        can_do = [i["display_name"] for i in items[:3]]
        out.append(
            {
                "id": cat,
                "name": name,
                "icon": icon,
                "description": desc,
                "count": len(items),
                "can_do": can_do,
            }
        )
    return out


def build_runtimes(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(s["runtime_id"] for s in skills)
    order = {"prompt": 0, "process": 1, "mcp": 2}
    out = []
    for rt in sorted(counts, key=lambda r: order.get(r, 9)):
        out.append(
            {
                "id": rt,
                "name": RUNTIME_LABEL.get(rt, rt),
                "count": counts[rt],
            }
        )
    return out


# ---- 页面模板（样式 / 布局 / 交互对标 output/index.html） --------------------

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="data:,">
<style>
:root{--ink:#171714;--muted:#6f6c63;--ivory:#f6f2e8;--paper:#fffdf7;--gold:#b49358;--gold2:#d6bf8b;--line:#ded7c6;--dark:#24231f;--green:#48685b;--shadow:0 18px 60px rgba(41,35,24,.09)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--ivory);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
a{color:inherit}button,input,select{font:inherit}.shell{max-width:1440px;margin:auto;padding:0 28px 72px}
.hero{margin:24px 0 30px;min-height:390px;border-radius:28px;padding:52px;background:linear-gradient(128deg,rgba(20,20,18,.96),rgba(48,44,34,.94)),radial-gradient(circle at 88% 5%,#8c7446,transparent 42%);color:#fff;position:relative;overflow:hidden;box-shadow:var(--shadow)}
.hero:before{content:"";position:absolute;width:420px;height:420px;border:1px solid rgba(214,191,139,.34);border-radius:50%;right:-95px;top:-170px;box-shadow:0 0 0 55px rgba(214,191,139,.05),0 0 0 110px rgba(214,191,139,.035)}
.kicker{letter-spacing:.18em;color:var(--gold2);font-size:13px;font-weight:700;text-transform:uppercase}h1{font-size:clamp(38px,6vw,72px);line-height:1.08;margin:16px 0 18px;max-width:850px;letter-spacing:-.035em}.hero p{max-width:820px;color:#dedbd2;font-size:18px;margin:0}.hero-note{margin-top:24px!important;padding-left:14px;border-left:3px solid var(--gold2);font-size:14px!important;color:#c9c4b8!important}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:34px;max-width:920px}.stat{border-top:1px solid rgba(255,255,255,.22);padding-top:15px}.stat b{display:block;font-size:30px;color:#fff}.stat span{font-size:13px;color:#bbb7ae}
.section{margin:44px 0}.section-head{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:18px}h2{font-size:30px;margin:0;letter-spacing:-.02em}.section-head p{margin:0;color:var(--muted)}
.category-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.category-card{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:20px;cursor:pointer;transition:.2s;min-height:190px;box-shadow:0 6px 24px rgba(44,38,25,.04)}.category-card:hover{transform:translateY(-3px);border-color:var(--gold);box-shadow:var(--shadow)}.category-top{display:flex;justify-content:space-between;gap:14px}.category-icon{font-size:24px}.category-count{font-size:12px;background:#ede5d4;border-radius:999px;padding:4px 9px;height:max-content}.category-card h3{margin:12px 0 7px;font-size:18px}.category-card p{font-size:13px;color:var(--muted);margin:0 0 10px}.category-card ul{margin:0;padding-left:18px;font-size:12px;color:#4e4b43}
.dashboard{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}.panel{background:var(--paper);border:1px solid var(--line);border-radius:20px;padding:22px}.bars{display:grid;gap:10px}.bar-row{display:grid;grid-template-columns:150px 1fr 52px;gap:10px;align-items:center;font-size:13px}.bar-track{height:10px;background:#eee8dc;border-radius:99px;overflow:hidden}.bar-fill{height:100%;background:linear-gradient(90deg,var(--gold),var(--gold2));border-radius:99px}.industry-cloud{display:flex;flex-wrap:wrap;gap:9px}.industry-chip{border:1px solid var(--line);background:#faf7ef;border-radius:999px;padding:8px 11px;font-size:13px;cursor:pointer}.industry-chip b{color:var(--gold);margin-left:5px}
.catalog{background:var(--paper);border:1px solid var(--line);border-radius:24px;overflow:hidden;box-shadow:var(--shadow)}.toolbar{position:sticky;top:0;z-index:4;padding:18px;background:rgba(255,253,247,.96);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);display:grid;grid-template-columns:1.6fr 1fr 1fr auto;gap:10px}.control{width:100%;border:1px solid var(--line);border-radius:12px;padding:11px 13px;background:white;color:var(--ink)}.result-meta{padding:14px 20px;color:var(--muted);font-size:13px;border-bottom:1px solid #eee8dc}.skills{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0}.skill{padding:20px;border-right:1px solid #eee8dc;border-bottom:1px solid #eee8dc;min-height:225px}.skill:nth-child(3n){border-right:0}.badges{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}.badge{font-size:11px;padding:3px 8px;border-radius:999px;background:#eee7d8;color:#5d523e}.badge.ind{background:#e8eeea;color:#3f5e52}.skill h3{font-size:17px;line-height:1.35;margin:0 0 8px}.skill p{font-size:13px;color:var(--muted);margin:0 0 12px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}.invoke{display:block;padding:8px 9px;border-radius:8px;background:#292821;color:#eee9df;font:11px ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.source{font-size:11px;color:#9a9589;margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pagination{display:flex;justify-content:center;align-items:center;gap:10px;padding:18px}.btn{border:1px solid var(--line);background:#fff;border-radius:10px;padding:8px 13px;cursor:pointer}.btn:disabled{opacity:.35;cursor:not-allowed}.empty{padding:70px;text-align:center;color:var(--muted);grid-column:1/-1}
.footer{margin-top:32px;padding:26px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;display:flex;justify-content:space-between;gap:20px}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(max-width:1050px){.category-grid{grid-template-columns:repeat(2,1fr)}.skills{grid-template-columns:repeat(2,1fr)}.skill:nth-child(3n){border-right:1px solid #eee8dc}.skill:nth-child(2n){border-right:0}.dashboard{grid-template-columns:1fr}}
@media(max-width:700px){.shell{padding:0 14px 40px}.hero{padding:30px 22px;min-height:auto}.stats{grid-template-columns:repeat(2,1fr)}.category-grid{grid-template-columns:1fr}.toolbar{grid-template-columns:1fr}.skills{grid-template-columns:1fr}.skill{border-right:0!important}.bar-row{grid-template-columns:110px 1fr 40px}.footer{flex-direction:column}}
</style>
</head>
<body><main class="shell">
<section class="hero">
<div class="kicker">biscuitbot · Capability Map</div>
<h1>__TITLE__</h1>
<p>这是一张可搜索、可筛选的能力全景图。它不仅告诉你“有哪些能力”，还回答每一类能力属于什么执行方式（技能 / 应用 / MCP）、来源如何，以及在运行时如何找到并调用。</p>
<p class="hero-note">数据由 CapabilityRegistry 读时聚合：技能（SkillsLoader）、CLI 应用、MCP 预设。为避免全部说明同时占用上下文，这张地图用于“列表可见、按需显式调用”。</p>
<div class="stats"><div class="stat"><b>__S1__</b><span>已注册能力</span></div><div class="stat"><b>__S2__</b><span>能力类别</span></div><div class="stat"><b>__S3__</b><span>执行方式</span></div><div class="stat"><b>__S4__</b><span>缺失 / 不可用</span></div></div>
</section>
<section class="section"><div class="section-head"><div><div class="kicker" style="color:var(--gold)">Capability Architecture</div><h2>你拥有的能力类别</h2></div><p>点击任一类别，直接筛选该类全部能力</p></div><div class="category-grid" id="categoryGrid"></div></section>
<section class="section dashboard"><div class="panel"><div class="section-head"><h2 style="font-size:22px">能力分布</h2><p>按能力数量排序</p></div><div class="bars" id="bars"></div></div><div class="panel"><div class="section-head"><h2 style="font-size:22px">执行方式</h2><p>能力如何被调用</p></div><div class="industry-cloud" id="industryCloud"></div></div></section>
<section class="section"><div class="section-head"><div><div class="kicker" style="color:var(--gold)">Searchable Catalog</div><h2>全部能力目录</h2></div><p>支持名称、说明、类别、执行方式、来源搜索</p></div>
<div class="catalog"><div class="toolbar"><input id="search" class="control" placeholder="搜索：视频、财务、自动化、AI、网页……"><select id="category" class="control"><option value="">全部能力类别</option></select><select id="runtime" class="control"><option value="">全部执行方式</option></select><select id="sort" class="control"><option value="id">按编号</option><option value="name">按名称</option><option value="category">按类别</option></select></div><div class="result-meta" id="resultMeta"></div><div class="skills" id="skills"></div><div class="pagination"><button class="btn" id="prev">上一页</button><span id="pageInfo"></span><button class="btn" id="next">下一页</button></div></div></section>
<footer class="footer"><span>生成时间：__GEN__</span><span>biscuitbot 能力地图 · 可运行 <code>python scripts/gen_capabilities_map.py</code> 重新生成</span></footer>
</main>
<script>
const SKILLS=__SKILLS_JSON__;
const CATEGORIES=__CATEGORIES_JSON__;
const RUNTIMES=__RUNTIMES_JSON__;
const byId=id=>document.getElementById(id);
const state={q:'',category:'',runtime:'',sort:'id',page:1,size:24};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function init(){
 const cg=byId('categoryGrid'); cg.innerHTML=CATEGORIES.map(c=>`<article class="category-card" data-cat="${c.id}"><div class="category-top"><span class="category-icon">${c.icon}</span><span class="category-count">${c.count.toLocaleString()} 个</span></div><h3>${esc(c.name)}</h3><p>${esc(c.description)}</p><ul>${c.can_do.slice(0,3).map(x=>`<li>${esc(x)}</li>`).join('')}</ul></article>`).join('');
 cg.querySelectorAll('[data-cat]').forEach(el=>el.onclick=()=>{state.category=el.dataset.cat;byId('category').value=state.category;state.page=1;render();document.querySelector('.catalog').scrollIntoView({behavior:'smooth'})});
 const max=Math.max(...CATEGORIES.map(c=>c.count)); byId('bars').innerHTML=CATEGORIES.slice().sort((a,b)=>b.count-a.count).slice(0,12).map(c=>`<div class="bar-row"><span>${esc(c.name)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(2,c.count/max*100)}%"></div></div><b>${c.count}</b></div>`).join('');
 byId('industryCloud').innerHTML=RUNTIMES.map(i=>`<button class="industry-chip" data-ind="${i.id}">${esc(i.name)}<b>${i.count}</b></button>`).join(''); byId('industryCloud').querySelectorAll('[data-ind]').forEach(el=>el.onclick=()=>{state.runtime=el.dataset.ind;byId('runtime').value=state.runtime;state.page=1;render()});
 byId('category').innerHTML+=[...CATEGORIES].sort((a,b)=>a.name.localeCompare(b.name,'zh-CN')).map(c=>`<option value="${c.id}">${esc(c.name)}（${c.count}）</option>`).join('');
 byId('runtime').innerHTML+=RUNTIMES.map(i=>`<option value="${i.id}">${esc(i.name)}（${i.count}）</option>`).join('');
 byId('search').oninput=e=>{state.q=e.target.value.trim().toLowerCase();state.page=1;render()}; byId('category').onchange=e=>{state.category=e.target.value;state.page=1;render()}; byId('runtime').onchange=e=>{state.runtime=e.target.value;state.page=1;render()}; byId('sort').onchange=e=>{state.sort=e.target.value;state.page=1;render()}; byId('prev').onclick=()=>{state.page--;render()}; byId('next').onclick=()=>{state.page++;render()}; render();
}
function filtered(){let a=SKILLS.filter(s=>(!state.category||s.category_id===state.category)&&(!state.runtime||s.runtime_id===state.runtime));if(state.q){a=a.filter(s=>`${s.name} ${s.description} ${s.id} ${s.category} ${s.runtime_label} ${s.source_label}`.toLowerCase().includes(state.q))};return a.sort((a,b)=>state.sort==='name'?a.name.localeCompare(b.name,'zh-CN'):state.sort==='category'?a.category.localeCompare(b.category,'zh-CN')||a.name.localeCompare(b.name,'zh-CN'):a.id.localeCompare(b.id)||a.name.localeCompare(b.name,'zh-CN'))}
function render(){const all=filtered(),pages=Math.max(1,Math.ceil(all.length/state.size));state.page=Math.min(state.page,pages);const start=(state.page-1)*state.size,items=all.slice(start,start+state.size);byId('resultMeta').textContent=`找到 ${all.length.toLocaleString()} 个能力 · 第 ${state.page} / ${pages} 页`;byId('skills').innerHTML=items.length?items.map(s=>`<article class="skill"><div class="badges"><span class="badge">${esc(s.category)}</span><span class="badge ind">${esc(s.runtime_label)}</span></div><h3>${esc(s.name)}</h3><p>${esc(s.description)}</p><span class="invoke">${esc(s.invoke)}</span><div class="source">来源 ${esc(s.source_label)}${s.requires?' · 依赖 '+esc(s.requires):''}</div></article>`).join(''):'<div class="empty">没有找到匹配的能力，请更换关键词或筛选条件。</div>';byId('pageInfo').textContent=`${state.page} / ${pages}`;byId('prev').disabled=state.page<=1;byId('next').disabled=state.page>=pages}
init();
</script>
</body>
</html>
"""


def _js(o: Any) -> str:
    """JSON 序列化并转义 </ 以防提前闭合 <script>，保留中文可读。"""
    s = json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/")


def main() -> None:
    from biscuitbot.capabilities.registry import CapabilityRegistry
    from biscuitbot.config.loader import load_config

    cfg = load_config()
    caps = CapabilityRegistry(cfg.workspace_path).list()

    skills = build_skills(caps)
    categories = build_categories(skills)
    runtimes = build_runtimes(skills)
    unavailable = sum(1 for c in caps if str(c.get("status")) != "available")

    html = (
        TEMPLATE.replace("__TITLE__", f"biscuitbot {len(caps)}+ 能力地图")
        .replace("__S1__", f"{len(caps):,}")
        .replace("__S2__", f"{len(categories):,}")
        .replace("__S3__", f"{len(runtimes):,}")
        .replace("__S4__", f"{unavailable:,}")
        .replace("__SKILLS_JSON__", _js(skills))
        .replace("__CATEGORIES_JSON__", _js(categories))
        .replace("__RUNTIMES_JSON__", _js(runtimes))
        .replace("__GEN__", datetime.now().strftime("%Y年%m月%d日 %H:%M"))
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成：{OUT}  （{len(caps)} 个能力 / {len(categories)} 个类别 / {len(runtimes)} 种执行方式）")


if __name__ == "__main__":
    main()
