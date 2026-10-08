"""Audit implemented registrations against recovered client requests, without importing handlers."""
import ast
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def display_path(path):
    """仓库内文件用正斜杠相对路径；仓库外只保留文件名。

    生成物要进版本库，仓库外路径（例如测试用临时目录）可能带本机用户名与绝对目录，
    因此这里绝不输出本机绝对路径。parity_report.py 的同名函数保持同样语义。
    """
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(ROOT)).replace('\\','/')
    except ValueError:
        return '<outside>/' + resolved.name


def module_string_constants(tree):
    """收集模块级 `NAME = '字符串字面量'`，供 @register(NAME) 解析。

    只认“单目标赋值 + 字符串字面量”这一种确定性写法。推导式、拼接、f-string、
    其它名字或函数参数一律不解析——解析不了就跳过，不做任何猜测。
    """
    constants={}
    for node in tree.body:
        if (isinstance(node,ast.Assign) and len(node.targets)==1
                and isinstance(node.targets[0],ast.Name)
                and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)):
            constants[node.targets[0].id]=node.value.value
    return constants


def scan_implementations(paths):
    """扫描源码得到 {request_name: {file,line,function}}；重复静态登记抛 ValueError。"""
    implementations={}
    for path in paths:
        tree=ast.parse(Path(path).read_text(encoding='utf-8-sig'))
        constants=module_string_constants(tree)
        # initialization.py explicitly registers each bounded ReadSpec key.
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='READS' for t in node.targets) and isinstance(node.value,ast.Dict):
                for key,value in zip(node.value.keys,node.value.values):
                    if isinstance(key,ast.Constant) and isinstance(key.value,str) and isinstance(value,ast.Call) and isinstance(value.func,ast.Name) and value.func.id=='ReadSpec':
                        implementations[key.value]={'file':display_path(path),
                                                    'line':key.lineno,'function':'_register_read'}
        for node in ast.walk(tree):
            if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Name) and decorator.func.id=='register'):
                    continue
                if len(decorator.args)!=1:
                    continue
                argument=decorator.args[0]
                if isinstance(argument,ast.Constant):
                    name=argument.value
                elif isinstance(argument,ast.Name):
                    # 只有模块级 NAME = '字面量' 才解析；函数参数等一律跳过。
                    name=constants.get(argument.id)
                else:
                    name=None
                if name is None:
                    continue
                if name in implementations:
                    raise ValueError('Duplicate static registration: '+name)
                implementations[name]={'file':display_path(path),
                                       'line':node.lineno,'function':node.name}
    return implementations


def main():
    schema=json.loads((ROOT/'05-protocol/endpoints.json').read_text(encoding='utf-8'))
    entries=schema.get('schemas',schema.get('endpoints'))
    if isinstance(entries,dict):
        entries=list(entries.values())
    if not isinstance(entries,list):
        raise ValueError('Unrecognized schema catalog')
    paths=[ROOT/'07-server/server_core.py',*sorted((ROOT/'07-server/handlers').glob('*.py'))]
    implementations=scan_implementations(paths)
    requests=[]
    for entry in entries:
        observed=entry.get('observed',{})
        if not entry.get('client_send_literal') and 'c2s' not in observed.get('directions',[]):
            continue
        requests.append({'name':entry['name'],'opcode':entry['opcode'],
                         'observed_request':'c2s' in observed.get('directions',[]),
                         'implementation':implementations.get(entry['name']),
                         'runtime_verified':False})
    report={'purpose':'Static registration inventory, not functional equivalence or client acceptance',
            'registered_handlers':len(implementations),'known_client_requests':len(requests),
            'registered_request_count':sum(r['implementation'] is not None for r in requests),
            'requests':requests,'registrations':implementations}
    target=ROOT/'90-notes/feature-coverage.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['registered_handlers','known_client_requests','registered_request_count']}))


if __name__=='__main__':
    main()
