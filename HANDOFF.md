# 交接说明（HANDOFF）

> 这份文件是给「新会话 / 压缩上下文后」快速恢复状态用的。
> 它记录的是**当前真实状态与踩过的坑**，不是开发计划。
> 最后更新：v1.2.0 已发布并部署。

## 一句话现状

插件已完成并上线：**v1.2.0**，已推送到 GitHub，已部署到服务器，QQ 内可正常出图。
工作区干净，无残留临时文件。测试 222 个全绿，ruff 干净。

## 项目位置

| 项目 | 值 |
| --- | --- |
| 本地路径 | `H:\Hermes Project\AstrBot_plugin\astrbot_plugin_steam_deal_card` |
| GitHub | https://github.com/YouYi5213/astrbot_plugin_steam_deal_card （公开） |
| 服务器 | `81.71.72.19:22`，`root` / `ATong5213` |
| 插件目录（宿主机） | `/www/wwwroot/data/plugins/astrbot_plugin_steam_deal_card` |
| 容器 | `astrbot`，挂载 `/www/wwwroot/data -> /AstrBot/data` |
| 运行环境 | python 3.12.13，httpx 0.28.1，AstrBot 4.28.0，QQ 走 napchat/aiocqhttp |

**重要**：GitHub 账号是 **YouYi5213**（不是 BestATong）。
部署用 **paramiko + SFTP 直传**（本机没有 ssh.exe/plink/sshpass）；
**不要用 gh-proxy.com 镜像**，它曾返回过旧文件。

## 命令一览

```
steam游戏 <游戏名|appid|Steam链接>     查当前价/史低/评价/封面
steam游戏 <序号>                       从候选列表里选
steam打折 [数量]                       当前促销列表
steam在线 <游戏名|appid>               单个游戏实时在线人数
steam热度 [数量]                       在线人数榜（按实时人数降序）
```

别名：`steam游戏查询`/`steam查价`/`steam价格`；`steam特惠`/`steam促销`/`steam优惠`；
`steam在线人数`/`steam人数`；`steam热度榜`/`steam排行`/`steam在线榜`。

## 必须知道的坑（都踩过，别再踩）

### 1. 命令必须用 `filter.regex`，不能用 `filter.command`
`CommandFilter` 要求 `event.is_at_or_wake_command`（即必须带 wake_prefix / @机器人 / 私聊）。
该部署 `wake_prefix = ["月明夜"]`，所以裸命令 `steam游戏 泰拉瑞亚` 会被**静默忽略且无日志**。
`RegexFilter` 不受此限制。测试里用 AST 锁死了这一点。

### 2. 发图必须走 `chain_result` + `Comp.Image(file="base64://...")`
`event.image_result("base64://...")` 会把参数交给媒体解析器**当文件路径**，
产生 `/AstrBot/base64:/iVBOR...` → `OSError: [Errno 36] File name too long`。
正确写法见 `main.py` 的 `_image_result()`。测试禁止再出现 `.image_result(` 调用。

### 3. Steam 热门榜不是实时人数
`ISteamChartsService/GetMostPlayedGames` 返回 `peak_in_game`（**当日峰值**）。
实测两者排序差异极大（GTA V 峰值第 8 / 实时第 34；Rust 峰值第 26 / 实时第 13），
比值 0.26~1.01 浮动，**无法换算**。
所以榜单**只用来挑候选**，排序一律用 `ISteamUserStats/GetNumberOfCurrentPlayers`
逐游戏查实时值后重排。峰值仅作参考单独标注。

### 4. 国内服务器主机连通性
- `api.steampowered.com`：经常不通（连接挂起）→ 必须回退 `api.steamchina.com`
- `api.steamchina.com`：快（~0.36s），但**不返回 `main_capsule` 文件名** → 需要 capsule 候选链
- `store.steampowered.com/search/results?specials=1`：时通时断（实测 6 次成 2 次）→ 5 次重试 + 6 秒短超时
- `store.steamchina.com`：可达但只有 37 条蒸汽平台商品，**不能替代全量**

### 5. 时区：优先 zoneinfo，失败回退固定 +08:00
`zoneinfo` 需要系统 tzdata，**精简镜像常缺**（我本机就缺）。
读取失败时回退 `timezone(timedelta(hours=8))`。
中国自 1991 年起全境统一 +08:00 且无夏令时，故回退是**精确等价**。

### 6. 其它工程约定
- 中文游戏名靠**小黑盒**搜索接口转 appid（Steam 搜索不认中文）
- 用 SW 写中文会乱码：**写文件一律用 write 工具**，别用 pwsh 的 `Set-Content`
- 测试里 CJK 字面量写成 `\uXXXX` 转义，防止被非 UTF-8 工具改写
- 测试文件里 `LookupError` 是**插件自己的异常**（继承 RuntimeError），
  与 builtin 同名但**不是同一个类** —— 必须 `from ...service import LookupError`
- pwsh 里 unittest 即使 `OK` 也会 `[exit code: 1]`（stderr 被当成错误），看 `Ran N / OK` 行

## 本地开发命令

```bash
cd astrbot_plugin_steam_deal_card
python -m unittest discover -s tests -t .
python -m ruff check .
python -m ruff format --check .
```

## 文件结构

| 文件 | 作用 |
| --- | --- |
| `main.py` | 入口、正则命令注册、`_image_result()`、待选会话管理 |
| `steam_api.py` | Steam + 小黑盒 HTTP 客户端、主机故障转移、解析器 |
| `service.py` | 编排：解析游戏、取卡、打折、在线人数、渲染 |
| `models.py` | `GameCard` / `DealItem` / `PlayerCount` 等 dataclass |
| `render.py` | Pillow 渲染三张卡片 + 北京时间处理 |
| `name_match.py` | 中英跨语言匹配打分 |
| `tests/` | 192 个测试；`test_command_binding.py` 用 AST 锁命令契约 |

## 待办 / 未做

- 用户测试：还没确认 QQ 内实际发出的图是否正常（我无法自己发 QQ 消息）
- `steam热度` 候选取自热门榜前 100，是「热门游戏内的排行」而非全站绝对前 N
- 在线人数只统计通过 Steam 启动的玩家（不含独立客户端/主机/离线）
- Nintendo 相关功能：用户明确说**不做**
- SteamTools(Watt Toolkit) 代理：用户明确说**先不用**

## 发布流程（每次改代码）

1. 改代码 → `python -m unittest discover -s tests -t .` + `ruff check/format`
2. 同步 `main.py` 的 `PLUGIN_VERSION` 与 `metadata.yaml` 的 `version`
3. `CHANGELOG.md` 加条目
4. `git add -A && git commit && git push origin main`
5. SFTP 传 `main.py/render.py/models.py/service.py/steam_api.py/metadata.yaml/CHANGELOG.md/_conf_schema.json`
6. `rm -rf <插件目录>/__pycache__`
7. `docker restart astrbot`，等约 28 秒
8. `docker logs astrbot --tail 400 | grep -i steam_deal_card` 确认版本号
