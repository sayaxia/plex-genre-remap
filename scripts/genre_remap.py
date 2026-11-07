#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plex genre 重映射：不刷新元数据，直接走 HTTP API 改类型（流派）标签。

为什么存在：
    刷新元数据（agent refresh）会重新刮削：触发翻译 API、重下封面、
    还可能换 provider 重写演员。而「类型替换表」的效果完全可以离线复算 ——
    读出每部影片当前 genre，按表映射，整体写回。

用法（按顺序）：
    python genre_remap.py plan              # 抓取 + 只读计划（首次会建缓存）
    python genre_remap.py pilot 5           # 先试 5 部，逐条校验非 genre 字段
    python genre_remap.py run               # 全量写入（每 50 条做健康检查）
    python genre_remap.py verify            # 复测：剩余不符 + 剩余日文
    python genre_remap.py rollback          # 用最近一次备份还原

安全规则（全部实测踩出来的，别改回去）：
    1. 绝不 PUT 空 genre 列表 —— 一次空列表 PUT 能把 Plex 进程直接搞挂。
    2. 列表接口 /sections/{id}/all 只返回每部影片**前 2 个** Genre，
       必须逐条 GET /library/metadata/{rk} 才拿得到完整类型。
    3. 绝不在 Plex 运行时用任何 SQLite 工具经 SMB 打开它的数据库，
       哪怕 mode=ro 也会扰动 -shm 的 WAL 索引，导致 Plex 读到不一致页
       → SQLITE_NOTADB → 进程崩溃。本工具全程只走 HTTP API。
    4. 本机若有系统代理，访问局域网地址必须显式绕过（urllib 默认走代理会 502）。
"""
from __future__ import print_function

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
STATE = os.path.join(SKILL_DIR, 'state')
os.makedirs(STATE, exist_ok=True)

CACHE = os.path.join(STATE, 'cache.json')
BACKUP = os.path.join(STATE, 'backup.json')
RESULT = os.path.join(STATE, 'result.json')
PLAN_F = os.path.join(STATE, 'plan.json')

KANA = re.compile(u'[\u3041-\u309f\u30a1-\u30fa\u30ff]')
# 只收简繁有差异、或日文语境才用的字形，避免把「医」「体」等中文也判成日文
JP_ONLY = set(u'優単発変関気楽悪圧売覚様嬢実戦験髪顔児図団転伝説読薬鉄銭駅広辺齢渋獣姦婬倶髄髭娘')

MAX_URL = 6000


# ---------------- 纯函数（selftest 覆盖） ----------------

def parse_mapping_text(text):
    """明文映射表 -> dict。行格式 `原文=译文`。

    * 键统一大写（与插件 parse_table 一致，匹配忽略大小写）
    * 同键后写覆盖前写
    * `原文=`（空值）合法：表示把该类型**删除**
    * `#` 开头整行是注释；行内 `\\t#` 之后是注释
    """
    table = {}
    for line in text.split('\n'):
        s = line.rstrip('\r').strip()
        if not s or s.startswith('#'):
            continue
        if '\t#' in s:
            s = s.split('\t#', 1)[0].rstrip()
        if '=' not in s or s.startswith('='):
            continue
        k, v = s.split('=', 1)
        k, v = k.strip(), v.strip()
        if k:
            table[k.upper()] = v
    return table


def parse_base64_table(b64_text):
    """复刻插件 utils.parse_table(sep='\\n', b64=True) 的语义。"""
    raw = base64.b64decode(b64_text).decode('utf-8')
    table = {}
    for kv in raw.split('\n'):
        kv = kv.strip()
        if kv.count('=') > 0 and not kv.startswith('='):
            i = kv.find('=')
            table[kv[:i].upper()] = kv[i + 1:]
    return table


def target_genres(genres, mapping):
    """按表映射一组 genre。返回 (新列表, 是否变化)。

    * 命中表 -> 替换；映射值为空 -> 删除该条
    * 多对一时目标去重（保留首个出现位置）
    * 顺序保持原顺序
    """
    out, seen = [], set()
    for g in genres:
        v = mapping.get(g.upper(), g)
        if v == '':
            continue
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out, out != list(genres)


def build_genre_params(genres):
    """['A','B'] -> [('genre[0].tag.tag','A'), ('genre[1].tag.tag','B')]"""
    return [(u'genre[%d].tag.tag' % i, g) for i, g in enumerate(genres)]


def check_puttable(genres):
    """写入前的安全闸。返回 None 或拒绝原因。"""
    if not genres:
        return u'empty genre list (would kill Plex)'
    if any((g is None) or (not g.strip()) for g in genres):
        return u'blank tag in list'
    return None


def is_japanese(tag):
    if tag is None:
        return False
    if KANA.search(tag):
        return True
    return bool(JP_ONLY & set(tag))


# ---------------- 配置与连接 ----------------

DEFAULT_PREFS_TAIL = u'Plex/Library/Application Support/Plex Media Server'


def load_config(path=None):
    """查找顺序：--config → $PLEX_GENRE_CONFIG → skill 目录 config.json"""
    candidates = []
    if path:
        candidates.append(path)
    if os.environ.get('PLEX_GENRE_CONFIG'):
        candidates.append(os.environ['PLEX_GENRE_CONFIG'])
    candidates.append(os.path.join(SKILL_DIR, 'config.json'))
    for c in candidates:
        if c and os.path.exists(c):
            cfg = json.load(io.open(c, encoding='utf-8'))
            cfg['_config_path'] = c
            return cfg
    sys.exit(u'找不到 config.json（参考 config.example.json），路径: %s' % ', '.join(
        c for c in candidates if c))


def derive_paths(cfg):
    """prefs_path / plugin_prefs 留空时按 plex_url 的主机名推导（群晖套件布局）。"""
    host = urllib.parse.urlparse(cfg['plex_url']).hostname or ''
    if not cfg.get('prefs_path') and host:
        cfg['prefs_path'] = u'//%s/%s/Preferences.xml' % (host, DEFAULT_PREFS_TAIL)
    agent = cfg.get('agent') or 'com.plexapp.agents.metatube'
    if not cfg.get('plugin_prefs') and host:
        cfg['plugin_prefs'] = (u'//%s/%s/Plug-in Support/Preferences/%s.xml'
                               % (host, DEFAULT_PREFS_TAIL, agent))
    return cfg


class Plex(object):
    """带 token、绕代理、带重试的 Plex HTTP 客户端。"""

    def __init__(self, cfg, token=None):
        self.base = cfg['plex_url'].rstrip('/')
        self.timeout = int(cfg.get('timeout') or 60)
        # 本机有系统代理时访问局域网会被拦成 502，必须显式绕过
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._tok = token or os.environ.get('PLEX_TOKEN')
        self._cfg = cfg

    def token(self):
        if self._tok:
            return self._tok
        s = io.open(self._cfg['prefs_path'], encoding='utf-8', errors='replace').read()
        self._tok = re.search(r'PlexOnlineToken="([^"]+)"', s).group(1)
        return self._tok

    def get(self, path, timeout=None, retries=3):
        sep = '&' if '?' in path else '?'
        url = self.base + path + sep + "X-Plex-Token=" + self.token()
        last = None
        for i in range(retries):
            try:
                return self.opener.open(urllib.request.Request(url), timeout=timeout or self.timeout).read()
            except Exception as e:
                last = e
                time.sleep(1 + i)
        raise last

    def put(self, path, timeout=None):
        sep = '&' if '?' in path else '?'
        url = self.base + path + sep + "X-Plex-Token=" + self.token()
        return self.opener.open(urllib.request.Request(url, method='PUT'),
                                timeout=timeout or self.timeout).read()

    def put_genres(self, rk, genres):
        """整体替换一部影片的 genre。绝不接受空列表。"""
        why = check_puttable(genres)
        if why:
            raise ValueError(why)
        q = urllib.parse.urlencode(build_genre_params(genres), encoding='utf-8')
        path = "/library/metadata/%s?%s" % (rk, q)
        if len(path) > MAX_URL:
            raise ValueError('url too long: %d' % len(path))
        return self.put(path)

    def healthy(self):
        self.get('/identity', timeout=10, retries=1)
        return True


# ---------------- 读取与计划 ----------------

def read_plugin_table(cfg, plex):
    """优先直接解析插件里的 Base64 替换表（与 agent 刷新行为完全一致）。"""
    try:
        root = ET.fromstring(io.open(cfg['plugin_prefs'], encoding='utf-8',
                                      errors='replace').read().encode('utf-8'))
        b64 = root.findtext('genre_substitution_table') or ''
        m = parse_base64_table(b64)
        if m:
            return m, u'plugin'
    except Exception as e:
        print(u'读插件替换表失败(%s)，改用本地映射文件' % e)
    mf = cfg.get('mapping_file')
    if not mf or not os.path.exists(mf):
        sys.exit(u'映射表两处都拿不到：插件偏好不可读，且 mapping_file 未配置/不存在')
    m = parse_mapping_text(io.open(mf, encoding='utf-8-sig').read())
    if not m:
        sys.exit(u'映射文件为空: %s' % mf)
    return m, u'file:%s' % mf


def discover_sections(plex):
    x = ET.fromstring(plex.get('/library/sections'))
    return [(int(d.get('key')), d.get('title') or '') for d in x.findall('Directory')
            if d.get('type') == 'movie']


def fetch_all(plex, sections, workers=6, refresh=False):
    """逐条拉详情（列表接口只回前 2 个 Genre，不能用）。结果缓存到 state/cache.json。"""
    from concurrent.futures import ThreadPoolExecutor
    if os.path.exists(CACHE) and not refresh:
        data = json.load(io.open(CACHE, encoding='utf-8'))
        if [s for s in data.get('sections', [])] == [s for s, _ in sections]:
            print(u'使用缓存 %s（%s，%d 部）；要刷新加 --refresh-cache' %
                  (os.path.basename(CACHE), data.get('ts'), data['total']))
            return data
    plex.token()  # 预取 token，避免多线程初始化竞争
    rks, t0 = [], time.time()
    for sid, title in sections:
        start, size = 0, 500
        while True:
            x = ET.fromstring(plex.get(
                "/library/sections/%d/all?X-Plex-Container-Start=%d&X-Plex-Container-Size=%d"
                % (sid, start, size)))
            batch = x.findall('Video')
            total = int(x.get('totalSize') or 0)
            rks.extend((int(v.get('ratingKey')), v.get('title') or '', sid) for v in batch)
            if not batch or start + len(batch) >= total:
                break
            start += size
    print(u'影片 %d 部，逐条拉取详情…' % len(rks))

    def detail(args):
        rk, title, sid = args
        v = ET.fromstring(plex.get("/library/metadata/%d" % rk)).find('Video')
        return {'rk': rk, 'title': title, 'section': sid,
                'genres': [g.get('tag') for g in v.findall('Genre')]}

    items = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i, it in enumerate(ex.map(detail, rks), 1):
            items.append(it)
            if i % 500 == 0:
                print(u'  %d/%d  %.0fs' % (i, len(rks), time.time() - t0))
    data = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'sections': [s for s, _ in sections], 'total': len(items), 'items': items}
    json.dump(data, io.open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
    print(u'抓取 %d 部，用时 %.1fs -> %s' % (len(items), time.time() - t0, CACHE))
    return data


def build_plan(plex, cfg, refresh=False):
    mapping, src = read_plugin_table(cfg, plex)
    sections = resolve_sections(plex, cfg)
    data = fetch_all(plex, sections, workers=int(cfg.get('workers') or 6), refresh=refresh)
    plan, skipped = {}, []
    for it in data['items']:
        gs = it['genres']
        if not gs:
            continue
        tgt, changed = target_genres(gs, mapping)
        if not changed:
            continue
        if not tgt:
            skipped.append((it['rk'], gs))
            continue
        plan[it['rk']] = {'before': gs, 'after': tgt, 'title': it['title']}
    return mapping, src, data, plan, skipped


def resolve_sections(plex, cfg):
    wanted = [int(s) for s in (cfg.get('sections') or [])]
    if not wanted:
        wanted = [s for s, _ in discover_sections(plex)]
        print(u'未配置 sections，自动选用全部电影库: %s' % wanted)
    all_secs = dict(discover_sections(plex))
    return [(s, all_secs.get(s, '')) for s in wanted if s in all_secs]


# ---------------- 写入 ----------------

KEEP_FIELDS = ('title', 'year', 'studio', 'summary', 'thumb', 'art', 'rating',
               'audienceRating', 'originallyAvailableAt', 'guid', 'duration')


def vsnap(plex, rk):
    v = ET.fromstring(plex.get("/library/metadata/%s" % rk)).find('Video')
    d = dict((k, v.get(k)) for k in KEEP_FIELDS)
    d['roles'] = [(r.get('tag'), r.get('role')) for r in v.findall('Role')]
    d['directors'] = [x.get('tag') for x in v.findall('Director')]
    d['tags'] = [x.get('tag') for x in v.findall('Tag')]
    d['genres'] = [g.get('tag') for g in v.findall('Genre')]
    return d


def load_backup():
    if os.path.exists(BACKUP):
        return json.load(io.open(BACKUP, encoding='utf-8'))
    return {}


def save_backup(backup):
    json.dump(backup, io.open(BACKUP, 'w', encoding='utf-8'), ensure_ascii=False)


# ---------------- 子命令 ----------------

def cmd_plan(args, plex, cfg):
    mapping, src, data, plan, skipped = build_plan(plex, cfg, refresh=args.refresh_cache)
    n_b = sum(len(v['before']) for v in plan.values())
    n_a = sum(len(v['after']) for v in plan.values())
    print(u'缓存: %s  影片 %d 部' % (data['ts'], data['total']))
    print(u'映射表: %d 条（空值删除 %d 条）来源: %s'
          % (len(mapping), sum(1 for v in mapping.values() if v == ''), src))
    if args.limit_items:
        plan = dict(list(sorted(plan.items()))[:args.limit_items])
    print(u'需要改写: %d 部' % len(plan))
    print(u'  genre 关联: %d -> %d（净 %+d）' % (n_b, n_a, n_a - n_b))
    print(u'因全部映射为空而跳过: %d 部' % len(skipped))
    for rk, gs in skipped:
        print(u'   rk=%s %s' % (rk, gs))
    left = {}
    for v in plan.values():
        for g in v['after']:
            if is_japanese(g):
                left.setdefault(g, 0)
                left[g] += 1
    if left:
        print(u'\n改写后仍为日文（表里还没有的词条）: %d 个' % len(left))
        for g, c in sorted(left.items(), key=lambda x: -x[1]):
            print(u'   %-34s %d 部' % (g, c))
    json.dump(dict((str(k), v) for k, v in plan.items()),
              io.open(PLAN_F, 'w', encoding='utf-8'), ensure_ascii=False)
    print(u'\n计划已写入 %s' % PLAN_F)


def cmd_pilot(args, plex, cfg):
    mapping, src, data, plan, skipped = build_plan(plex, cfg, refresh=args.refresh_cache)
    keys = sorted(plan.keys(), key=lambda k: -len(plan[k]['before']))[:args.count]
    print(u'试点 %d 部（类型最多的，覆盖面最大）\n' % len(keys))
    backup = load_backup()
    ok = 0
    for rk in keys:
        p = plan[rk]
        b = vsnap(plex, rk)
        if b['genres'] != p['before']:
            print(u'  rk=%s 缓存与实时不一致，跳过\n    API=%s\n    cache=%s'
                  % (rk, b['genres'], p['before']))
            continue
        try:
            plex.put_genres(rk, p['after'])
        except Exception as e:
            print(u'  rk=%s PUT 失败: %s' % (rk, e))
            continue
        a = vsnap(plex, rk)
        diff = [k for k in b if k != 'genres' and b[k] != a[k]]
        good = (a['genres'] == p['after']) and not diff
        print(u'  rk=%-7s %d -> %d 个类型 %s'
              % (rk, len(b['genres']), len(a['genres']), u'OK' if good else u'FAIL'))
        print(u'      %s' % b['genres'])
        print(u'   -> %s' % a['genres'])
        if diff:
            print(u'      !! 非 genre 字段变化: %s' % diff)
        if good:
            ok += 1
            backup[str(rk)] = b['genres']
    save_backup(backup)
    print(u'\n试点: %d/%d 通过；原始值备份在 %s' % (ok, len(keys), BACKUP))


def cmd_run(args, plex, cfg):
    mapping, src, data, plan, skipped = build_plan(plex, cfg, refresh=args.refresh_cache)
    backup, done, fail = load_backup(), {}, []
    keys = sorted(plan.keys())
    if args.items:
        keys = [k for k in keys if k in args.items]
    if args.limit_items:
        keys = keys[:args.limit_items]
    print(u'全量: %d 部，开始 %s' % (len(keys), time.strftime('%H:%M:%S')))
    t0 = time.time()
    for i, rk in enumerate(keys, 1):
        p = plan[rk]
        try:
            if str(rk) not in backup:
                backup[str(rk)] = p['before']
            plex.put_genres(rk, p['after'])
            done[str(rk)] = p['after']
        except Exception as e:
            fail.append((rk, u'%s' % e))
        if i % 50 == 0:
            save_backup(backup)
            json.dump(done, io.open(RESULT, 'w', encoding='utf-8'), ensure_ascii=False)
            try:
                plex.healthy()
                h = u'OK'
            except Exception as e:
                print(u'  !! Plex 健康检查失败: %s，中止' % e)
                break
            el = time.time() - t0
            print(u'  %d/%d  %.1fs  剩余约 %.0fs  Plex=%s'
                  % (i, len(keys), el, el / i * (len(keys) - i), h))
    save_backup(backup)
    json.dump(done, io.open(RESULT, 'w', encoding='utf-8'), ensure_ascii=False)
    print(u'\n完成 %d 部，失败 %d 部，用时 %.0fs' % (len(done), len(fail), time.time() - t0))
    for rk, e in fail[:20]:
        print(u'  rk=%s %s' % (rk, e))


def cmd_verify(args, plex, cfg):
    mapping, src, data, plan, skipped = build_plan(plex, cfg, refresh=True)
    print(u'\n=== 复测（缓存已刷新）===')
    print(u'仍与映射表不符: %d 部' % len(plan))
    for rk in sorted(plan.keys())[:20]:
        print(u'  rk=%s %s' % (rk, plan[rk]['before'][:5]))
    bad = {}
    for it in data['items']:
        for g in it['genres']:
            if is_japanese(g):
                bad.setdefault(g, []).append(it['rk'])
    print(u'\n全库仍为日文: %d 个 / %d 部'
          % (len(bad), len(set(x for v in bad.values() for x in v))))
    for g, rks in sorted(bad.items(), key=lambda x: -len(x[1]))[:40]:
        print(u'  %-36s %d 部' % (g, len(rks)))
    empty = [(it['rk'], it['genres']) for it in data['items']
             if any((g is None) or (not g.strip()) for g in it['genres'])]
    print(u'\n含空字符串 genre 的影片: %d 部' % len(empty))
    for rk, gs in empty[:10]:
        print(u'  rk=%s %r' % (rk, gs))


def cmd_rollback(args, plex, cfg):
    backup = load_backup()
    if not backup:
        sys.exit(u'没有备份可回滚: %s' % BACKUP)
    print(u'回滚 %d 部…' % len(backup))
    n = 0
    for rk, gs in backup.items():
        if not gs:
            continue
        try:
            plex.put_genres(rk, gs)
            n += 1
        except Exception as e:
            print(u'  rk=%s 失败 %s' % (rk, e))
    print(u'已回滚 %d 部' % n)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('command', choices=['plan', 'pilot', 'run', 'verify', 'rollback'])
    ap.add_argument('count', nargs='?', type=int, default=5, help='pilot 的条数')
    ap.add_argument('--config', help='指定 config.json 路径')
    ap.add_argument('--mapping', help='指定明文映射文件（覆盖 config 的 mapping_file）')
    ap.add_argument('--section', action='append', type=int,
                    help='限定 section id，可重复（覆盖 config 的 sections）')
    ap.add_argument('--item', action='append', type=int,
                    help='限定 ratingKey，可重复（run 时只处理这些）')
    ap.add_argument('--limit', dest='limit_items', type=int, help='只处理前 N 部')
    ap.add_argument('--refresh-cache', action='store_true', help='强制重新抓取')
    ap.add_argument('--token', help='X-Plex-Token（默认读 Preferences.xml）')
    args = ap.parse_args()

    cfg = derive_paths(load_config(args.config))
    if args.mapping:
        cfg['mapping_file'] = args.mapping
    if args.section:
        cfg['sections'] = args.section
    args.items = set(args.item or [])
    plex = Plex(cfg, token=args.token)

    {'plan': cmd_plan, 'pilot': cmd_pilot, 'run': cmd_run,
     'verify': cmd_verify, 'rollback': cmd_rollback}[args.command](args, plex, cfg)


if __name__ == '__main__':
    main()
