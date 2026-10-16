"""知识库服务。

一份知识库同时服务四处：
  - ASR 热词（型号别名，降低误识）
  - LLM 校正术语表（把口语型号对齐标准名）
  - 句级硬错检测的对照基准（规格 / 官方价 / 违禁词）
  - 段级 / 整场点评的事实依据

种子数据为虚构演示耳机与示例规则，不能作为真实商品或法律合规依据；运营可在后台增改并 Excel 导入，
DB 里的条目会覆盖/补充种子数据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KnowledgeItem

# ---------- 虚构示例（仅供演示；实际使用前替换为自己的知识库）----------
SEED_PRODUCTS = [
    {
        "name": "Demo Pro",
        "aliases": ["演示旗舰", "演示旗舰", "demo pro", "演示旗舰"],
        "attrs": {
            "impedance_ohm": 60,
            "sensitivity_db": 83,
            "official_price": 39999,
            "drivability": "hard",  # 极难推，需大功率台式耳放
            "sellpoints": ["纳米级振膜", "旗舰平板", "需搭配大功率耳放"],
        },
    },
    {
        "name": "Demo Air",
        # “阿雅”在当前产品线中可能指多个 Demo Air 版本，不能作为默认别名。
        "aliases": ["演示轻量", "demo air", "演示轻量"],
        "attrs": {
            "impedance_ohm": 32,
            "sensitivity_db": 94,
            "official_price": 6999,
            "drivability": "medium",
            "sellpoints": ["隐形磁体", "大尺寸平板振膜"],
        },
    },
    {
        "name": "Demo Studio",
        # Demo Studio 还有 V2 / se / WiFi / UNVEILED 等版本，泛称不自动绑版本。
        "aliases": ["Demo Studio"],
        "attrs": {
            "impedance_ohm": 35,
            "sensitivity_db": 90,
            "official_price": 19999,
            "drivability": "medium",
            "sellpoints": ["隐形磁体", "纳米振膜"],
        },
    },
]

SEED_FORBIDDEN = [
    # 医疗功效
    "治疗", "治愈", "根治", "包治", "疗效", "药效", "抗癌", "降血压",
    # 绝对化 / 极限词
    "全网最低", "最低价", "绝对", "百分百", "第一品牌", "国家级",
    # 虚假紧迫
    "错过今天就", "最后一次", "再也没有",
]

SEED_SCRIPT_RULES = [
    {"name": "讲机型必报关键参数", "must": ["阻抗", "灵敏度", "推力建议"]},
    {"name": "报价后补促单话术", "must": ["库存", "下单引导"]},
]

# 违禁词上下文豁免：这些否定/澄清词出现在前面时，不算违规
_NEGATIONS = ("无法", "不能", "不会", "没法", "不可能", "并非", "不是", "别说", "勿", "禁止", "不要说", "不敢")


def forbidden_context_is_exempt(text: str, index: int, keyword: str) -> bool:
    """Shared negation/context exemption used by legacy and snapshot rule engines."""
    before = text[max(0, index - 4):index]
    after = text[index + len(keyword): index + len(keyword) + 4]
    if any(neg in before for neg in _NEGATIONS):
        return True
    if any(neg in after for neg in ("不是", "并非", "≠", "没有")):
        return True
    if keyword == "绝对" and after.startswith("值"):
        return True
    if keyword == "第一" and after[:1] in ("次", "时", "天", "步", "印"):
        return True
    return False


@dataclass
class Product:
    name: str
    aliases: list[str]
    impedance_ohm: int | None = None
    sensitivity_db: int | None = None
    official_price: int | None = None
    drivability: str = ""          # easy | medium | hard
    sellpoints: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        low = text.lower()
        if self.name.lower() in low:
            return True
        return any(a.lower() in low for a in self.aliases)


@dataclass
class KnowledgeBase:
    products: list[Product]
    forbidden: list[str]
    script_rules: list[dict]

    def hotwords(self) -> list[str]:
        words: list[str] = []
        for p in self.products:
            words.append(p.name)
            words.extend(p.aliases)
        return words

    def glossary(self) -> list[str]:
        return [p.name for p in self.products]

    def find_product(self, text: str) -> Product | None:
        """按命中的完整度选型号，避免短名抢先命中长型号。

        例如同时存在 ``SUNDARA`` 和 ``SUNDARA Closed-back`` 时，
        文本中出现完整后者应优先返回后者，而不是受数据库插入顺序影响。
        """
        low = (text or "").lower()
        candidates: list[tuple[int, int, Product]] = []
        for p in self.products:
            terms = [p.name, *p.aliases]
            matched = [t for t in terms if t and t.lower() in low]
            if not matched:
                continue
            # 完整名称优先于别名；同长度时保留产品列表顺序，结果稳定。
            name_hit = 1 if p.name.lower() in low else 0
            score = max(len(t) for t in matched)
            candidates.append((score, name_hit, p))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    def hit_forbidden(self, text: str) -> list[str]:
        """命中违禁/极限词，但带上下文豁免，降低误杀（如'无法根治'、'绝对值'、'第一品牌不是我们的'）。"""
        hits: list[str] = []
        for w in self.forbidden:
            idx = text.find(w)
            if idx < 0:
                continue
            if forbidden_context_is_exempt(text, idx, w):
                continue
            hits.append(w)
        return hits


def _product_from_dict(d: dict) -> Product:
    a = d.get("attrs", {})
    return Product(
        name=d["name"],
        aliases=list(d.get("aliases", [])),
        impedance_ohm=a.get("impedance_ohm"),
        sensitivity_db=a.get("sensitivity_db"),
        official_price=a.get("official_price"),
        drivability=a.get("drivability", ""),
        sellpoints=list(a.get("sellpoints", [])),
    )


def load_kb(db: Session | None = None) -> KnowledgeBase:
    """合并种子 + DB。DB 中同名条目覆盖种子。"""
    products = {p["name"]: _product_from_dict(p) for p in SEED_PRODUCTS}
    forbidden = list(SEED_FORBIDDEN)
    script_rules = list(SEED_SCRIPT_RULES)

    if db is not None:
        rows = db.execute(select(KnowledgeItem)).scalars().all()
        for it in rows:
            if it.kind == "product":
                try:
                    attrs = json.loads(it.attrs) if it.attrs else {}
                except json.JSONDecodeError:
                    attrs = {}
                products[it.name] = _product_from_dict(
                    {"name": it.name,
                     "aliases": [a.strip() for a in it.aliases.split(",") if a.strip()],
                     "attrs": attrs}
                )
            elif it.kind == "forbidden":
                if it.name and it.name not in forbidden:
                    forbidden.append(it.name)
            elif it.kind == "script":
                script_rules.append({"name": it.name, "note": it.note})

    return KnowledgeBase(
        products=list(products.values()),
        forbidden=forbidden,
        script_rules=script_rules,
    )
