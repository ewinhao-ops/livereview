"""罗盘全局上报（装一次、永久生效）。

旧机制的问题：/api/sessions/{id}/compass 的 token 和 URL 都是一场一配——
打卡自动建场后场控拿不到场次号，等于没法用。
本模块改为固定入口：场控电脑装一次油猴脚本（或书签），脚本固定发
POST /api/compass（全局 token），后端收到后自动挂到「当前正在直播的场次」，
没有在播场次就丢弃（返回 attached:false）。场控日常唯一操作 = 保持罗盘页开着。

鉴权：POST /api/compass 命中 main.py 的 endswith("/compass") 豁免，走全局 token 校验；
GET /api/compass/setup 走统一登录；partner 版在 partner.py（业绩后台 BFF 用）。
上报地址 = PUBLIC_BASE_URL（默认 https://workspace.example.test/fupan）+ /api/compass。早先装的脚本仍打
https://review.example.test/api/compass（旧域名继续原样代理这个接口），后端只看路径、不看域名，照常收。
"""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import public_url
from ..db import get_db
from ..models import CompassPoint, ReplaySession, SessionStatus, SystemSetting

router = APIRouter(prefix="/api/compass", tags=["compass"])

_TOKEN_KEY = "compass_global_token"

# 罗盘「直播大屏」真实指标标题（2026-07-06 实测 compass.jinritemai.com/screen/shop/main）。
# 全等匹配，杜绝旧模糊词的张冠李戴（如"成交"命中"成交粉丝占比"抓到 62.5%）。
# 上报键 = 页面标题原文，归因引擎按名字特征分类（率/时长/量）。
_METRIC_TITLES = [
    # 成交
    "直播间用户支付金额", "直播间成交金额", "净成交金额", "成交金额",
    "成交件数", "成交人数", "成交订单数",
    "点击-成交转化率", "成交转化率", "观看-成交转化率",
    "千次观看用户支付金额", "成交粉丝占比", "客单价",
    # 流量 / 互动
    "实时在线人数", "平均在线人数", "在线人数",
    "累计观看人数", "观看人次", "观看人数",
    "新增粉丝数", "新加直播团人数", "加入粉丝团数", "人均观看时长",
    "曝光-进入率", "曝光进入率", "直播间曝光人数", "直播间进入人数",
    "点赞次数", "评论次数", "分享次数", "点赞数", "评论数",
    "商品曝光人数", "商品点击人数",
]


def get_global_token(db: Session) -> str:
    row = db.get(SystemSetting, _TOKEN_KEY)
    if row is None or not (row.value or "").strip():
        token = secrets.token_urlsafe(18)
        if row is None:
            db.add(SystemSetting(key=_TOKEN_KEY, value=token))
        else:
            row.value = token
        db.commit()
        return token
    return row.value.strip()


def _active_session(db: Session) -> ReplaySession | None:
    """当前应挂靠的场次：未结束、状态在 采集前/采集中，取最新一场。"""
    return db.execute(
        select(ReplaySession).where(
            ReplaySession.ended_at.is_(None),
            ReplaySession.status.in_([SessionStatus.created, SessionStatus.parsing,
                                      SessionStatus.live]),
        ).order_by(ReplaySession.id.desc())
    ).scalars().first()


@router.post("")
async def report(request: Request, db: Session = Depends(get_db)):
    """全局罗盘上报：token 校验 → 自动挂当前在播场次。无在播场次则丢弃（200，脚本无感）。"""
    body = await request.body()
    if len(body) > 8192:
        raise HTTPException(413, "数据过大")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "数据格式错误")
    if not hmac.compare_digest(str(payload.get("token") or ""), get_global_token(db)):
        raise HTTPException(403, "token 无效")
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data:
        return {"ok": True, "attached": False, "reason": "empty"}
    row = _active_session(db)
    if row is None or row.started_at is None:
        return {"ok": True, "attached": False, "reason": "no_live_session"}
    if db.query(CompassPoint).filter(CompassPoint.session_id == row.id).count() >= 5000:
        return {"ok": True, "attached": False, "reason": "cap"}
    start_ms = max(0, int((datetime.now() - row.started_at).total_seconds() * 1000))
    db.add(CompassPoint(session_id=row.id, start_ms=start_ms,
                        data=json.dumps(data, ensure_ascii=False)[:4000]))
    db.commit()
    return {"ok": True, "attached": True, "session_id": row.id}


# ---------- 采集脚本生成 ----------

def _scraper_js(url: str, token: str) -> str:
    """页面抓取逻辑（书签/油猴共用主体），两遍扫描：
    ① odometer 翻牌大数字（GMV 等）：textContent 混着动画占位假 8，必须只拼
       .odometer-value/.odometer-formatting-mark 才是真值；向上找容器文本以
       指标标题开头来配对（主卡 DOM 序在前，"含异常交易"副卡自然被去重跳过）。
    ② 普通指标卡：标题叶子与指标表**全等匹配**（子串匹配会张冠李戴）→ 取相邻数字。
    最后 sendBeacon 上报。"""
    ks = json.dumps(_METRIC_TITLES, ensure_ascii=False)
    return (
        'var U=%s,T=%s,KS=%s;'
        'function grab(){var m={};'
        # ① odometer 大数字（先跑，占住 key，防第②遍把乱值写进来）
        'var ods=document.querySelectorAll(".odometer");'
        'for(var i=0;i<ods.length;i++){var od=ods[i];'
        'var ps=od.querySelectorAll(".odometer-value,.odometer-formatting-mark");'
        'var val="";for(var j=0;j<ps.length;j++)val+=(ps[j].textContent||"").trim();'
        'if(!val||!/[0-9]/.test(val))continue;'
        'var cur=od.parentElement,hit="";'
        'for(var up=0;up<4&&cur&&!hit;up++){var txt=(cur.textContent||"").trim();'
        'for(var k=0;k<KS.length;k++){if(txt.indexOf(KS[k])===0){hit=KS[k];break}}'
        'if(!hit)cur=cur.parentElement}'
        'if(hit&&!(hit in m))m[hit]=val.slice(0,24)}'
        # ② 普通指标卡（全等匹配）
        'var els=document.querySelectorAll("body *");'
        'for(var i=0;i<els.length;i++){var e=els[i];if(e.children.length)continue;'
        'var t=(e.textContent||"").trim();if(!t||t.length>14)continue;'
        'if(KS.indexOf(t)<0)continue;'
        'var v="",s=e.nextElementSibling;'
        'if(s)v=(s.textContent||"").trim();'
        'if(!v||!/[0-9]/.test(v)){var p=e.parentElement;if(p)v=(p.textContent||"").replace(t,"").trim()}'
        'if(v&&/[0-9]/.test(v)&&!(t in m))m[t]=v.slice(0,24)}'
        'var n=Object.keys(m).length;'
        'if(n)try{navigator.sendBeacon(U,JSON.stringify({token:T,data:m}))}catch(x){}'
        'return n}'
    ) % (json.dumps(url), json.dumps(token), ks)


def build_userscript(url: str, token: str) -> str:
    """Tampermonkey 油猴脚本：装一次，打开罗盘页即自动每 30 秒上报，带悬浮状态角标。"""
    return (
        "// ==UserScript==\n"
        "// @name         LiveReview 罗盘数据上报\n"
        "// @namespace    livereview\n"
        "// @version      1.0\n"
        "// @description  自动抓取抖音罗盘实时指标并上报复盘系统（自动挂当前直播场次）\n"
        "// @match        https://compass.jinritemai.com/*\n"
        "// @match        https://compass.douyin.com/*\n"
        "// @grant        none\n"
        "// ==/UserScript==\n"
        "(function(){\n"
        f"{_scraper_js(url, token)}\n"
        "var badge=document.createElement('div');"
        "badge.style.cssText='position:fixed;right:10px;bottom:10px;z-index:99999;"
        "background:rgba(20,30,50,.85);color:#7fd4a8;font:12px/1.6 sans-serif;"
        "padding:4px 10px;border-radius:8px;pointer-events:none';\n"
        "function tick(){var n=grab();var t=new Date().toTimeString().slice(0,8);"
        "badge.textContent=n?('罗盘上报中 ✓ '+t+'（'+n+'项）'):('罗盘上报：本页未识别到指标 '+t);}\n"
        "function boot(){if(document.body){document.body.appendChild(badge);tick();"
        "setInterval(tick,30000);}else setTimeout(boot,1000);}\n"
        "boot();\n"
        "})();\n"
    )


def build_bookmarklet(url: str, token: str) -> str:
    """书签兜底（不装油猴时用）：点一次启动，本页刷新前每 30 秒上报。"""
    core = _scraper_js(url, token)
    return ("javascript:(function(){" + core +
            "grab();if(window.__cmp)clearInterval(window.__cmp);"
            "window.__cmp=setInterval(grab,30000);"
            "alert('罗盘上报已启动(每30秒)，保持本页打开')})();")


def setup_payload(db: Session) -> dict:
    """setup 公共数据：token/脚本 + 采集状态（最近上报/今日点数/当前挂靠场次）。"""
    token = get_global_token(db)
    url = public_url("/api/compass")
    last = db.execute(select(CompassPoint).order_by(CompassPoint.id.desc())).scalars().first()
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_n = db.query(CompassPoint).filter(CompassPoint.created_at >= today0).count()
    active = _active_session(db)
    return {
        "ok": True,
        "report_url": url,
        "userscript": build_userscript(url, token),
        "bookmarklet": build_bookmarklet(url, token),
        "last_point_at": (last.created_at.isoformat(timespec="seconds") + "+08:00"
                          if last and last.created_at else None),
        "today_points": today_n,
        "active_session_id": active.id if active else None,
    }


@router.get("/setup")
def global_setup(request: Request, db: Session = Depends(get_db)):
    """全局接入信息（复盘系统内查看，统一登录）。"""
    return setup_payload(db)
