# AstrBot Steam 游戏价格卡片

无需 API Key 的 AstrBot 插件：以**图片**查询 Steam 游戏的当前价格、历史最低价、
玩家评价和商店图片，并可以图片列出当前正在打折的游戏。

中文游戏名会自动转换成 Steam 能识别的名称；如果有多个候选，插件会给出带编号的
图片列表，回复序号即可选择。

## 功能

- **中文名自动转换**：`泰拉瑞亚` → `Terraria`（appid 105600），无需 LLM、无需 API Key。
- **多候选选择**：`生化危机` 这类不唯一的名称会列出编号选项，回复 `1` 即可查询。
- **图片卡片**：游戏详情卡包含封面图、当前价、原价与折扣、史低、距史低差额、
  简介、评价、发行日期与开发商。查询游戏时会**在同一条消息内**附带可点击的链接
  （图片内的链接无法点击，因此不画在图上），其中「商品详情」指向国内可访问的小黑盒页面。
- **打折列表**：列出当前促销游戏，含商品展示图、游戏名、当前价、折扣、史低和折扣结束日期。
- **在线人数**：查询单个游戏的**实时**在线人数，或按实时人数从高到低查看热度排行，
  卡片带游戏封面缩略图。
- **即将推出**：Steam 商店首页「热门即将推出」货架，含封面、价格与**发售日期**。
- **评价信息**：好评率、评测数量和「好评如潮」等 Steam 评价标签。
- **无需 Key**：只使用 Steam 商店与公开接口，不接入 ITAD，不需要 Steam Web API Key。

## 安装

在 AstrBot 管理面板中从 GitHub 仓库安装：

```text
https://github.com/YouYi5213/astrbot_plugin_steam_deal_card
```

也可以将仓库克隆到：

```text
AstrBot/data/plugins/astrbot_plugin_steam_deal_card
```

依赖 `httpx` 与 `Pillow`，安装插件时会自动安装。

## 命令

```text
steam游戏 <游戏名|appid|Steam链接>
steam游戏 <序号>          # 从上一条候选列表中选一个
steam打折 [数量]
steam在线 <游戏名|appid>
steam热度 [数量]
steam即将推出 [数量]
```

示例：

```text
steam游戏 泰拉瑞亚          # 中文名自动转换为 Terraria
steam游戏 Terraria         # 英文名同样支持
steam游戏 黑神话悟空
steam游戏 105600           # 直接按 appid 查询
steam游戏 https://store.steampowered.com/app/105600/
steam打折                  # 默认 10 款
steam打折 15               # 显示 15 款
steam在线 泰拉瑞亚           # 单个游戏的实时在线人数
steam在线 730              # 也可以按 appid
steam热度                  # 在线人数榜，默认 20 款
steam热度 10               # 只看前 10
steam即将推出              # Steam 商店「热门即将推出」货架
```

别名：

- `steam游戏`：`steam游戏查询`、`steam查价`、`steam价格`
- `steam打折`：`steam特惠`、`steam促销`、`steam优惠`
- `steam在线`：`steam在线人数`、`steam人数`
- `steam热度`：`steam热度榜`、`steam排行`、`steam在线榜`
- `steam即将推出`：`steam即将发售`、`steam预售`、`steam未发售`

### 多候选选择

当游戏名对应多个 Steam 条目时，插件会返回一张编号列表图片：

```text
steam游戏 生化危机
```

回复序号即可查询对应游戏：

```text
steam游戏 1
```

候选列表在 **5 分钟**内有效；也可以直接重新发送完整游戏名。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `timeout_seconds` | `20` | HTTP 请求超时时间，单位为秒 |
| `country` | `CN` | Steam 商店区域代码，影响价格与货币 |
| `language` | `schinese` | Steam 商店语言，影响游戏名与描述语言 |
| `history_country` | `cn` | 小黑盒历史价格区域，用于查询史低 |
| `max_deals` | `10` | `steam打折`、`steam即将推出` 默认显示数量 |
| `max_players` | `20` | `steam热度` 在线人数榜默认显示数量 |

## 数据来源

| 数据 | 来源 |
| --- | --- |
| 当前价格、折扣、折扣结束时间、评价、商店图 | Steam 商店公开接口 |
| 中文游戏名 → Steam appid | 小黑盒公开搜索接口 |
| 历史最低价 | 小黑盒公开历史价格接口 |
| 实时在线人数 | Steam `ISteamUserStats/GetNumberOfCurrentPlayers` |
| 热度榜候选与峰值 | Steam `ISteamChartsService/GetMostPlayedGames` |
| 热门即将推出榜 | Steam 商店首页「热门即将推出」货架（内嵌于首页 HTML） |

折扣结束时间来自 Steam 返回的 `discount_end_date`，是商店公布的正式结束时间。

## 实现说明

- **中文名转换**：Steam 商店搜索接口不识别中文（`泰拉瑞亚` 返回 0 条结果），
  因此插件改用小黑盒搜索接口把中文名解析成 appid，再向 Steam 查询价格与评价。
  整个过程不需要 LLM，也不依赖任何密钥。
- **跨语言匹配**：小黑盒返回的是本地化名称（`Terraria` 在中文语境下叫 `泰拉瑞亚`），
  所以插件会同时用「小黑盒名称 + Steam 名称」两套拼写做匹配打分，
  保证 `Terraria` 和 `泰拉瑞亚` 都能命中同一个 appid。
- **批量查询**：价格、评价和图片通过 `IStoreBrowseService/GetItems` 一次性批量获取
  （50 个 appid 约 1.4 秒），因此打折列表不会因为逐个查询而变慢。
- **在线人数与热度榜**：Steam 的热门榜接口返回的是**当日峰值** `peak_in_game`，
  不是实时人数。实测两者的排序差异很大（`GTA V` 峰值第 8、实时第 34；
  `Rust` 峰值第 26、实时第 13），比值在 0.26 ~ 1.01 之间浮动，无法换算。
  因此榜单**只用来挑选候选游戏**，排序一律用 `GetNumberOfCurrentPlayers`
  的实时值逐游戏查询后重新排序；榜单峰值作为参考信息单独标注，绝不当作实时人数展示。
  实时人数需要逐个请求，因此并发上限为 12：实测 20 个约 2 秒，50 个约 2.4 秒。
- **字体**：按 Windows / Linux / macOS 常见中文字体顺序自动查找，找不到时回退到
  Pillow 默认字体。如需在精简系统上使用，可将字体放到 `assets/fonts/`。
- **接口自检**：启动时会在后台探测各依赖（Steam 商店数据、小黑盒中文名转换、
  Steam 热门榜、Steam 在线人数）并写入日志。出问题时可直接看日志定位，
  不必等用户反馈「没反应」。
- **降级策略**：所有上游接口都是非官方接口，因此每一处失败都有兜底——
  史低取不到时卡片照出，只是不显示史低；封面下载失败时画占位图；
  单个游戏人数失败时跳过该游戏；小黑盒不可用时改用 Steam 搜索兜底
  （英文名与 appid 仍可用，中文名会给出明确提示）。

## 已知限制

- 打折列表展示的是 Steam 商店「优惠」页的排序结果，不是全站两万多个折扣商品。
- 在线人数只统计**通过 Steam 启动**的玩家，不含独立客户端、主机和离线模式。
- `steam热度` 的候选取自 Steam 热门榜前 100，因此上榜的是「当前热门的游戏」，
  而不是全站所有游戏里在线人数的绝对前 N。
- 史低来自小黑盒记录，可能与其它史低数据源存在差异。
- 免费游戏与尚未发售的游戏没有价格，卡片会显示对应状态。
- `steam即将推出` 取自 Steam 商店首页的「热门即将推出」货架，与页面一致，约 30 条。
  该货架**无法用搜索接口复现**（`comingsoon` 排序按发售日期，实测前 100 条无一有评测），
  因此直接解析首页 HTML，命令的条数上限也受货架条数限制。
- 图片渲染需要 Pillow；渲染失败时会自动回退为文字结果。

## 开发

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t .
python -m compileall -q __init__.py main.py models.py name_match.py render.py service.py steam_api.py
```

## License

代码使用 [MIT](LICENSE) 许可证。本项目是非官方社区插件，与 Steam、Valve 或小黑盒
不存在隶属、合作或背书关系。
