"""Pin equipment source evidence without reading or changing player saves."""
import hashlib
import json
from pathlib import Path
from seed_generator import LUA_DIR
from equip_service import array_records
HERE=Path(__file__).resolve().parent
FILES=('EquipProto.lua','EquipData.lua','EquipMgr.lua','EquipCalculator.lua','StuffArray.lua',
       'CharacterCardsData.lua','GameMsg.lua','GEnum.lua','cfgCfgEquip.lua',
       'cfgCfgEquipExp.lua','cfgCfgMaterialEquip.lua','cfgCfgEquipExpRand.lua',
       'cfgCfgEquipSkill.lua','cfgCfgCardPropertyEnum.lua','cfgCfgLifeBuffer.lua')
def generate():
    factors=list(array_records('cfgCfgEquipExpRand.lua').values())
    source={f:{'sha256':hashlib.sha256((LUA_DIR/f).read_bytes()).hexdigest(),
               'bytes':(LUA_DIR/f).stat().st_size} for f in FILES}
    return {'schema_version':1,'source':source,
            'strengthening':{'item':10021,'item_exp':1,'gold_per_item_exp':5,
                             'max_selected_materials':10,'initial_chip_level':0,
                             'critical_factors':[{k:r[k] for k in ('id','fRand','nWeight')} for r in factors],
                             'random_skill_changes':False},
            'write_protocols':[2721,2737,2742,2712,2719,2727],
            'validation':'static_source_and_isolated_wire_tests; actual chip UI pending'}
if __name__=='__main__':
    path=HERE/'data/equipment-source.json'
    path.write_text(json.dumps(generate(),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(path)
