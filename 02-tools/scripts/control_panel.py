"""Local control page. Call serve_control(handler, handler.path) from GET.

This helper's state routes only inspect fixed workspace paths and loopback
ports. The page submits user-selected actions to the separate control gateway;
this module never writes account, ticket or process state. Run --self-test for
isolated SQLite/HTTP checks without a running game server.
"""
from __future__ import annotations

import html
import json
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PORTS = (("bootstrap", "资源与控制页", 18080),
         ("business", "账号查询", 19001),
         ("business", "游戏业务", 19041))
BUILD_STATUS = {
    "label": "开发版本 · 大厅已进入，完整离线游玩尚未验收",
    "implemented": ["本机资源服务与 SQLite 存档", "新号大厅已实机进入，构建新手引导已显示",
                    "编队、部分养成和道具；抽卡、首章结算、部分任务与签到有后端测试",
                    "非付费商店已实现并通过事务测试，实际客户端操作仍待验收"],
    "pending": ["后续引导、首关与退出重启续档的完整实机验证",
                "装备、更多关卡和特殊战斗模式",
                "抽卡、养成与非付费商店的完整实机循环",
                "宿舍、基地、公会及活动等未完成玩法"],
    "note": "当前列表描述开发范围；端口可连接不代表玩法已完整验证。目标是官方停服后继续本机游玩，充值不在范围内；你可以自行选择内容访问范围。",
}


def _object(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _integer(value, default=0):
    try:
        return int(value) if not isinstance(value, (dict, list, bool)) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _text(value, limit=80):
    return value[:limit] if isinstance(value, str) else ""


def local_database(root):
    """Where the server keeps its save.

    A source checkout keeps it inside the checkout, which is also what a
    temporary test tree looks like, so root wins there. A frozen build keeps it
    beside the executable (server_core resolves it through app_path), which is
    not below root, so the application directory has to answer instead.
    """
    if getattr(sys, 'frozen', False):
        from config_codec import app_path
        return app_path('data', 'players.sqlite3')
    return root / "07-server/data/players.sqlite3"


def read_accounts(database: Path):
    """Whitelist public local save fields; never return raw state or tickets."""
    result = {"available": False, "accounts": [], "total": 0, "error": None,
              "path": str(database).replace("\\", "/")}
    if not database.is_file():
        result["error"] = "尚未生成本机存档。首次创建本地账号后会出现数据。"
        return result
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0.5)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            result["total"] = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            rows = connection.execute(
                "SELECT uid, account_name, created_at, revision, state_json "
                "FROM accounts ORDER BY uid LIMIT 50").fetchall()
        finally:
            connection.close()
        for uid, account_name, created_at, revision, serialized in rows:
            account = {"uid": _integer(uid), "account": _text(account_name),
                       "created_at": _integer(created_at), "revision": _integer(revision)}
            try:
                state = _object(json.loads(serialized))
                player = _object(state.get("player"))
                login = _object(state.get("login"))
                progress = _object(state.get("progress"))
                cards = [card for card in _list(state.get("cards")) if isinstance(card, dict)]
                stages = [row for row in _list(progress.get("mainLine")) if isinstance(row, dict)]
                account.update({
                    "name": _text(player.get("name")) or "未命名总队长",
                    "level": _integer(player.get("level")), "exp": _integer(player.get("exp")),
                    "gold": _integer(player.get("gold")), "diamond": _integer(player.get("diamond")),
                    "hot": _integer(player.get("hot")), "tp": _integer(player.get("tp")),
                    "resources": {**{key: _integer(player.get(key)) for key in
                                      ("gold", "diamond", "army_coin", "hot", "tp")},
                                  **{key: _integer(login.get(key)) for key in
                                     ("ability_num", "BIND_DIAMOND")},
                                  "store_exp": _integer(state.get("store_exp"))},
                    "inventory": {str(key): _integer(value) for key, value in
                                  _object(state.get("inventory")).items()
                                  if str(key).isdecimal() and _integer(key) > 0},
                    "offline_archive_pools": sorted({_integer(value) for value in
                                                     _list(state.get("offline_archive_pools"))
                                                     if _integer(value) > 0}),
                    "offline_unlock_all": False,
                    "offline_access": state.get("offline_access", {}),
                    "card_count": len(cards),
                    "cards": [{"cid": _integer(card.get("cid")), "cfgid": _integer(card.get("cfgid")),
                               "name": _text(card.get("name")), "level": _integer(card.get("level")),
                               "break_level": _integer(card.get("break_level"))} for card in cards[:200]],
                    "progress": {
                        "cleared_count": len(_list(progress.get("cleared_stages"))),
                        "guide_count": len(_list(progress.get("completed_guides"))),
                        "stages": [{"id": _integer(row.get("id")),
                                    "star": min(3, max(0, _integer(row.get("star"))))}
                                   for row in stages[:200]],
                    },
                    "error": None,
                })
            except (ValueError, TypeError):
                account["error"] = "此账号存档格式异常，无法显示；没有修改存档。"
            result["accounts"].append(account)
        result["available"] = True
        if not rows:
            result["message"] = "存档数据库可读取，目前没有账号。请在客户端创建本地新号。"
        if result["total"] > 50:
            result["message"] = "显示前 50 个本地账号。"
    except sqlite3.Error as error:
        reason = "数据库忙，请稍后刷新" if "locked" in str(error).lower() else "数据库不可读取或结构不兼容"
        result["error"] = "存档读取失败：" + reason + "。数据没有修改。"
    except OSError:
        result["error"] = "存档文件不可访问。请检查本机文件权限。"
    return result


def probe_port(port: int):
    """A TCP connection snapshot only; no game request or remote host contact."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return {"listening": True, "error": None}
    except OSError:
        return {"listening": False, "error": "本机端口连接失败；服务可能已停止或尚未就绪。"}


def collect_state(root: Path = None, port_probe=None):
    # Resolve both defaults at call time: a frozen server re-points ROOT after
    # import, and an import-time default would keep reading the bundled path.
    root = ROOT if root is None else root
    port_probe = probe_port if port_probe is None else port_probe
    process_record = {"available": False, "started": None, "error": None}
    pids = {}
    record_path = root / "07-server/run/processes.json"
    try:
        with record_path.open(encoding="utf-8-sig") as source:
            record = _object(json.load(source))
        process_record.update(available=True, started=_text(record.get("started")))
        for process in _list(record.get("processes")):
            process = _object(process)
            role = process.get("role")
            if role in ("bootstrap", "business") and _integer(process.get("pid")) > 0:
                pids[role] = _integer(process["pid"])
    except FileNotFoundError:
        process_record["error"] = "没有启动器记录；服务也可能由终端手动启动。"
    except (OSError, ValueError, TypeError):
        process_record["error"] = "启动器记录不可读取，以下状态以端口快照为准。"
    services = []
    for role, label, port in PORTS:
        status = port_probe(port)
        services.append({"role": role, "label": label, "port": port,
                         "recorded_pid": pids.get(role), **status})
    return {"updated_at": datetime.now(timezone.utc).isoformat(),
            "services": services, "process_record": process_record,
            "database": read_accounts(local_database(root)),
            "build": BUILD_STATUS}


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CrossCore PS · 本地控制端</title>
<style>
:root{font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;color:#253045;background:#f3f5f8;color-scheme:light}*{box-sizing:border-box}body{margin:0}main{max-width:1080px;margin:auto;padding:28px 20px 48px}
header,.toolbar{display:flex;gap:14px;align-items:center;justify-content:space-between;flex-wrap:wrap}header{margin-bottom:20px}h1{font-size:25px;line-height:1.3;margin:0}h2{font-size:18px;margin:0 0 12px}h3{font-size:15px;margin:0}p{margin:7px 0}.sub,.muted,.help{color:#647186;font-size:13px}.tag{padding:3px 11px;border-radius:16px;background:#e6ebf2;white-space:nowrap}
section,.action-card{background:white;border:1px solid #e1e6ee;border-radius:12px;padding:20px;margin:16px 0}.notice{background:#fff9e9;border-color:#eedca6}.error{color:#b3392e}.success{color:#257044}.services,.stats,.action-grid,.columns{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.service,.stat{padding:12px;border:1px solid #e4e9f0;border-radius:8px}.stats{grid-template-columns:repeat(4,1fr);margin:14px 0}.stat strong{display:block;font-size:22px;line-height:1.4;font-variant-numeric:tabular-nums}.action-grid,.columns{grid-template-columns:1fr 1fr;gap:18px}.action-card{margin:0;min-width:0}.role-card,.access-card,.pool-card{grid-column:1/-1}.pool-list{max-height:240px;overflow:auto;border:1px solid #e4e9f0;border-radius:7px;padding:8px 12px}.pool-row{display:flex;align-items:center;gap:9px;margin:7px 0}.pool-row input,.access-choice input{width:auto}.pool-row small{margin-left:auto;color:#647186;font-size:12px}.access-choice{display:flex;gap:9px;align-items:center}.pool-buttons{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.actions{margin-top:18px}
button,input,select,textarea{font:inherit;border:1px solid #cbd3df;border-radius:7px;background:white;padding:7px 10px;color:inherit;max-width:100%}button{cursor:pointer}button:disabled,input:disabled,select:disabled,textarea:disabled{opacity:.6;cursor:default}.primary{background:#315ec9;border-color:#315ec9;color:white;padding:8px 17px}.secondary{padding:4px 9px;font-size:13px}label{display:block;margin-top:12px;margin-bottom:5px;font-size:14px}input,textarea{width:100%}textarea{resize:vertical;min-height:100px}select.full{width:100%}.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.form-row>*{min-width:0}.picker{width:100%;margin-top:8px}.attachment{display:grid;grid-template-columns:minmax(0,1fr) 100px 40px;gap:8px;margin:8px 0}.attachment button{padding:4px}.result{margin-top:12px;white-space:pre-wrap;min-height:24px}.target{border-left:3px solid #315ec9;padding-left:11px;font-weight:500}.submit-row{display:flex;align-items:center;gap:12px;margin-top:16px}.warning{background:#fff4e5;border:1px solid #f0cf9a;border-radius:8px;padding:9px 12px;color:#8a5310;font-size:13px;margin-top:10px}.hidden{display:none!important}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px;border-bottom:1px solid #e8edf3}th{color:#647186;font-weight:500}.table-wrap{overflow-x:auto}details{margin-top:15px}summary{cursor:pointer;font-weight:500}.stage{display:inline-block;padding:3px 9px;margin:3px 5px 3px 0;background:#edf2f8;border-radius:5px}pre{overflow:auto;background:#f4f6f9;border-radius:7px;padding:12px;font:13px/1.7 Consolas,monospace}code{font-family:Consolas,monospace}ul{padding-left:20px;margin:7px 0}#network-error{display:none;padding:12px 16px;background:#fff0ee;border:1px solid #efbdb7;border-radius:8px}footer{font-size:12px;color:#647186}
@media(max-width:650px){main{padding:20px 12px}header{align-items:flex-start}h1{font-size:22px}section,.action-card{padding:16px}.services,.columns,.action-grid{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.role-card{grid-column:auto}.attachment{grid-template-columns:minmax(0,1fr) 85px 36px}.form-row{gap:8px}}
</style></head><body><main>
<header><div><h1>CrossCore PS 本地控制端</h1><p class="sub">查看存档、修改资源、添加角色与发送邮件</p></div><span class="tag">本机账号</span></header>
<div id="network-error" role="alert" class="error"></div>
<section class="notice"><h2 id="build-label">开发版本 · 大厅已进入，完整离线游玩尚未验收</h2><p id="build-note">正在读取当前状态…</p><details><summary>实现范围与待完成内容</summary><div class="columns"><div><h3>已有基础</h3><ul id="implemented"></ul></div><div><h3>仍需完成</h3><ul id="pending"></ul></div></div><p class="muted">当前实现范围以代码与测试为准。</p></details></section>
<section><div class="toolbar"><h2>服务状态</h2><button id="refresh" type="button">刷新状态</button></div><div id="services" class="services"></div><p id="record" class="muted"></p><p class="muted">端口状态仅表示本机服务可连接，实际玩法进度以客户端为准。</p></section>
<section><div class="toolbar"><h2>当前账号</h2><select id="accounts" aria-label="选择本地账号" class="hidden"></select></div><p id="database-message" class="muted">正在读取存档…</p><div id="account-view" class="hidden"><p id="account-title"></p><p id="account-detail" class="muted"></p><div id="stats" class="stats"></div><p id="progress"></p><div id="stages"></div><details><summary id="card-summary">角色列表</summary><div class="table-wrap"><table><thead><tr><th>角色</th><th>角色 ID</th><th>等级</th><th>跃升值</th></tr></thead><tbody id="cards"></tbody></table></div></details></div><p class="muted">存档每 10 秒刷新一次。停止服务后保留存档。</p></section>
<div class="toolbar"><div><h2>账号操作</h2><p id="operation-target" class="target">请选择一个可读取的本地账号。</p></div><button id="reload-catalog" type="button">重新加载配置</button></div><p id="catalog-status" class="muted" role="status">正在加载资源与角色配置…</p>
<div class="action-grid actions">
<form id="access-form" class="action-card access-card"><h2>内容访问</h2><p id="access-current" class="help">正在读取当前访问设置…</p><label class="access-choice"><input id="access-pools" type="checkbox">开放历史卡池</label><label class="access-choice"><input id="access-activities" type="checkbox">开放活动及活动内关卡</label><label class="access-choice"><input id="access-illustrations" type="checkbox">上架历史插画</label><p class="help">主线、普通功能与引导始终遵循原版条件；历史插画需商店解锁后购买。不会改变通关和奖励记录。尚未实现的玩法会提示具体缺口。</p><button id="access-submit" type="submit" class="primary">保存访问设置</button><p id="access-result" class="result" role="status"></p></form>
<form id="resource-form" class="action-card"><h2>修改资源与物品</h2><p class="help">选择常用资源，或按 ID、名称查找物品。</p><label for="resource-choice">资源</label><select id="resource-choice" class="full"></select><div id="item-search-fields" class="hidden"><label for="item-search">查找物品</label><input id="item-search" placeholder="输入物品 ID 或名称" autocomplete="off"><select id="item-picker" class="picker" size="5" aria-label="选择物品"></select><p id="item-matches" class="help"></p></div><p id="resource-current" class="help"></p><div class="form-row"><div><label for="resource-mode">修改方式</label><select id="resource-mode" class="full"><option value="add">增加 / 减少</option><option value="set">设置总数量</option></select></div><div><label id="amount-label" for="resource-amount">增减数量</label><input id="resource-amount" type="number" step="1" value="100" required></div></div><p id="resource-help" class="help">正数增加，负数减少。</p><p id="resource-warning" class="warning hidden" role="alert"></p><div class="submit-row"><button id="resource-submit" class="primary" type="submit" disabled>应用修改</button></div><p id="resource-result" class="result" role="status"></p><h3>当前账号背包</h3><p class="help">按账号列出存档中数量大于 0 的物品；点「改」会把该物品填进上面的表单，再逐条调整。</p><label for="inventory-search">筛选背包</label><input id="inventory-search" placeholder="输入物品 ID 或名称" autocomplete="off"><p id="inventory-summary" class="help"></p><div class="table-wrap"><table><thead><tr><th>物品</th><th>物品 ID</th><th>数量</th><th>操作</th></tr></thead><tbody id="inventory-rows"></tbody></table></div></form>
<form id="mail-form" class="action-card"><h2>发送游戏邮件</h2><label for="mail-title">标题</label><input id="mail-title" maxlength="256" placeholder="邮件标题" required><label for="mail-content">正文 <span class="help">（可选）</span></label><textarea id="mail-content" maxlength="16000" placeholder="输入邮件正文"></textarea><label>附件 <span class="help">（可选，物品 ID 与数量）</span></label><div id="attachments"></div><button id="add-attachment" class="secondary" type="button">＋ 添加附件</button><p class="help">附件按配置上限和当前余额检查；领取时会再次校验，其他未领取邮件不计入当前余额。</p><datalist id="item-hints"></datalist><label for="mail-days">有效期（天）</label><input id="mail-days" type="number" min="1" max="3650" step="1" value="30" required><div class="submit-row"><button id="mail-submit" class="primary" type="submit" disabled>发送邮件</button></div><p id="mail-result" class="result" role="status"></p><button id="clear-mail" class="secondary" type="button">清空当前账号邮件…</button><p class="help">删除全部当前邮件及未领取附件，保留既有领取回执；操作前会再次确认。</p><p id="clear_mail-result" class="result" role="status"></p></form>
<form id="role-form" class="action-card role-card"><h2>直接添加角色</h2><div class="form-row"><div><label for="role-search">查找角色</label><input id="role-search" placeholder="输入角色 ID 或名称" autocomplete="off"><p id="role-matches" class="help"></p></div><div><label for="role-picker">选择角色</label><select id="role-picker" class="full" aria-label="选择要添加的角色"></select><p id="role-current" class="help"></p></div></div><div class="submit-row"><button id="role-submit" class="primary" type="submit" disabled>添加角色</button></div><p id="role-result" class="result" role="status"></p></form>
<form id="pools-form" class="action-card pool-card"><h2>归档卡池</h2><p class="help">开启已保存的活动卡池访问，保留原概率与内容；常驻和首抽池无需归档。</p><p id="pools-current" class="help"></p><div id="pool-list" class="pool-list"></div><div class="pool-buttons"><button id="pools-limited" type="button" class="primary">开启全部限定归档池</button><button id="pools-enable" type="submit">开启所选归档池</button><button id="pools-disable" type="button">关闭所选归档池</button></div><p id="pools-result" class="result" role="status"></p></form>
</div>
<section><h2>启动与停止</h2><p>在 PowerShell 中进入项目目录，运行现有脚本：</p><pre>Set-Location -LiteralPath '@ROOT@'
.\start_local.ps1</pre><p>停止本项目的资源与业务服务：</p><pre>.\stop_local.ps1</pre><p class="muted">停止服务保留存档与资源。控制页由资源服务提供，停止后无法刷新；下次启动可继续读取。启动失败时查看 <code>07-server/run</code> 的日志。</p></section><footer id="updated">尚未取得状态</footer></main>
<script>
'use strict';
const INT_MAX=2147483647, $=id=>document.getElementById(id);let snapshot=null,selected='',refreshBusy=false,actionBusy=false,catalogBusy=false,csrfToken='';
let catalog=null,itemMap=new Map(),mailItemMap=new Map(),roleMap=new Map(),accessDraftUid='';const retries=new Map();
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(cls)node.className=cls;return node;}
function number(value){return Number(value||0).toLocaleString('zh-CN');}
function list(id,values){$(id).replaceChildren(...values.map(value=>el('li',value)));}
function currentAccount(){return snapshot?.database?.accounts?.find(account=>String(account.uid)===selected)||null;}
function integer(value,label,min=-INT_MAX,max=INT_MAX){const raw=String(value).trim();const parsed=Number(raw);if(!/^-?\d+$/.test(raw)||!Number.isSafeInteger(parsed)||parsed<min||parsed>max)throw new Error(label+'必须是 '+min+' 到 '+max+' 之间的整数。');return parsed;}
function mailText(value,label,max,required=false){const text=String(value).trim();if(required&&!text)throw new Error('请填写'+label+'。');const invalidUnicode=[...text].some(char=>char.codePointAt(0)>=0xd800&&char.codePointAt(0)<=0xdfff);if(invalidUnicode||/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(text)||new TextEncoder().encode(text).length>max)throw new Error(label+'过长或含有无效字符，请缩短或重新输入。');return text;}
function actionUid(){const account=currentAccount();if(!account||account.error)throw new Error('请先选择一个可读取的本地账号。');return integer(account.uid,'账号',1);}
function updateAvailability(){
 const account=currentAccount(),ready=!!account&&!account.error&&!!catalog&&!!csrfToken&&!catalogBusy;
 document.querySelectorAll('.actions input,.actions select,.actions textarea,.actions button').forEach(node=>{node.disabled=actionBusy||!ready;});
 $('resource-submit').disabled=actionBusy||!ready||(!catalog.resources.length&&!catalog.items.length);
 $('role-submit').disabled=actionBusy||!ready||!catalog.roles.length;
 const pools=catalog?.pools?.filter(pool=>pool.archivable)||[];['pools-enable','pools-disable'].forEach(id=>$(id).disabled=actionBusy||!ready||!pools.length);$('pools-limited').disabled=actionBusy||!ready||!pools.some(pool=>pool.limited);
 $('accounts').disabled=actionBusy||!snapshot?.database?.accounts?.length;$('reload-catalog').disabled=actionBusy||catalogBusy;
 $('operation-target').textContent=account&&!account.error?`操作账号：${account.name} · UID ${account.uid}`:'请选择一个可读取的本地账号。';
}
function renderAccount(){
 const accounts=snapshot.database.accounts,account=accounts.find(a=>String(a.uid)===selected)||accounts[0];
 $('account-view').classList.toggle('hidden',!account);if(!account){selected='';renderInventory();updateAvailability();return;}
 selected=String(account.uid);$('accounts').value=selected;$('account-title').textContent=account.name||'存档不可显示';
 $('account-detail').textContent=`本地账号 ${account.account} · UID ${account.uid} · 存档修订 ${account.revision}`;
 $('stats').replaceChildren();$('stages').replaceChildren();$('cards').replaceChildren();
 if(account.error){$('progress').textContent=account.error;$('progress').className='error';$('card-summary').textContent='角色不可读取';renderInventory();updateAvailability();return;}
 const stats=[['总队长等级',account.level],['金币',account.gold],['晶石',account.diamond],['体力',account.hot]];
 $('stats').replaceChildren(...stats.map(([label,value])=>{const node=el('div',undefined,'stat');node.append(el('span',label,'muted'),el('strong',number(value)));return node;}));
 $('progress').className='';$('progress').textContent=`已通关 ${number(account.progress.cleared_count)} 关 · 已完成引导 ${number(account.progress.guide_count)} 组 · 探索点 ${number(account.tp)}`;
 $('stages').replaceChildren(...account.progress.stages.map(stage=>el('span',`关卡 ${stage.id} · ${'★'.repeat(stage.star)}${'☆'.repeat(3-stage.star)}`,'stage')));
 $('card-summary').textContent=`角色列表（${number(account.card_count)} 个）`;
 $('cards').replaceChildren(...account.cards.map(card=>{const row=el('tr');[card.name||`角色 ${card.cid}`,card.cfgid,card.level,card.break_level].forEach(value=>row.append(el('td',value)));return row;}));
 if(account.card_count>account.cards.length){const row=el('tr'),cell=el('td','仅显示前 200 个角色。');cell.colSpan=4;row.append(cell);$('cards').append(row);}
 updateResourceCurrent();updateRoleCurrent();updatePoolsCurrent();updateAccessCurrent();updateAttachmentLimits();renderInventory();updateAvailability();
}
function render(data){
 snapshot=data;$('build-label').textContent=data.build.label;$('build-note').textContent=data.build.note;list('implemented',data.build.implemented);list('pending',data.build.pending);
 $('services').replaceChildren(...data.services.map(service=>{const node=el('div',undefined,'service');node.append(el('h3',service.label),el('p',service.listening?'端口可连接':'端口未就绪',service.listening?'success':'error'),el('p',`127.0.0.1:${service.port}`,'muted'));if(service.recorded_pid)node.append(el('p',`启动器记录 PID ${service.recorded_pid}`,'muted'));if(service.error)node.append(el('p',service.error,'muted'));return node;}));
 const record=data.process_record;$('record').textContent=record.error||(record.started?`启动记录：${record.started}`:'启动器没有记录时间。');const db=data.database;
 $('database-message').textContent=db.error||db.message||`存档可读取 · ${db.total} 个本地账号`;$('database-message').className=db.error?'error':'muted';
 $('accounts').replaceChildren(...db.accounts.map(account=>{const option=el('option',`${account.name||account.account} · ${account.uid}`);option.value=String(account.uid);return option;}));$('accounts').classList.toggle('hidden',!db.accounts.length);renderAccount();
 $('updated').textContent=`最近读取：${new Date(data.updated_at).toLocaleString('zh-CN')} · 仅访问本机服务`;
}
async function fetchJson(path,options={}){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);try{const response=await fetch(path,{cache:'no-store',...options,signal:controller.signal});let data;try{data=await response.json();}catch(error){throw new Error('本地服务暂时不可用，请稍后重试。');}if(!response.ok)throw new Error(data.message||'本地服务未能处理请求，请稍后重试。');return data;}finally{clearTimeout(timeout);}}
async function refresh(){if(refreshBusy)return;refreshBusy=true;$('refresh').disabled=true;try{render(await fetchJson('/control/state'));$('network-error').style.display='none';}catch(error){$('network-error').textContent='无法连接本地控制服务。显示的可能是上次状态，请启动本地服务后刷新。';$('network-error').style.display='block';$('updated').textContent='当前状态无法刷新 · '+new Date().toLocaleString('zh-CN');}finally{refreshBusy=false;$('refresh').disabled=false;}}
function matches(rows,text){const query=String(text).trim().toLowerCase();return rows.filter(row=>!query||String(row.cfgid).includes(query)||String(row.name||'').toLowerCase().includes(query));}
function fillItems(){if(!catalog)return;const rows=matches(catalog.items,$('item-search').value),previous=$('item-picker').value;$('item-picker').replaceChildren(...rows.slice(0,60).map(item=>{const option=el('option',`${item.cfgid} · ${item.name||'未命名物品'}`);option.value=String(item.cfgid);return option;}));if(rows.some(row=>String(row.cfgid)===previous))$('item-picker').value=previous;const exact=rows.find(row=>String(row.cfgid)===$('item-search').value.trim());if(exact)$('item-picker').value=String(exact.cfgid);$('item-matches').textContent=rows.length?`找到 ${rows.length} 项${rows.length>60?'，显示前 60 项，请继续输入筛选':''}`:'没有找到匹配的物品。';updateResourceCurrent();}
function fillRoles(){if(!catalog)return;const rows=matches(catalog.roles,$('role-search').value),previous=$('role-picker').value;$('role-picker').replaceChildren(...rows.slice(0,100).map(role=>{const option=el('option',`${role.name||'未命名角色'} · ${role.cfgid}${role.limited?' · 限定':''}${role.quality!==undefined?' · 品质 '+role.quality:''}`);option.value=String(role.cfgid);return option;}));if(rows.some(row=>String(row.cfgid)===previous))$('role-picker').value=previous;const exact=rows.find(row=>String(row.cfgid)===$('role-search').value.trim());if(exact)$('role-picker').value=String(exact.cfgid);$('role-matches').textContent=rows.length?`找到 ${rows.length} 个角色${rows.length>100?'，请继续输入筛选':''}`:'没有找到匹配的角色。';updateRoleCurrent();}
function itemRisks(item){return Array.isArray(item?.risks)?item.risks:[];}
function riskText(code){return catalog?.riskLabels?.[code]||code;}
function resourceMetadata(key){if(!catalog)return null;const named=catalog.resources.find(resource=>resource.key===key);if(named)return named;const match=/^item:([1-9][0-9]*)$/.exec(key);if(!match)return null;const item=itemMap.get(Number(match[1]));if(!item)return null;const canonical=catalog.resources.find(resource=>resource.cfgid===item.cfgid);return {...item,...canonical,key,label:item.name,kind:'item',cfgid:item.cfgid,risks:item.risks||[]};}
function chosenResource(){const key=$('resource-choice').value;return resourceMetadata(key==='__item__'?'item:'+$('item-picker').value:key);}
function resourceBalance(account,resource){const canonical=catalog?.resources?.find(row=>row.key===resource.key||resource.cfgid!==undefined&&row.cfgid===resource.cfgid);const key=canonical?.key||resource.key;if(account.resources?.[key]!==undefined)return account.resources[key];if(account[key]!==undefined)return account[key];return resource.cfgid!==undefined?account.inventory?.[String(resource.cfgid)]??0:undefined;}
function resourceChanged(){$('item-search-fields').classList.toggle('hidden',$('resource-choice').value!=='__item__');updateResourceCurrent();}
function updateResourceCurrent(){const account=currentAccount(),resource=chosenResource(),warning=$('resource-warning');if(!account||!resource){$('resource-current').textContent='';warning.textContent='';warning.classList.add('hidden');return;}const value=resourceBalance(account,resource);$('resource-current').textContent=(value===undefined?'当前数量暂不可显示':'当前数量：'+number(value))+(resource.max!==undefined?(resource.max_by_player_level?' · 上限随等级变化，最高 ':' · 上限 ')+number(resource.max):'');const risks=itemRisks(resource);warning.textContent=risks.length?'风险提示：'+risks.map(code=>riskText(code)).join('；')+'。仍可写入，但游戏内表现不保证。':'';warning.classList.toggle('hidden',!risks.length);const set=$('resource-mode').value==='set';$('resource-amount').max=String(resource.max??INT_MAX);$('resource-amount').min=String(set?0:-INT_MAX);}
function inventoryRows(account,query){const text=String(query||'').trim().toLowerCase();if(!account||account.error)return [];return Object.entries(account.inventory||{}).map(([cfgid,count])=>{const id=Number(cfgid),item=itemMap.get(id);return {cfgid:id,num:Number(count)||0,name:item?.name||''};}).filter(row=>row.num>0).filter(row=>!text||String(row.cfgid).includes(text)||row.name.toLowerCase().includes(text)).sort((a,b)=>b.num-a.num||a.cfgid-b.cfgid);}
function fillFromInventory(cfgid,num){$('resource-choice').value='__item__';resourceChanged();$('item-search').value=String(cfgid);fillItems();$('item-picker').value=String(cfgid);$('resource-mode').value='set';modeChanged();$('resource-amount').value=String(num);updateResourceCurrent();showResult('resource','已选中物品 '+cfgid+'（当前 '+number(num)+'）；确认目标数量后点「应用修改」。',true);}
function renderInventory(){const account=currentAccount(),rows=inventoryRows(account,$('inventory-search').value),shown=rows.slice(0,200);$('inventory-rows').replaceChildren(...shown.map(row=>{const tr=el('tr');tr.append(el('td',row.name||'未命名物品'),el('td',row.cfgid),el('td',number(row.num)));const cell=el('td'),button=el('button','改','secondary');button.type='button';button.addEventListener('click',()=>fillFromInventory(row.cfgid,row.num));cell.append(button);tr.append(cell);return tr;}));$('inventory-summary').textContent=!account?'请先选择一个账号。':account.error?account.error:'共 '+number(inventoryRows(account,'').length)+' 种持有物品，显示 '+number(shown.length)+' 条'+(rows.length>shown.length?'（请用筛选缩小范围）':'')+'。';}
function updateRoleCurrent(){const account=currentAccount(),id=Number($('role-picker').value),count=account?.cards?.filter(card=>card.cfgid===id).length||0;$('role-current').textContent=id?(count?`当前角色列表中已有 ${count} 个。`:'当前角色列表中未显示此角色。'):'';}
function modeChanged(){const set=$('resource-mode').value==='set';$('amount-label').textContent=set?'目标总数量':'增减数量';$('resource-help').textContent=set?'设置为此数量；填写 0 可清空该项。':'正数增加，负数减少。修改后的总数须在该项上限内。';$('resource-amount').min=String(set?0:-INT_MAX);updateResourceCurrent();}
async function session(){const data=await fetchJson('/control/session');if(typeof data.csrf_token!=='string'||!data.csrf_token)throw new Error('无法取得操作权限，请重新加载配置。');csrfToken=data.csrf_token;return csrfToken;}
function configureCatalog(data){const allItems=Array.isArray(data.items)?data.items:[];catalog={resources:Array.isArray(data.resources)?data.resources:[],items:allItems,mailItems:allItems.filter(item=>item.mail_supported===true||(item.mail_supported===undefined&&item.supported!==false)),roles:Array.isArray(data.roles)?data.roles.filter(role=>role.supported!==false):[],pools:Array.isArray(data.pools)?data.pools:[],riskLabels:(data.risk_labels&&typeof data.risk_labels==='object')?data.risk_labels:{}};itemMap=new Map(catalog.items.map(item=>[Number(item.cfgid),item]));mailItemMap=new Map(catalog.mailItems.map(item=>[Number(item.cfgid),item]));roleMap=new Map(catalog.roles.map(role=>[Number(role.cfgid),role]));}
async function loadCatalog(){if(catalogBusy||actionBusy)return;catalogBusy=true;$('catalog-status').className='muted';$('catalog-status').textContent='正在加载资源与角色配置…';updateAvailability();try{const [data]=await Promise.all([fetchJson('/control/catalog'),session()]);configureCatalog(data);const previous=$('resource-choice').value;$('resource-choice').replaceChildren(...catalog.resources.map(resource=>{const option=el('option',resource.label);option.value=resource.key;return option;}));if(catalog.items.length){const option=el('option','其他物品（按 ID 或名称查找）');option.value='__item__';$('resource-choice').append(option);}if(catalog.resources.some(row=>row.key===previous)||previous==='__item__')$('resource-choice').value=previous;fillItems();fillRoles();fillPools();suggestItems('');updateAttachmentLimits();resourceChanged();renderInventory();$('catalog-status').textContent=`配置已加载：${number(catalog.items.length)} 种可写入账号背包的物品（其中 ${number(catalog.items.filter(item=>itemRisks(item).length).length)} 种带风险提示）、${number(catalog.mailItems.length)} 种可邮件发送附件、${number(catalog.roles.length)} 个角色。部分特殊礼包仅支持邮件领取。你选择并提交后才会修改存档。`;}catch(error){$('catalog-status').className='error';$('catalog-status').textContent=error.message||'控制功能暂时不可用，请稍后重新加载。';}finally{catalogBusy=false;updateAvailability();}}
function fillPools(){if(!catalog)return;const rows=catalog.pools.filter(pool=>pool.archivable);$('pool-list').replaceChildren(...rows.map(pool=>{const row=el('label',undefined,'pool-row'),check=el('input');check.type='checkbox';check.value=String(pool.id);check.checked=!!pool.limited;row.append(check,el('span',`${pool.name||'卡池 '+pool.id}${pool.limited?' · 限定':''}`),el('small',undefined));return row;}));if(!rows.length)$('pool-list').append(el('p','当前没有可归档的活动卡池。','muted'));updatePoolsCurrent();}
function updatePoolsCurrent(){const account=currentAccount();if(!account)return;const ids=account.offline_archive_pools||[],names=ids.map(id=>catalog?.pools?.find(pool=>pool.id===id)?.name||String(id));$('pools-current').textContent=(account.offline_access?.pools?'历史卡池已开放；单独归档设置仍保留。 ':'')+(names.length?'单独归档开启：'+names.join('、'):'未设置单独归档卡池。');[...$('pool-list').children].forEach(row=>{const check=row.querySelector('input'),label=row.querySelector('small');if(check&&label)label.textContent=ids.includes(Number(check.value))?'已单独归档':'';});}
function updateAccessCurrent(){const account=currentAccount();if(!account)return;$('access-current').textContent='主线与普通功能：原版进度门槛。';if(accessDraftUid!==String(account.uid)){for(const key of ['pools','activities','illustrations'])$('access-'+key).checked=!!account.offline_access?.[key];accessDraftUid=String(account.uid);}}
function poolsPayload(uid,ids,enabled){const selected=[...new Set(ids.map(id=>integer(id,'卡池 ID',1)))].sort((a,b)=>a-b);if(!selected.length)throw new Error('请至少选择一个可归档卡池。');for(const id of selected){if(!catalog?.pools?.some(pool=>pool.id===id&&pool.archivable))throw new Error('卡池 '+id+'不支持归档管理。');}return {uid:integer(uid,'账号',1),pool_ids:selected,enabled:!!enabled};}
function accessPayload(uid,policy){return {uid:integer(uid,'账号',1),policy};}
function submitPools(enabled,limitedOnly=false){try{const ids=limitedOnly?catalog.pools.filter(pool=>pool.archivable&&pool.limited).map(pool=>pool.id):[...$('pool-list').querySelectorAll('input:checked')].map(node=>node.value);submitOperation('pools',poolsPayload(actionUid(),ids,enabled));}catch(error){showResult('pools',error.message,false);}}
function suggestItems(text){if(!catalog)return;$('item-hints').replaceChildren(...matches(catalog.mailItems,text).slice(0,60).map(item=>{const option=el('option');option.value=String(item.cfgid);option.label=item.name||'';return option;}));}
function mailItemLimit(cfgid,account){const item=mailItemMap.get(Number(cfgid));if(!item)return null;const maximum=integer(item.mail_max??item.max??INT_MAX,'附件数量上限',1);const resource=item.supported!==false?resourceMetadata('item:'+item.cfgid):null;const dynamic=!!resource?.max_by_player_level;const current=account&&resource?resourceBalance(account,resource):undefined;const balanceMaximum=resource?.max??item.max??INT_MAX;return {item,maximum,current,balanceMaximum,dynamic,remaining:current!==undefined&&!dynamic?Math.max(0,balanceMaximum-current):undefined};}
function updateAttachment(row){const id=row.querySelector('.attachment-id'),count=row.querySelector('.attachment-count'),note=row.querySelector('.attachment-limit'),limit=mailItemLimit(id.value,currentAccount());count.max=String(limit?.maximum??INT_MAX);if(!limit){note.textContent='输入附件物品 ID 查看配置上限与当前可领取数量。';return;}note.textContent=`${limit.item.name||limit.item.cfgid} · 单次附件上限 ${number(limit.maximum)}`+(limit.current!==undefined?` · 当前 ${number(limit.current)}`:'')+(limit.remaining!==undefined?` · 最多还能领取 ${number(limit.remaining)}`:limit.dynamic?' · 实际容量随等级变化，领取时再次检查。':' · 专用奖励将在领取时校验。');}
function updateAttachmentLimits(){[...$('attachments').children].forEach(updateAttachment);}
function addAttachment(){if($('attachments').children.length>=20){showResult('mail','最多添加 20 行附件。',false);return;}const row=el('div',undefined,'attachment'),id=el('input'),count=el('input'),remove=el('button','×'),note=el('p',undefined,'help attachment-limit');note.style.gridColumn='1 / -1';id.className='attachment-id';id.placeholder='物品 ID';id.setAttribute('list','item-hints');id.setAttribute('aria-label','附件物品 ID');id.addEventListener('input',()=>{suggestItems(id.value);updateAttachment(row);});id.addEventListener('focus',()=>suggestItems(id.value));count.className='attachment-count';count.type='number';count.min='1';count.max=String(INT_MAX);count.step='1';count.value='1';count.setAttribute('aria-label','附件数量');remove.type='button';remove.setAttribute('aria-label','移除此附件');remove.addEventListener('click',()=>row.remove());row.append(id,count,remove,note);$('attachments').append(row);updateAttachment(row);updateAvailability();}
function resourcePayload(uid,key,mode,raw){const amount=integer(raw,'资源数量');if(!key)throw new Error('请选择要修改的资源或物品。');if(!['add','set'].includes(mode))throw new Error('请选择修改方式。');if(mode==='set'&&amount<0)throw new Error('目标总数量不能为负数。');if(mode==='add'&&amount===0)throw new Error('增减数量不能为 0。');const resource=resourceMetadata(key);if(!resource)throw new Error('此资源暂不支持直接修改，请从配置中选择。');const account=currentAccount(),before=account&&Number(account.uid)===Number(uid)?resourceBalance(account,resource):undefined;const after=mode==='set'?amount:before===undefined?undefined:before+amount;if(after!==undefined)integer(after,'修改后的总数量',0,resource.max??INT_MAX);return {uid:integer(uid,'账号',1),key,mode,amount};}
function mailPayload(uid,title,content,rows,days){title=mailText(title,'邮件标题',256,true);content=mailText(content,'邮件正文',16000);if(!Array.isArray(rows)||rows.length>20)throw new Error('最多添加 20 行附件。');const combined=new Map();for(const row of rows){if(!String(row.cfgid).trim())continue;const cfgid=integer(row.cfgid,'附件物品 ID',1),num=integer(row.num,'附件数量',1);if(!mailItemMap.has(cfgid))throw new Error('未找到可邮件发送的附件 '+cfgid+'，请从邮件附件配置提示中选择。');combined.set(cfgid,integer((combined.get(cfgid)||0)+num,'同种附件总数量',1));}const selected=currentAccount(),account=selected&&Number(selected.uid)===Number(uid)?selected:null;for(const [cfgid,num] of combined){const limit=mailItemLimit(cfgid,account),name=limit.item.name||cfgid;if(num>limit.maximum)throw new Error(name+' 的同种附件总数量不能超过 '+number(limit.maximum)+'。');if(limit.remaining!==undefined&&num>limit.remaining)throw new Error(name+' 当前 '+number(limit.current)+'，上限 '+number(limit.balanceMaximum)+'，最多还能领取 '+number(limit.remaining)+'。请先消耗该资源或减少附件数量。');}return {uid:integer(uid,'账号',1),title,content,attachments:[...combined].map(([cfgid,num])=>({cfgid,num})),expires_days:integer(days,'有效期',1,3650)};}
function clearMailPayload(uid){return {uid:integer(uid,'账号',1)};}
function clearMail(){try{const account=currentAccount(),payload=clearMailPayload(actionUid());if(!window.confirm(`确认清空 ${account.name||'当前账号'}（UID ${payload.uid}）的全部邮件？未领取附件也会删除，既有领取回执保留。`))return;submitOperation('clear_mail',payload);}catch(error){showResult('clear_mail',error.message,false);}}
function rolePayload(uid,cfgid){const id=integer(cfgid,'角色 ID',1);if(!roleMap.has(id)||roleMap.get(id).supported===false)throw new Error('请选择配置中存在的角色。');return {uid:integer(uid,'账号',1),cfgid:id};}
function uuid(){if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID();const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);}
function operationRecord(kind,payload){const signature=JSON.stringify(payload),old=retries.get(kind);if(old?.signature===signature)return old;const record={signature,request_id:uuid()};retries.set(kind,record);return record;}
function showResult(kind,text,ok){const node=$(kind+'-result');node.textContent=text;node.className='result '+(ok?'success':'error');}
async function submitOperation(kind,payload){if(actionBusy)return;const record=operationRecord(kind,payload);actionBusy=true;updateAvailability();$(kind+'-result').className='result muted';$(kind+'-result').textContent='正在提交…';try{const token=await session();const result=await fetchJson('/control/api/'+kind,{method:'POST',headers:{'Content-Type':'application/json','X-Control-Token':token},body:JSON.stringify({...payload,request_id:record.request_id})});retries.delete(kind);showResult(kind,result.message||(result.ok?'操作成功。':'操作未完成。'),!!result.ok);if(result.ok){if(kind==='access')accessDraftUid='';await refresh();}}catch(error){showResult(kind,(error.message||'未收到提交结果，请稍后重试。')+' 相同内容可重试，不会重复执行同一操作。',false);}finally{actionBusy=false;updateAvailability();}}
$('refresh').addEventListener('click',refresh);$('reload-catalog').addEventListener('click',loadCatalog);$('accounts').addEventListener('change',()=>{selected=$('accounts').value;renderAccount();});$('resource-choice').addEventListener('change',resourceChanged);$('resource-mode').addEventListener('change',modeChanged);$('item-search').addEventListener('input',fillItems);$('item-picker').addEventListener('change',updateResourceCurrent);$('role-search').addEventListener('input',fillRoles);$('role-picker').addEventListener('change',updateRoleCurrent);$('add-attachment').addEventListener('click',addAttachment);$('clear-mail').addEventListener('click',clearMail);$('inventory-search').addEventListener('input',renderInventory);
$('resource-form').addEventListener('submit',event=>{event.preventDefault();try{const resource=chosenResource();if(!resource)throw new Error('请选择要修改的资源或物品。');const uid=actionUid(),risks=itemRisks(resource);if(risks.length&&!window.confirm(`该物品有风险提示：\n${risks.map(code=>riskText(code)).join('\n')}\n\n仍要写入账号 ${uid} 的背包吗？`))return;submitOperation('resource',resourcePayload(uid,resource.key,$('resource-mode').value,$('resource-amount').value));}catch(error){showResult('resource',error.message,false);}});
$('mail-form').addEventListener('submit',event=>{event.preventDefault();try{const rows=[...$('attachments').children].map(row=>({cfgid:row.querySelector('.attachment-id').value,num:row.querySelector('.attachment-count').value}));submitOperation('mail',mailPayload(actionUid(),$('mail-title').value,$('mail-content').value,rows,$('mail-days').value));}catch(error){showResult('mail',error.message,false);}});
$('role-form').addEventListener('submit',event=>{event.preventDefault();try{submitOperation('role',rolePayload(actionUid(),$('role-picker').value));}catch(error){showResult('role',error.message,false);}});
$('pools-form').addEventListener('submit',event=>{event.preventDefault();submitPools(true);});$('pools-disable').addEventListener('click',()=>submitPools(false));$('pools-limited').addEventListener('click',()=>submitPools(true,true));$('access-form').addEventListener('submit',event=>{event.preventDefault();try{submitOperation('access',accessPayload(actionUid(),Object.fromEntries(['pools','activities','illustrations'].map(key=>[key,$('access-'+key).checked]))));}catch(error){showResult('access',error.message,false);}});
addAttachment();modeChanged();updateAvailability();refresh();loadCatalog();setInterval(refresh,10000);
</script></body></html>'''


def serve_control(handler, path: str):
    """Handle the two read-only GET routes; return False for all other paths."""
    route = urlsplit(path).path
    if route not in ("/control", "/control/", "/control/state"):
        return False
    if getattr(handler, "command", "GET") != "GET":
        return False
    if route == "/control/state":
        # Pass ROOT explicitly: the default argument binds the import-time path,
        # which is wrong for the frozen server, where control_http re-points ROOT.
        body = json.dumps(collect_state(ROOT), ensure_ascii=False, allow_nan=False).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    else:
        body = PAGE.replace("@ROOT@", html.escape(str(ROOT).replace("'", "''"))).encode("utf-8")
        content_type = "text/html; charset=utf-8"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
    return True


def _self_test():
    from contextlib import closing
    import hashlib
    import io
    import shutil
    import subprocess
    import tempfile
    import unittest
    from unittest.mock import patch

    class ControlTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory(prefix="crosscore-control-")
            self.root = Path(self.temp.name)
            self.database = self.root / "07-server/data/players.sqlite3"
            self.database.parent.mkdir(parents=True)

        def tearDown(self):
            self.temp.cleanup()

        def make_database(self, state=None):
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("CREATE TABLE accounts (uid INTEGER, account_name TEXT, created_at INTEGER, revision INTEGER, state_json TEXT)")
                if state is not None:
                    connection.execute("INSERT INTO accounts VALUES (1, 'local-test', 0, 2, ?)", (json.dumps(state),))

        def test_missing_and_empty_database(self):
            self.assertFalse(read_accounts(self.database)["available"])
            self.assertFalse(self.database.exists())
            self.make_database()
            result = read_accounts(self.database)
            self.assertTrue(result["available"])
            self.assertEqual(result["accounts"], [])
            self.assertIn("没有账号", result["message"])

        def test_offline_database_read_and_credentials_excluded(self):
            self.make_database({"player": {"name": "<script>测试</script>", "level": 3, "gold": 12,
                                          "diamond": 7, "token": "SECRET-TOKEN"},
                                "cards": [{"cid": 1, "cfgid": 71010, "name": "总队长", "level": 2}],
                                "progress": {"cleared_stages": [1001], "mainLine": [{"id": 1001, "star": 3}]},
                                "login": {"key": "SECRET-KEY", "ability_num": 9},
                                "inventory": {"10001": 12, "60101": 5, "SECRET-ITEM": "SECRET-VALUE"},
                                "offline_archive_pools": [1049, 1026, 1026],
                                "offline_unlock_all": True, "session": "SECRET-SESSION"})
            before = hashlib.sha256(self.database.read_bytes()).digest()
            result = collect_state(self.root, lambda port: {"listening": False, "error": "测试中服务未启动"})
            self.assertTrue(result["database"]["available"])
            self.assertTrue(all(not row["listening"] for row in result["services"]))
            account = result["database"]["accounts"][0]
            self.assertEqual((account["level"], account["gold"], account["card_count"]), (3, 12, 1))
            self.assertEqual(account["progress"]["cleared_count"], 1)
            self.assertEqual(account["resources"]["ability_num"], 9)
            self.assertEqual(account["inventory"], {"10001": 12, "60101": 5})
            self.assertEqual(account["offline_archive_pools"], [1026, 1049])
            self.assertIs(account["offline_unlock_all"], False)
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).digest())

        def test_malformed_save_and_schema_report_errors(self):
            self.make_database()
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("INSERT INTO accounts VALUES (1, 'local', 0, 0, '{bad')")
            self.assertIsNotNone(read_accounts(self.database)["accounts"][0]["error"])
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("DROP TABLE accounts")
            self.assertIsNotNone(read_accounts(self.database)["error"])

        def test_state_route_follows_module_root_and_lists_inventory(self):
            class Handler:
                command = "GET"
                def __init__(self):
                    self.wfile, self.headers = io.BytesIO(), {}
                def send_response(self, status): self.status = status
                def send_header(self, key, value): self.headers[key] = value
                def end_headers(self): pass
            self.make_database({"player": {"level": 1}, "inventory": {"60101": 7, "10004": 3}})
            # A frozen server re-points ROOT after import; the state route must
            # read that root, not the path captured when the module loaded.
            with patch(__name__ + ".ROOT", self.root), patch(
                    __name__ + ".probe_port", lambda port: {"listening": False, "error": None}):
                handler = Handler()
                self.assertTrue(serve_control(handler, "/control/state"))
            database = json.loads(handler.wfile.getvalue())["database"]
            self.assertTrue(database["available"])
            self.assertEqual(database["accounts"][0]["inventory"], {"60101": 7, "10004": 3})

        def test_live_wal_read_latest_commit_without_changes(self):
            self.make_database()
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("INSERT INTO accounts VALUES (1, 'local', 0, 0, ?)",
                                   (json.dumps({"player": {"level": 4}}),))
                connection.commit()
                wal = Path(str(self.database) + "-wal")
                before = (self.database.read_bytes(), wal.read_bytes())
                self.assertEqual(read_accounts(self.database)["accounts"][0]["level"], 4)
                self.assertEqual(before, (self.database.read_bytes(), wal.read_bytes()))

        def test_http_route_html_and_method(self):
            class Handler:
                command = "GET"
                def __init__(self):
                    self.wfile, self.headers = io.BytesIO(), {}
                def send_response(self, status): self.status = status
                def send_header(self, key, value): self.headers[key] = value
                def end_headers(self): pass
            handler = Handler()
            self.assertTrue(serve_control(handler, "/control?test=1"))
            self.assertEqual(handler.status, 200)
            self.assertEqual(int(handler.headers["Content-Length"]), len(handler.wfile.getvalue()))
            self.assertIn("本地控制端", handler.wfile.getvalue().decode("utf-8"))
            self.assertIn("当前账号背包", handler.wfile.getvalue().decode("utf-8"))
            self.assertIn("resource-warning", handler.wfile.getvalue().decode("utf-8"))
            self.assertNotIn(b"https://", handler.wfile.getvalue())
            self.assertFalse(serve_control(Handler(), "/other"))
            self.make_database()
            state = collect_state(self.root, lambda port: {"listening": False, "error": None})
            with patch(__name__ + ".collect_state", return_value=state):
                json_handler = Handler()
                self.assertTrue(serve_control(json_handler, "/control/state"))
                self.assertEqual(json.loads(json_handler.wfile.getvalue())["database"]["total"], 0)
                self.assertEqual(json_handler.headers["Cache-Control"], "no-store")
            handler.command = "POST"
            self.assertFalse(serve_control(handler, "/control/state"))

        def test_page_javascript_payload_limits_and_resource_display(self):
            node = shutil.which("node")
            if not node:
                self.skipTest("Node.js unavailable; page JavaScript check was not run")
            script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
            syntax = subprocess.run([node, "--check"], input=script, text=True,
                                    capture_output=True, timeout=15)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            # Do not evaluate the page's startup/listeners/fetch calls. Exercise
            # pure payload builders in an isolated VM with fixed public fields.
            functions = script.split("$('refresh').addEventListener", 1)[0]
            assertions = r'''
configureCatalog({resources:[{key:'gold',cfgid:10001,max:INT_MAX},{key:'hot',cfgid:10035,max:999,max_by_player_level:true},{key:'store_exp',cfgid:10003,max:INT_MAX},{key:'tp',max:3}],items:[{cfgid:10035,supported:true},{cfgid:10003,max:INT_MAX,mail_max:INT_MAX,supported:true},{cfgid:100011,name:'气象罕见星尘',max:9999,mail_max:9999,supported:true},{cfgid:60101,max:1000,supported:true},{cfgid:10998,name:'粲晶',supported:true,risks:['domain'],mail_supported:false},{cfgid:10059,name:'复归五日构建礼包',supported:true,mail_supported:true,mail_max:100},{cfgid:60102,supported:true,mail_supported:false},{cfgid:60103,name:'兼容旧目录物品'}],roles:[{cfgid:71020,supported:true}],pools:[{id:1026,archivable:true,limited:true},{id:1,archivable:false}],risk_labels:{domain:'独立域管理',type:'类型风险'}});
snapshot={database:{accounts:[{uid:1,resources:{gold:12,hot:82,store_exp:1000,tp:3},inventory:{'10035':0,'60101':5,'100011':7777,'10004':3},cards:[]}]}};selected='1';
assert.strictEqual(integer('2147483647','test'),2147483647);
assert.strictEqual(integer('-2147483647','test'),-2147483647);
for(const value of ['2147483648','-2147483648','1.5','1e3',true])assert.throws(()=>integer(value,'test'));
assert.strictEqual(resourcePayload(1,'gold','set',INT_MAX).amount,INT_MAX);
assert.strictEqual(resourcePayload(1,'gold','add',-12).amount,-12);
assert.throws(()=>resourcePayload(1,'gold','add',-13));
assert.throws(()=>resourcePayload(1,'gold','add',INT_MAX));
assert.throws(()=>resourcePayload(1,'hot','set',1000));
assert.throws(()=>resourcePayload(1,'item:60101','set',1001));
assert.strictEqual(resourcePayload(1,'item:10998','set',1).key,'item:10998');
assert.deepStrictEqual(resourceMetadata('item:10998').risks,['domain']);
assert.strictEqual(riskText('domain'),'独立域管理');
assert.strictEqual(riskText('not-a-code'),'not-a-code');
assert.strictEqual(itemRisks(undefined).length,0);
assert.strictEqual(resourcePayload(1,'item:10059','set',1).amount,1);
assert.strictEqual(itemMap.has(10059),true);
assert.strictEqual(mailItemMap.has(10059),true);
assert.strictEqual(mailPayload(1,'复归礼包','',[{cfgid:10059,num:1}],30).attachments[0].cfgid,10059);
assert.strictEqual(matches(catalog.mailItems,'10059')[0].name,'复归五日构建礼包');
assert.strictEqual(matches(catalog.items,'10059').length,1);
assert.deepStrictEqual(inventoryRows(currentAccount(),'').map(row=>row.cfgid),[100011,60101,10004]);
assert.deepStrictEqual(inventoryRows(currentAccount(),'10004').map(row=>row.cfgid),[10004]);
assert.deepStrictEqual(inventoryRows(currentAccount(),'气象').map(row=>row.cfgid),[100011]);
assert.deepStrictEqual(inventoryRows(currentAccount(),'zzz').map(row=>row.cfgid),[]);
assert.deepStrictEqual(inventoryRows(null,''),[]);
assert.deepStrictEqual(inventoryRows({error:'坏存档',inventory:{'1':1}},''),[]);
assert.strictEqual(resourcePayload(1,'item:60102','set',1).amount,1);
assert.throws(()=>mailPayload(1,'x','',[{cfgid:60102,num:1}],1));
assert.strictEqual(mailPayload(1,'x','',[{cfgid:60103,num:1}],1).attachments[0].cfgid,60103);
assert.strictEqual(resourceBalance(currentAccount(),resourceMetadata('hot')),82);
assert.strictEqual(resourceBalance(currentAccount(),resourceMetadata('item:10035')),82);
assert.strictEqual(resourceBalance(currentAccount(),resourceMetadata('store_exp')),1000);
assert.strictEqual(resourceBalance(currentAccount(),resourceMetadata('item:60101')),5);
const mail=mailPayload(1,'测试','',[{cfgid:60101,num:2},{cfgid:60101,num:3}],3650);
assert.strictEqual(mail.content,'');assert.strictEqual(mail.attachments.length,1);assert.strictEqual(mail.attachments[0].num,5);assert.strictEqual(mail.expires_days,3650);
assert.strictEqual(mailPayload(1,'x'.repeat(256),'x'.repeat(16000),[{cfgid:60101,num:995}],1).attachments[0].num,995);
assert.throws(()=>mailPayload(1,'x','',[{cfgid:60101,num:996}],1));
assert.throws(()=>mailPayload(1,'x','',[{cfgid:100011,num:22222}],1),/9,999/);
assert.strictEqual(mailItemLimit(100011,currentAccount()).remaining,2222);
assert.strictEqual(mailPayload(1,'x','',[{cfgid:100011,num:2222}],1).attachments[0].num,2222);
assert.throws(()=>mailPayload(1,'x','',[{cfgid:100011,num:2223}],1),/最多还能领取 2,222/);
assert.throws(()=>mailPayload(1,'x','',[{cfgid:100011,num:1111},{cfgid:100011,num:1112}],1));
assert.throws(()=>mailPayload(1,'x','',[{cfgid:10059,num:101}],1),/100/);
assert.strictEqual(mailPayload(1,'x','',[{cfgid:10059,num:100}],1).attachments[0].num,100);
assert.strictEqual(mailPayload(1,'x','',[{cfgid:10003,num:99999999},{cfgid:10003,num:99999999}],1).attachments[0].num,199999998);
assert.throws(()=>mailPayload(1,'x','',[{cfgid:10003,num:INT_MAX-999}],1));
assert.strictEqual(mailItemLimit(10035,currentAccount()).remaining,undefined); // No guessed level cap.
assert.strictEqual(clearMailPayload(1).uid,1);assert.throws(()=>clearMailPayload(0));
let sequence=0;globalThis.crypto={randomUUID:()=>String(++sequence)};
const mailRetry=operationRecord('mail',{uid:1}),clearRetry=operationRecord('clear_mail',{uid:1});
assert.notStrictEqual(mailRetry.request_id,clearRetry.request_id);
assert.strictEqual(operationRecord('clear_mail',{uid:1}).request_id,clearRetry.request_id);
const clearPosts=[];let clearPrompt='';submitOperation=(kind,payload)=>clearPosts.push({kind,payload});
globalThis.window={confirm:message=>{clearPrompt=message;return false;}};
clearMail();assert.strictEqual(clearPosts.length,0);
assert.match(clearPrompt,/UID 1/);assert.match(clearPrompt,/未领取附件/);assert.match(clearPrompt,/回执保留/);
window.confirm=()=>true;clearMail();
assert.strictEqual(clearPosts.length,1);assert.strictEqual(clearPosts[0].kind,'clear_mail');
assert.strictEqual(clearPosts[0].payload.uid,1);
assert.strictEqual(mailPayload(1,'中'.repeat(85),'',[],1).title.length,85);
assert.strictEqual(mailPayload(1,'😀'.repeat(64),'',[],1).title.length,128);
assert.throws(()=>mailPayload(1,'中'.repeat(86),'',[],1));
assert.throws(()=>mailPayload(1,'x'.repeat(257),'',[],1));
assert.throws(()=>mailPayload(1,'x','中'.repeat(5334),[],1));
assert.throws(()=>mailPayload(1,'x','bad\u0000text',[],1));
assert.throws(()=>mailPayload(1,'\ud800','',[],1));
assert.throws(()=>mailPayload(1,'x','',[],3651));
assert.throws(()=>mailPayload(1,'x','',[{cfgid:60101,num:2147483648}],1));
assert.throws(()=>mailPayload(1,'x','',[{cfgid:60101,num:INT_MAX},{cfgid:60101,num:1}],1));
assert.throws(()=>mailPayload(1,'x','',Array.from({length:21},()=>({cfgid:60101,num:1})),1));
assert.throws(()=>mailPayload(1,'x','',[{cfgid:10998,num:1}],1));
assert.strictEqual(rolePayload(1,71020).cfgid,71020);
assert.throws(()=>rolePayload(2147483648,71020));
assert.throws(()=>rolePayload(1,10998));
assert.strictEqual(poolsPayload(1,[1026,1026],true).pool_ids.length,1);
assert.throws(()=>poolsPayload(1,[1],false));
assert.strictEqual(accessPayload(1,{pools:false}).policy.pools,false);
assert.strictEqual(accessPayload(1,{pools:true}).policy.pools,true);
console.log('page payload and display checks passed');
'''
            harness = ("const vm=require('node:vm'),assert=require('node:assert/strict');"
                       "const context={assert,TextEncoder,console};vm.createContext(context);"
                       "vm.runInContext(" + json.dumps(functions + assertions) + ",context);")
            checked = subprocess.run([node], input=harness, text=True,
                                     capture_output=True, timeout=15)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("page payload and display checks passed", checked.stdout)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ControlTests)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        raise SystemExit(0 if _self_test() else 1)
    parser.print_help()
