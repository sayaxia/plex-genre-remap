# plex-genre-remap

不刷新元数据，直接通过 Plex HTTP API 批量改写影片的类型（genre/流派）标签。

它既是一个命令行工具，也是一个 [WorkBuddy](https://www.workbuddy.cn) skill —— 放进 `~/.workbuddy/skills/` 后，说一句「把替换表的效果应用到全库，不要刷新元数据」就能触发。

## 它解决什么

Plex + MetaTube 这套组合里，类型的中文化靠插件设置里的「类型替换表」（genre substitution table，Base64 存储的 `原文=译文` 清单）。问题在于：**这张表只在刮削/刷新时生效**。

想让存量库应用新表，官方路径只有一条：全库刷新元数据。而刷新的代价是：

- 每一条都重新刮削 → **消耗翻译 API**（标题/简介翻译）
- 重新下载封面 → 已钉好的自定义封面被冲掉
- 可能触发重新匹配换 provider → 演员被清空重写

但替换表的效果完全可以**离线复算**：读出每部影片当前的 genre 列表，按表映射，再整体写回去。这个工具就是这么做的 —— 全程 HTTP API，实测 **3194 部的库改写 879 部只要 48 秒**，翻译 API 消耗为零，封面、演员、标题一个字节都不动。

## 三条铁律（都是实测崩过 Plex 才留下的）

1. **绝不 PUT 空 genre 列表。** `PUT /library/metadata/{rk}` 不带任何 genre 参数，
   能让 Plex 进程直接退出。工具内置安全闸：会被映射删空的影片一律跳过。
   `AAA=`（空值）的删除语义是安全的 —— Plex 收到映射后的空串会自己丢弃，
   不会留下空白标签（879 部实测含空 genre 的为 0）。
2. **列表接口 `/library/sections/{id}/all` 只返回每部影片前 2 个 Genre。**
   据此做的任何统计都是截断的子集（曾据此算出「230 个类型」，逐条读详情后其实是 495 个）。
   必须逐条 `GET /library/metadata/{rk}`。
3. **Plex 运行中，不要用任何 SQLite 工具经 SMB 打开它的数据库**，哪怕 `mode=ro`。
   远端只读连接会扰动 `-shm` 的 WAL 索引，Plex 读到不一致页会报
   `file is not a database` 然后进程崩溃。本工具全程只走 HTTP API。

顺带：`PUT ...?genre[N].tag.tag=值` 是**整体替换**，一次传全；本机有系统代理时访问
局域网必须显式绕过（工具已内置 `ProxyHandler({})`）。

## 前置条件

- Plex Media Server（实测 PMS 1.40.x），能访问 32400 端口
- `Preferences.xml` 可读（用于取 `PlexOnlineToken`），或直接给 `--token` / `$PLEX_TOKEN`
- （可选）MetaTube 插件偏好的 `Plug-in Support/Preferences/com.plexapp.agents.metatube.xml`
  可读 —— 工具会直接解析里面的 Base64 替换表，与 agent 刷新行为完全一致；
  拿不到就用 `mapping_file` 指向明文文件（`原文=译文`，每行一条）
- Python 3.6+，无第三方依赖

## 安装

### 一句话装（推荐）

在 WorkBuddy 里新建对话，发这两行：

```text
安装 skill：https://github.com/sayaxia/plex-genre-remap
装到 ~/.workbuddy/skills/plex-genre-remap，config.json 缺的参数问我。
```

能一句话搞定，是因为仓库把该给的信息都给全了：public 可匿名 clone、根目录有
`SKILL.md`（frontmatter 里写了 `name` 和 `description`，agent 读完就知道什么时候触发）、
`config.example.json` 带完整默认值。剩下只有 Plex 地址这类**只有你知道**的东西，
所以提示词里写一句「缺的参数问我」就够了。

装完**新开一次对话**，说「把类型替换表应用到存量库，不要刷新元数据」即可。

### 完整版（想装完立刻能验收时用）

<details>
<summary>展开：带验收步骤和约束的完整提示词</summary>

```text
帮我把 plex-genre-remap 安装成 WorkBuddy skill。

仓库：https://github.com/sayaxia/plex-genre-remap（public，直接 clone 即可）

步骤：
1. clone 到 ~/.workbuddy/skills/plex-genre-remap
   Windows 上是 C:\Users\<用户名>\.workbuddy\skills\plex-genre-remap
   如果目录已存在，先告诉我要覆盖还是保留，不要直接删。
2. 把 config.example.json 复制成 config.json（config.json 被 .gitignore 排除，只在本地存在）：
   Windows: copy config.example.json config.json
   macOS/Linux: cp config.example.json config.json
3. 按我给的信息填 config.json 的这几项，其余留空用默认推导：
   - plex_url     Plex 地址，例如 http://192.168.1.10:32400
   - prefs_path   Plex 的 Preferences.xml（SMB 路径或本地路径，群晖布局可留空自动推导）
   - plugin_prefs MetaTube 插件偏好 xml（留空自动推导；没有 MetaTube 就留空，
                   用 mapping_file 指向明文映射文件）
   - mapping_file 明文映射文件路径（原文=译文，每行一条；插件表可读时可不填）
   我还没给的话，先问我要，不要猜。
4. 验证：
   python scripts/selftest.py        # 必须 39/39 通过
   python scripts/genre_remap.py plan  # 必须能列出库和计划，不写任何东西
5. 把 config.json 的最终内容（token 之类的敏感值可以打码）和上面两条命令的输出贴给我。

约束：
- 不要 git add / commit / push config.json 和 state/ 目录，里面有内网地址和库数据。
- 不要改 scripts/ 下的代码，先按原样跑通。
- 装完提醒我：新装的 skill 要**新开一次对话**才会出现在可调用列表里。
```

</details>

### 手动装

```bash
git clone https://github.com/sayaxia/plex-genre-remap.git \
          ~/.workbuddy/skills/plex-genre-remap
cp ~/.workbuddy/skills/plex-genre-remap/config.example.json \
   ~/.workbuddy/skills/plex-genre-remap/config.json
# 编辑 config.json：至少填 plex_url，其余可留空自动推导
```

只想当命令行工具用的话，clone 到任意目录也一样，加 `--config` 指过去即可。

## 用法

```bash
S=~/.workbuddy/skills/plex-genre-remap/scripts/genre_remap.py

python $S plan              # 只读：会改多少部、多少条关联、还剩哪些日文词条
python $S pilot 5           # 先试 5 部（逐条校验非 genre 字段一字不差）
python $S run               # 全量写入（每 50 条落盘备份 + 探活 Plex）
python $S verify            # 复测：剩余不符 / 剩余日文 / 空标签
python $S rollback          # 用 state/backup.json 还原
```

先 `pilot` 再 `run` 是铁律。常用参数：

| 参数 | 含义 |
|---|---|
| `--section ID` | 限定媒体库（可重复） |
| `--item KEY` | 限定 ratingKey（可重复） |
| `--mapping FILE` | 临时指定明文映射文件 |
| `--limit N` | 只处理前 N 部 |
| `--refresh-cache` | 强制重新抓取（默认用 state/cache.json 缓存） |

## 映射表语义（与插件 parse_table + table_substitute 一致）

```
AAA=BBB     多对一映射：多种相似描述统一到 BBB
AAA=        空值 = 删除该类型（Plex 会丢弃空串，不留空白标签）
```

- 键忽略大小写；同键后写覆盖前写
- 映射来源优先级：插件偏好里的 Base64 表（自动跟随插件设置）→ `mapping_file` 明文文件
- 写回时目标去重、保持原顺序；整体替换所以必须一次传全

## 配置

```json
{
  "plex_url": "http://<plex-host>:32400",
  "prefs_path": "//<plex-host>/Plex/Library/Application Support/Plex Media Server/Preferences.xml",
  "plugin_prefs": "//<plex-host>/Plex/.../Plug-in Support/Preferences/com.plexapp.agents.metatube.xml",
  "agent": "com.plexapp.agents.metatube",
  "sections": [],
  "mapping_file": "/path/to/mapping.txt",
  "workers": 6,
  "timeout": 60
}
```

- `prefs_path` / `plugin_prefs` 留空按 `plex_url` 主机名推导（群晖套件版布局）。
- `sections` 留空自动选用全部电影库。
- 配置查找顺序：`--config` → `$PLEX_GENRE_CONFIG` → skill 目录的 `config.json`。
- `state/`（缓存、备份、计划、结果）已被 gitignore，只在本地存在。

## 回滚

每次 `pilot` / `run` 写入前都会把**改写前的原始 genre** 存进 `state/backup.json`
（全量过程中每 50 条落盘一次，中途崩了也已落盘）：

```bash
python $S rollback          # 约每秒几十部，879 部约 1 分钟
```

## 自测

```bash
python scripts/selftest.py
```

39 项离线用例，不碰 Plex、不碰 NAS：明文/Base64 表解析（对齐插件语义，含后写覆盖、
空值保留）、目标 genre 计算（多对一/删除/去重/顺序）、PUT 安全闸（空列表/空串/超长 URL）、
日文判定（假名/日文专属汉字/中文不误判）、XML 解析、路径推导。

## 注意

- **规模参考**：3194 部的库，抓详情约 30 秒、改写 879 部 48 秒、复测约 30 秒。
- 目标中文标签库里不存在时 Plex 会自动新建，无需额外操作。
- `plan` / `verify` 输出的「仍为日文」清单就是下一批该补进替换表的词条；
  补完表（改插件设置或 mapping_file）重跑 `plan → run` 即可，增量生效。
- 本工具只改 genre。标签锁、其他字段编辑请走 Plex 自身界面。

## 许可

MIT
