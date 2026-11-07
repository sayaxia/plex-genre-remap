---
name: plex-genre-remap
description: 不刷新元数据、不消耗翻译 API，直接通过 Plex HTTP API 批量改写影片的类型（genre/流派）标签。当用户说「不想刷新元数据改类型」「类型替换表直接生效」「流派替换批量应用」「genre 映射/重映射」「直接改标签」「把日文类型换成中文」「替换表更新了但要应用到存量库」并涉及 Plex 时使用。适用于 MetaTube 类型替换表更新后，把效果离线应用到全库而不触发重新刮削。
agent_created: true
---

# Plex genre 重映射（不刷新元数据）

把「类型替换表」的映射效果**直接写进 Plex**：逐条读 genre → 按表映射 → 整体写回。
全程 HTTP API，不触发 agent 刷新，**不重新刮削、不烧翻译 API、不动封面、不动演员**。

## 何时使用

| 用户说的话 | 跑什么 |
|---|---|
| 「替换表更新了，把存量库的类型也换掉，别刷新」 | `plan` → `pilot 5` → `run` |
| 「先试几部看看会不会搞坏」 | `pilot 5`（逐条校验非 genre 字段一字不差） |
| 「改完了，检查一下」 | `verify`（重抓全库，报剩余不符 + 剩余日文 + 空标签） |
| 「改坏了，恢复回去」 | `rollback`（用 state/backup.json 还原） |
| 「还有哪些日文类型没进表」 | `plan`（干跑会列出「改写后仍为日文」清单） |

## 快速上手

```bash
S=~/.workbuddy/skills/plex-genre-remap/scripts/genre_remap.py
python $S plan            # 只读：会动多少部、多少条关联
python $S pilot 5         # 先 5 部，逐条 PUT + 校验
python $S run             # 确认无误后全量（每 50 条健康检查）
python $S verify          # 复测
```

先 `pilot` 再 `run` 是铁律：879 部 48 秒跑完的前提是试点先全绿。

## 映射表从哪来

1. **优先**：直接解析插件偏好里的 Base64 替换表（`Plug-in Support/Preferences/<agent>.xml`
   的 `genre_substitution_table`）—— 与 agent 刷新时的行为**完全一致**，改了插件设置就自动跟随。
2. **回退**：config 里 `mapping_file` 指向的明文文件（`原文=译文`，每行一条）。

语义（与插件 `parse_table` + `table_substitute` 一致）：

- `AAA=BBB`：多对一映射，多种相似描述可统一到 BBB
- `AAA=`（空值）：**删除**该类型（Plex 收到空串会自己丢弃，不会留空白标签）
- 键忽略大小写；同键后写覆盖前写

## 命令与参数

| 子命令 | 作用 |
|---|---|
| `plan` | 只读测绘 + 输出计划到 `state/plan.json`；列出「改写后仍为日文」的词条 |
| `pilot N` | 试 N 部（挑类型最多的），写前快照、写后读回，非 genre 字段必须一字不差 |
| `run` | 全量执行；每 50 条落盘备份 + 探活 Plex，异常立即中止 |
| `verify` | 强制重抓全库，报：仍与表不符 / 全库剩余日文 / 空字符串 genre |
| `rollback` | 按 `state/backup.json` 还原 |

| 参数 | 含义 |
|---|---|
| `--section ID` | 限定媒体库（可重复），覆盖 config |
| `--item KEY` | 限定 ratingKey（可重复），run 时只处理这些 |
| `--mapping FILE` | 临时指定明文映射文件 |
| `--limit N` | 只处理前 N 部 |
| `--refresh-cache` | 强制重新抓取（默认用 state/cache.json 缓存） |
| `--token` / `$PLEX_TOKEN` | 手动给 token，默认读 `Preferences.xml` 的 `PlexOnlineToken` |

## 配置

`~/.workbuddy/skills/plex-genre-remap/config.json`：

```json
{
  "plex_url": "http://<plex-host>:32400",
  "prefs_path": "//<plex-host>/Plex/Library/Application Support/Plex Media Server/Preferences.xml",
  "plugin_prefs": "//<plex-host>/Plex/.../Plug-in Support/Preferences/com.plexapp.agents.metatube.xml",
  "agent": "com.plexapp.agents.metatube",
  "sections": [],
  "mapping_file": "C:\\Users\\<用户名>\\Desktop\\流派替换.txt",
  "workers": 6,
  "timeout": 60
}
```

- `prefs_path` / `plugin_prefs` 留空会按 `plex_url` 主机名推导（群晖套件布局）。
- `sections` 留空自动选用全部电影库（`type=movie`）。
- 换机器只改这个文件，或 `--config other.json`。

## 三条铁律（实测崩过 Plex 才写进来的，别改回去）

1. **绝不 PUT 空 genre 列表**。一次空列表 PUT 能让 Plex 进程直接退出。
   所有会被映射删空的影片一律跳过（脚本已内置安全闸）。
2. **列表接口 `/sections/{id}/all` 只返回每部前 2 个 Genre**。据此做的统计全是截断的子集；
   必须逐条 `GET /library/metadata/{rk}` 拿完整类型（3194 部 6 线程约 30 秒）。
3. **Plex 运行中不要用任何 SQLite 工具经 SMB 打开它的数据库**，哪怕 `mode=ro`
   也会扰动 `-shm` 的 WAL 索引 → Plex 读到不一致页 → `file is not a database` → 崩溃。
   本工具全程只走 HTTP API。

另：本机有系统代理时，脚本已显式绕过（`ProxyHandler({})`），局域网请求不会被拦成 502。

## 写入语义

- `PUT /library/metadata/{rk}?genre[N].tag.tag=值` 是**整体替换**，必须一次传全。
- 副作用实测为零：title / summary / 封面 / 演员 / studio / guid 全部不动，
  `pilot` 会逐字段比对，任何非 genre 字段变化直接 FAIL。
- 目标中文标签库里不存在时 Plex 自动新建，无需额外操作。

## 结果怎么读

| 输出 | 含义 |
|---|---|
| `试点: 5/5 通过` | 可以 `run` |
| `仍与映射表不符: 0 部` | 全部生效 |
| `改写后仍为日文: N 个` | 这些词条还不在表里，把译文补进表/插件设置后重跑 |
| `含空字符串 genre 的影片: 0 部` | 删除映射没有留下垃圾（应为 0，否则报障） |

## 改代码之后

```bash
python scripts/selftest.py
```

39 项离线用例，不碰 Plex、不碰 NAS：明文/Base64 表解析（对齐插件语义）、
目标 genre 计算（多对一/删除/去重/顺序）、PUT 安全闸（空列表/空串/超长 URL）、
日文判定、XML 解析、路径推导。全绿再上真机。
