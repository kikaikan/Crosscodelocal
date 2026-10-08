"""Generate reviewable original progression gates and atlas product mapping."""
import json
from pathlib import Path
from handlers.initialization import config_table
from handlers.progression import STAGES
from handlers.shop import catalog, commodities

HERE=Path(__file__).resolve().parent


def main():
    rules=config_table('cfgCfgOpenRules.lua')
    lines=['# 原版功能开放条件', '', '依据当前保存客户端的 CfgOpenCondition、CfgOpenConditionMore、CfgOpenRules。主线和普通功能不接受专项开放绕过。', '',
           '| 功能 | 配置键 | 原始条件（全部满足） |','|---|---|---|']
    for table in ('cfgCfgOpenCondition.lua','cfgCfgOpenConditionMore.lua'):
        for key,cfg in sorted(config_table(table).items()):
            descriptions=[]
            for rid in cfg.get('conditions',[]):
                rule=rules[rid]; kind,value=rule['type'],rule['val']
                if kind==1: description=f'指挥官等级 ≥ {value}'
                elif kind==2:
                    stage=STAGES.get(str(value),{})
                    description=f"通关 {stage.get('chapterID','')} {stage.get('name','')}（ID {value}）"
                elif kind==3: description=f'完成引导 {value}'
                else: description=f'未知条件 {rid}（保持关闭）'
                if rule.get('openTime'): description+='，按配置星期开放'
                descriptions.append(description)
            lines.append('| '+cfg.get('sName',key)+' | '+str(key)+' | '+('；'.join(descriptions) or '无额外配置条件')+' |')
    pictures=catalog('cfgCfgArchiveMultiPicture.lua');items=catalog('cfgItemInfo.lua')
    rows=[]
    for cfg in commodities().values():
        if cfg.get('group')!=5: continue
        for item,num,kind in cfg['jGets']:
            info=items[item];pic=pictures[info['dy_value1']]
            if pic['itemId']!=item or info['type']!=16 or kind!=2:
                raise ValueError('Atlas ownership mapping mismatch')
            rows.append({'commodity':cfg['id'],'item':item,'picture':pic['id'],
                         'name':cfg.get('sName'),'costs':cfg.get('jCosts',[]),
                         'image':pic.get('img'),'icon':pic.get('icon'),'live2d':pic.get('l2dName')})
    (HERE/'data/progression-gates.md').write_text('\n'.join(lines)+'\n','utf-8')
    (HERE/'data/illustration-catalog-evidence.json').write_text(json.dumps({'count':len(rows),'source':'device-luascripts','products':rows},ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'gate_rows':len(lines)-5,'illustration_products':len(rows)}))


if __name__=='__main__': main()
