"""直播带货话术场景（scene）识别 + 分场景最佳实践。

给知识库和点评引入"话术场景"维度：一段话术处在开场/讲款/逼单/答疑…哪个环节，
决定了该做什么动作、该召回哪类知识。detect_scene 用带权关键词打分（快、实时、免 LLM），
scene_playbook 给该场景的标准动作，供段级点评判断"该场景该做的漏没漏"。

设计要点（来自审查修正）：
- 关键词带权：强信号词(2 分)压过泛词(1 分)，避免"耳机/声音"这类高频泛词主导。
- 平手用 PRIORITY 显式 tie-break：动作型(逼单/互动/异议)优先于泛讲款，避免系统性偏 product。
- 关键词经清洗，去掉"和/跟/比/对比/够用"等会子串误命中或语义过宽的词。
- 合规违禁词【独立并行检测】(detect_compliance)：违禁词常夹在讲款句里，不能和讲款比数量后被吞，
  命中即单独提示，无论主场景是什么。
"""

from __future__ import annotations

# (key, 中文名, {权重: [关键词]}) —— 命中即按权重计分
SCENES: list[tuple[str, str, dict[int, list[str]]]] = [
    ("opening", "开场留人", {
        2: ["欢迎", "点关注", "关注一下", "加个团", "加入粉丝团", "家人们", "灯牌", "福利款", "留个人"],
        1: ["刚进来", "新来的", "开播", "先别走", "预告", "留下来"]}),
    ("product", "产品讲款", {
        2: ["振膜", "单元", "阻抗", "灵敏度", "频响", "声场", "三频", "平板", "动圈", "静电",
            "Demo Studio", "Demo Pro", "ANANDA", "Edition", "SUNDARA", "煲机", "推力", "拓扑"],
        1: ["音质", "解析", "人声", "低频", "佩戴", "做工", "头梁", "耳罩", "旗舰", "这款耳机", "声音"]}),
    ("proof", "信任证据", {
        2: ["销量", "好评", "获奖", "正品", "保修", "质保", "旗舰店", "专利", "官方", "发烧友"],
        1: ["卖了", "口碑", "评测", "测评", "大牌", "多少年", "研发"]}),
    ("objection", "异议答疑", {
        2: ["太贵", "值不值", "值得吗", "犹豫", "纠结", "哪个好", "区别在", "适不适合", "够不够用", "预算不"],
        1: ["有点贵", "划算", "考虑一下", "学生党", "肉疼", "预算"]}),
    ("urge", "逼单促单", {
        2: ["下单", "拍下", "小黄车", "库存", "仅剩", "仅限", "限时", "限量", "秒杀", "涨价",
            "最后", "名额", "立减", "领券", "上车", "补货", "抓紧", "手慢", "错过"],
        1: ["抢", "只剩", "优惠", "活动价", "到手", "链接", "券", "拍它"]}),
    ("interaction", "互动宠粉", {
        2: ["扣1", "扣个1", "扣屏", "扣起来", "扣想要", "扣型号", "抽奖", "福袋", "连麦"],
        1: ["公屏", "点赞", "评论区", "打在公屏"]}),
    ("transition", "过款承接", {
        2: ["接下来", "下一款", "换一款", "第二款", "第三款", "再看一款", "看下一个"],
        1: ["上一款", "下一个"]}),
    ("compliance", "合规风险", {   # 仅用于知识库分库；实时识别走 detect_compliance
        2: ["合规", "违禁词", "极限词"], 1: ["功效", "夸大"]}),
]

# 违禁 / 极限词：独立并行检测，命中即提示（不与主场景比数量）
COMPLIANCE_WORDS: list[str] = [
    "最好", "第一", "唯一", "全网最低", "绝对", "根治", "治疗", "治愈", "永久",
    "国家级", "百分百", "最便宜", "史上最", "包治", "顶级", "最强", "全球首",
]

# 平手 tie-break 优先级（靠前者优先）：动作/风险型优先于泛讲款，product 垫底
PRIORITY = ["urge", "interaction", "objection", "opening", "proof",
            "transition", "product", "compliance", "general"]

_LABEL = {k: name for k, name, _ in SCENES}
_LABEL["general"] = "通用"
_LABEL[""] = "未分场景"      # 改造前入库、未打场景标签的存量切片
_KEY_FROM_LABEL = {name: k for k, name in _LABEL.items()}

# 每场景的标准动作（点评基线：判断"该场景该做的漏没漏"）
SCENE_PLAYBOOK: dict[str, list[str]] = {
    "opening": [
        "开场 30 秒讲清『直播间有什么 + 为什么值得留下』，并预告爆款/福利节点",
        "高频欢迎 + 引导关注/加团，把新进流量留住，别冷启动掉人",
        "抛留人钩子（限时福利/爆款预告），30 秒内给到停留理由"],
    "product": [
        "按『人群痛点→核心卖点→关键参数→适配建议→价格』推进，先讲对谁有用再报参数",
        "型号/参数一律以知识库为准，不得口误或编造（如 Demo Studio 无 Stealth 型号）；价格随直播活动变化，不做核对",
        "卖点用场景/对比/数据换角度复述，避免同句反复；单品讲解占比 >50% 再切款"],
    "proof": [
        "用销量/好评/获奖/品牌历史建立信任，数字要具体可信",
        "点到官方旗舰店/正品保修/售后资质，打消'会不会假货/售后没保障'的顾虑"],
    "objection": [
        "面对『贵/犹豫』先塑价值再谈价，用对比和人群适配化解，别急着降价",
        "主动回答高频疑问（和 XX 比怎么样/适不适合我/够不够用），别回避"],
    "urge": [
        "报价紧跟促单三件套：稀缺（库存/限量）+ 优惠对比（划线原价）+ 行动指令（拍小黄车）",
        "制造紧迫感（限时/最后 N 单/马上涨价）但不得虚假",
        "只报价不促单会断转化，务必给明确下单指令并重复引导"],
    "interaction": [
        "每讲完一款至少抛 1–2 次互动（扣 1/扣型号/福袋），提升停留与权重",
        "及时回应公屏提问，让观众有参与感，冷场时用互动把节奏接住"],
    "transition": [
        "换款要有过渡句衔接（『这款先到这，接下来看更适合 XX 的』），别硬切",
        "单品讲够再切下一款，频繁跳品观众跟不上、转化分散"],
    "compliance": [
        "禁绝对化词（全网最低/第一/唯一/最好）与医疗功效（治疗/根治耳鸣）",
        "比较性、销量、价格表述要有依据并与后台一致，避免平台处罚"],
    "general": [
        "保持节奏：语速 240–340 字/分，讲完卖点留 0.5–1 秒停顿让观众消化",
        "卖点结合人群痛点，避免一上来堆参数；报价前先塑价值"],
}


def detect_scene(text: str) -> tuple[str, int]:
    """一段话术 → (场景 key, 加权得分)。全不命中返回 ('general', 0)。

    得分 = 命中关键词权重之和；平手时按 PRIORITY 取靠前场景（动作型优先，避免偏 product）。
    """
    t = text or ""
    scores: dict[str, int] = {}
    for key, _name, tiers in SCENES:
        s = 0
        for wt, words in tiers.items():
            s += wt * sum(1 for w in words if w in t)
        if s:
            scores[key] = s
    if not scores:
        return "general", 0
    best = max(scores.items(),
              key=lambda kv: (kv[1], -PRIORITY.index(kv[0]) if kv[0] in PRIORITY else -99))
    return best[0], best[1]


def detect_compliance(text: str) -> int:
    """违禁 / 极限词命中数（独立于主场景，命中即应提示合规）。"""
    t = text or ""
    return sum(1 for w in COMPLIANCE_WORDS if w in t)


def scene_label(key: str) -> str:
    return _LABEL.get(key or "general", "通用")


def key_from_label(label: str) -> str:
    """LLM 输出的中文场景名 → 内部 key；无法识别返回 ''。"""
    return _KEY_FROM_LABEL.get((label or "").strip(), "")


def scene_playbook(key: str, k: int = 3) -> list[str]:
    tips = SCENE_PLAYBOOK.get(key or "general") or SCENE_PLAYBOOK["general"]
    return tips[:k]


def scene_keys() -> list[str]:
    return [k for k, _n, _w in SCENES] + ["general"]


# ===== 讲款循环模型：一款产品的完整讲解应闭环，缺环节即转化打折 =====
# (key, 中文名, 关键词) —— 顺序即讲款推进顺序
LOOP_STEPS: list[tuple[str, str, list[str]]] = [
    ("pain", "痛点人群", ["痛点", "适合", "如果你", "经常听", "发烧友", "入门", "升级",
                        "通勤", "办公", "什么人", "哪些人", "人群", "需求", "学生党", "发烧党"]),
    ("selling", "核心卖点", ["卖点", "优势", "好处", "亮点", "特点", "主打", "核心",
                          "牛在", "强在", "最大的特点", "杀手锏"]),
    ("spec", "关键参数", ["参数", "阻抗", "灵敏度", "振膜", "频响", "单元", "赫兹",
                       "重量", "克重", "尺寸", "口径", "欧姆", "拓扑"]),
    ("fit", "适配建议", ["适配", "搭配", "推力", "前端", "手机能推", "手机直推", "解码",
                      "耳放", "好推", "难推", "直推", "推得动", "什么设备", "前端搭配", "需要耳放"]),
    ("price", "价格", ["价格", "多少钱", "到手", "优惠价", "活动价", "原价", "券后",
                     "日常价", "售价", "报价", "块钱"]),
    ("cta", "促单下单", ["下单", "拍下", "小黄车", "库存", "抢购", "链接", "上车",
                      "立减", "领券", "限时", "拍它", "加购"]),
]


def detect_loop(text: str) -> dict[str, bool]:
    """一段讲款话术 → 各讲款环节是否出现。"""
    t = text or ""
    return {key: any(w in t for w in words) for key, _name, words in LOOP_STEPS}


def analyze_loop(text: str) -> dict:
    """讲款闭环覆盖：已覆盖 / 缺失环节 + 完整度百分比。"""
    cov = detect_loop(text)
    covered = [name for key, name, _ in LOOP_STEPS if cov[key]]
    missing = [name for key, name, _ in LOOP_STEPS if not cov[key]]
    n = len(LOOP_STEPS)
    return {"covered": covered, "missing": missing,
            "pct": round(len(covered) * 100 / n),
            "steps": [{"step": name, "ok": cov[key]} for key, name, _ in LOOP_STEPS]}


def aggregate_loops(loops: list[dict]) -> dict:
    """多段讲款闭环 → 整场诊断：哪些环节经常漏（>=半数讲款段缺失）。"""
    loops = [x for x in (loops or []) if x and x.get("steps")]
    if not loops:
        return {}
    n = len(loops)
    miss_cnt: dict[str, int] = {}
    for lp in loops:
        for s in lp["steps"]:
            if not s["ok"]:
                miss_cnt[s["step"]] = miss_cnt.get(s["step"], 0) + 1
    often_missing = [name for name, c in miss_cnt.items() if c >= (n + 1) // 2]
    avg_pct = round(sum(lp.get("pct", 0) for lp in loops) / n)
    return {"product_segments": n, "avg_completeness": avg_pct,
            "often_missing": often_missing}


def _as_key(value: str) -> str:
    """场景 key 或中文名都认成 key（整场总结传的是段落的 scene_label 中文名，节奏分析传的是 key）。
    认不出的算 general；空串保持空串（下面会被丢掉）。"""
    v = (value or "").strip()
    if v in _LABEL:
        return v
    return _KEY_FROM_LABEL.get(v, "general")


def analyze_scene_flow(keys: list[str]) -> dict:
    """各段场景序列 → 整场环节结构：弧线 + 分布 + 结构诊断（供整场逻辑复盘）。

    2026-10-04 修：以前只认 key，整场总结传进来的中文名全被当成「通用」，
    每个人都被诊断成「全程没有开场留人 / 逼单促单 / 互动宠粉」。"""
    from collections import Counter

    keys = [k for k in (_as_key(x) for x in (keys or [])) if k]
    if not keys:
        return {}
    n = len(keys)
    arc: list[str] = []            # 折叠连续重复 → 环节弧线
    for k in keys:
        if not arc or arc[-1] != k:
            arc.append(k)
    cnt = Counter(keys)
    dist = [{"scene": scene_label(k), "count": c, "pct": round(c * 100 / n)}
            for k, c in cnt.most_common()]
    present = set(keys)
    product_share = round(cnt.get("product", 0) * 100 / n)

    di: list[str] = []
    if "opening" not in present:
        di.append("全程未识别到明确『开场留人』环节，开播流量承接可能薄弱")
    if "urge" not in present:
        di.append("全程未识别到『逼单促单』环节，转化引导严重缺失，易只讲不卖")
    if "interaction" not in present:
        di.append("『互动宠粉』环节缺失，停留时长与权重可能受影响")
    if cnt.get("product", 0) and product_share < 30:
        di.append(f"讲款占比仅 {product_share}%，产品讲解可能不足、卖点没铺够")
    # 逼单是否跟在讲款之后（首个 product 段之后是否出现 urge 段）
    if "product" in present and "urge" in present:
        first_p = keys.index("product")
        if not any(k == "urge" for k in keys[first_p:]):
            di.append("逼单环节出现在讲款之前，讲款后未承接促单，转化容易断档")

    # ---- 框架提炼（2026-07-10 整改）：去噪 → 循环切分 → 相似合并 ----
    # 罗列全部片段没有指导意义；提炼出"主框架 + 变体"才能对标和改进。
    frame = _distill_frame(keys)

    return {"arc": [scene_label(k) for k in arc], "distribution": dist,
            "diagnostics": di, "product_share": product_share, "segments": n,
            "frame": frame}


def _distill_frame(keys: list[str]) -> dict:
    """把整场场景序列提炼为「主框架 + 变体」：
    ①去噪：general(通用/闲聊) 段不参与框架；②压缩连续重复；
    ③循环切分：以『产品讲款』为锚切成 N 个讲款循环（直播带货的天然单元）；
    ④相似合并：相同环节序列的循环合并计数，最多者为主框架。"""
    from collections import Counter

    clean = [k for k in keys if k and k != "general"]
    denoised = len(keys) - len(clean)
    comp: list[str] = []
    for k in clean:
        if not comp or comp[-1] != k:
            comp.append(k)
    if not comp:
        return {}

    # 开场前奏（第一个讲款之前的环节）单独记，不算循环
    try:
        first_p = comp.index("product")
    except ValueError:
        # 整场没有讲款段：无循环可言，只给压缩弧线
        return {"prelude": [scene_label(k) for k in comp], "loops": 0,
                "main": [], "main_count": 0, "variants": [], "denoised": denoised}
    prelude = comp[:first_p]

    # 按 product 锚点切循环：每个循环 = [product, ...直到下一个 product 前]
    loops: list[tuple[str, ...]] = []
    cur: list[str] = []
    for k in comp[first_p:]:
        if k == "product" and cur:
            loops.append(tuple(cur))
            cur = [k]
        else:
            cur.append(k)
    if cur:
        loops.append(tuple(cur))

    cnt = Counter(loops)
    (main_seq, main_count) = cnt.most_common(1)[0]
    variants = [{"seq": [scene_label(k) for k in seq], "count": c}
                for seq, c in cnt.most_common(4)[1:] if c >= 1]
    return {
        "prelude": [scene_label(k) for k in prelude],
        "loops": len(loops),
        "main": [scene_label(k) for k in main_seq],
        "main_count": main_count,
        "variants": variants,
        "denoised": denoised,
    }
