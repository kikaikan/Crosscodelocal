# CrossCore 本地部署服务端实现 _（Crosscodelocat / crosscore-ps）_

官方停服后，用你自己合法持有的客户端副本在本地离线游玩的实现。

它提供本机双 TCP 服务端（查询 / 游戏）与资源、控制 HTTP，让自备的客户端在本地完成登录与游玩；
业务按 `@register('Protocol:Message')` 插件注册，未实现的请求回 `SystemProto:Tips` 并保持连接，不伪造成功回包。

充值支付不实现；通关、星数、奖励与资产按真实操作写入本地存档。仓库只包含代码与文档，
不含任何官方素材，也不含修改或重打包官方客户端的工具。

## 安装

准备好下节的依赖之后，一条命令启动（需要 Windows 与 Python 3.13；服务端本身只用标准库）：

```powershell
.\start_local.ps1          # 或双击 start_local.cmd
```

启动后监听**资源 / 控制 HTTP 18080、查询 TCP 19001、游戏 TCP 19041**，默认只绑定 `127.0.0.1`。
`stop_local.ps1` 只停止启动器记录并核对过身份的本项目进程。

### 依赖

以下输入因版权与体积原因不随仓库分发，需要你自备；**缺任何一项都会在启动或测试阶段直接报错**：

| 需要的输入 | 放在哪里 | 怎么来 |
|---|---|---|
| 客户端解包出的 Lua | `03-unpack/lua/device-luascripts/` | 解包你自己持有的官方客户端；生成脚本写死这个相对路径 |
| 报文结构定义 | `05-protocol/endpoints.json` | `python -B 02-tools/scripts/protocol_codec.py --extract-schema` |
| 运行期数据表 | `07-server/data/*.json` | 按下面的顺序逐个生成 |
| 资源归档与服务器列表样本 | `01-device/`、`05-protocol/samples/` | 自备；缺失时启动器的资源服务会失败 |

在**仓库根目录**按顺序生成运行期输入（顺序不能颠倒，也不能漏；先给当前会话加上工具路径）：

```powershell
$env:PYTHONPATH = "$PWD/02-tools/scripts"

python -B 02-tools/scripts/protocol_codec.py --extract-schema      # 1  → 05-protocol/endpoints.json
python -B 07-server/data/gacha-build.py                           # 2  → gacha-*.json（9 个）
python -B 07-server/data/card-skill-types-build.py                # 3
python -B 07-server/data/progression-build.py                     # 4  → 21 个 progression-*/battle-*.json
python -B 07-server/data/admin-build.py                           # 5  → admin-*.json
python -B 07-server/data/battle-tactical-build.py                 # 6  需要第 4 步的产物
python -B 07-server/data/plr-ability-build.py                     # 7
python -B 07-server/data/plr-skill-group-build.py                 # 8
python -B 07-server/generate_skin_catalog.py                      # 9  需要 06-client/offline-resources/board-interaction-policy.json
python -B 07-server/seed_generator.py --output 07-server/data/new_account_seed.json   # 10  必须显式 --output
python -B 07-server/data/gifts-build.py                           # 11 需要第 5 步的产物与 PYTHONPATH
python -B 02-tools/scripts/item-pool-build.py                     # 12
python -B 02-tools/scripts/sub-talent-build.py                    # 13
python -B 07-server/generate_access_evidence.py                   # 14 需要 2/4/10/12 的产物
python -B 07-server/generate_equipment_evidence.py                # 15
python -B 02-tools/scripts/collection-unlock-build.py             # 16 需要 2/4/5 的产物
python -B 02-tools/scripts/wait_contracts.py                      # 17 可选
```

> [!WARNING]
> 第 10 步必须带 `--output 07-server/data/new_account_seed.json`。不带的话它会落到 `<仓库根>/data/`，
> 在仓库根新建一个 `data/` 目录，此后所有数据路径都被带偏，第 14/16 步会去找不存在的文件而失败。

### 免安装 exe（可选）

目标机器不需要安装 Python。构建（PyInstaller 6.x）并把可运行目录一次铺好：

```powershell
python -B 02-tools/scripts/build_server_exe.py --stage dist\run
.\dist\run\CrossCorePS-Server.exe
```

产物是单文件 `dist/CrossCorePS-Server.exe`。数据表与 `endpoints.json` **按设计不进 exe**：
`--stage` 把产物复制到目标目录，并用目录联结（Windows 下不需要管理员权限）把 `data/`、`05-protocol/`、
`03-unpack/` 挂到同级，目录里已存在的内容一律保留。exe 用自身所在目录解析这些路径。

## 使用

启动后客户端登录即可游玩，日常操作用控制页：

- 查看与控制：`http://127.0.0.1:18080/control`。选中账号后可看到资源与背包，按 `item:<cfgid>` 增、减或设为指定数量。
- 日志：运行 `view_logs.ps1` 打开实时窗口；事件同时写入 `07-server/logs/server.jsonl`。
- 停止：`stop_local.ps1`。
- 模拟器通过 `10.0.2.2` 访问本机服务。

### 命令行

服务端入口就是命令行程序，单模块调试与自检：

```powershell
python -B 07-server/server_core.py --help                          # 全部参数
python -B 07-server/server_core.py --handler handlers.gacha        # 只加载一个业务模块
python -B 07-server/server_core.py --check-imports '["handlers.gacha"]'   # 自检：导入指定模块并报告失败项
```

## 配置

服务端参数（`python -B 07-server/server_core.py --help`）：

| 参数 | 作用 |
|---|---|
| `--bind` / `--client-host` | 监听地址（默认 `127.0.0.1`）/ 下发给客户端的地址（默认 `10.0.2.2`） |
| `--query-port` / `--game-port` | 查询 / 游戏 TCP 端口，默认 19001 / 19041 |
| `--control-port` | 资源 / 控制 HTTP 端口，默认 18080；`0` 关闭（源码方式默认关闭） |
| `--static-dir` | 纯文件资源根，按 `/cross/release/...` 提供 |
| `--schema` / `--seed` / `--database` / `--log` | 报文定义、账号模板、存档、日志路径 |
| `--handler MODULE` | 指定业务模块，可重复；省略则加载全部默认模块 |
| `--progression-gates` `--restrict-pools` `--restrict-activities` `--restrict-illustrations` | 保留原版进度 / 卡池 / 活动 / 插画门槛 |

需要设备的脚本不写死 adb 路径，按 `--adb` → `CROSSCORE_ADB` → `ADB` → `PATH` → 常见 SDK / 模拟器目录
（版本号用通配）依次查找；显式指定的路径不可用时直接报错，不会静默回落。

## 目录结构

| 路径 | 内容 |
|---|---|
| `07-server/` | 服务端：asyncio 双 TCP + 资源 / 控制 HTTP，业务按 `@register('Proto:Message')` 插件注册 |
| `02-tools/scripts/` | 数据生成、协议编解码、审计与门禁 |
| `00-official/baseline.json` | 官方客户端基线指纹（只读输入；仓库里唯一与官方客户端相关的文件） |
| `start_local.ps1` / `.cmd`、`stop_local.ps1` / `.cmd`、`view_logs.ps1` | 启动、停止、日志窗口 |
| `03-unpack/`、`05-protocol/`、`04-capture/`、`06-client/` | 自备只读输入与本地工具产物，不随仓库分发 |
| `90-notes/`、`07-server/reports/` | 门禁与审计的本地输出，不随仓库分发 |

## 许可证

MIT，版权人 `crosscore-ps contributors`，见 [LICENSE](LICENSE)。
官方素材不在授权范围内。若权利方对本仓库有异议，请联系移除。
