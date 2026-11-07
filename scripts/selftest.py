#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plex-genre-remap 离线自测：不碰 Plex、不碰 NAS。

覆盖：明文/Base64 映射表解析（与插件 parse_table 语义一致）、
目标 genre 计算（多对一/删除/去重）、PUT 安全闸（空列表/空串/超长 URL）、
日文判定、genre 参数编码、section 发现。
"""
from __future__ import print_function

import base64
import io
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import genre_remap as g

PASS = FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(u'  ok    %s' % name)
    else:
        FAIL += 1
        print(u'  FAIL  %s   %s' % (name, detail))


# ---------- 1) 明文映射表解析 ----------
print(u'== 明文映射表解析 ==')
t = g.parse_mapping_text(u'\n'.join([
    u'中出し=内射',
    u'単体作品=单体作品',
    u'ハイビジョン=高清',
    u'# 注释行',
    u'',
    u'エステ=美容\t# 行内注释',
    u'エステ=美容沙龙',          # 后写覆盖
    u'デジモ=',                   # 空值 = 删除
]))
check(u'基本解析', t.get(u'中出し'.upper()) == u'内射')
check(u'键统一大写', u'中出し' in t and u'中出し'.upper() in t)
check(u'后写覆盖前写', t[u'エステ'.upper()] == u'美容沙龙')
check(u'空值保留（删除语义）', t[u'デジモ'.upper()] == u'')
check(u'注释被忽略', u'# 注释行'.upper() not in t)
check(u'无等号行被忽略', g.parse_mapping_text(u'没有等号的行').get(u'没有等号的行'.upper()) is None)
check(u'等号开头的行被忽略', g.parse_mapping_text(u'=value') == {})
check(u'CRLF 兼容', g.parse_mapping_text(u'a=b\r\nc=d\r\n') == {'A': 'b', 'C': 'd'})

# ---------- 2) Base64 表解析（插件语义） ----------
print(u'\n== Base64 表解析（插件 parse_table 语义） ==')
plain = u'\n'.join([u'中出し=内射', u'エステ=美容', u'デジモ=', u'bad line no eq', u'=skip'])
b64 = base64.b64encode(plain.encode('utf-8')).decode('ascii')
tb = g.parse_base64_table(b64)
check(u'解码条数', len(tb) == 3, repr(tb))
check(u'键大写', tb[u'中出し'.upper()] == u'内射')
check(u'空值保留', tb[u'デジモ'.upper()] == u'')
check(u'无等号行忽略', u'bad line no eq'.upper() not in tb)
check(u'= 开头行忽略', u'skip'.upper() not in tb)
# 与插件逐行语义一致：同键后写覆盖
plain2 = u'アナル=肛交\nアナル=后庭'
tb2 = g.parse_base64_table(base64.b64encode(plain2.encode('utf-8')).decode('ascii'))
check(u'同键后写覆盖', tb2[u'アナル'.upper()] == u'后庭')

# ---------- 3) 目标 genre 计算 ----------
print(u'\n== target_genres ==')
m = {u'中出し'.upper(): u'内射', u'単体作品'.upper(): u'内射',
     u'デジモ'.upper(): u''}
out, changed = g.target_genres([u'中出し', u'単体作品'], m)
check(u'多对一去重', out == [u'内射'] and changed)
out, changed = g.target_genres([u'巨乳', u'中出し'], m)
check(u'未命中保持原值', out == [u'巨乳', u'内射'] and changed)
out, changed = g.target_genres([u'巨乳'], m)
check(u'无变化返回 False', out == [u'巨乳'] and not changed)
out, _ = g.target_genres([u'デジモ'], m)
check(u'删除映射', out == [])
out, _ = g.target_genres([u'デジモ', u'デジモ'], m)
check(u'删除映射去重安全', out == [])
out, _ = g.target_genres([u'中出し', u'巨乳', u'単体作品'], m)
check(u'顺序保持（去重保留首个）', out == [u'内射', u'巨乳'])

# ---------- 4) PUT 安全闸 ----------
print(u'\n== put 安全闸 ==')
check(u'空列表拒绝', g.check_puttable([]) is not None)
check(u'None 列表拒绝', g.check_puttable(None) is not None)
check(u'含空串拒绝', g.check_puttable([u'正常', u'']) is not None)
check(u'含纯空白拒绝', g.check_puttable([u'正常', u'  ']) is not None)
check(u'正常列表放行', g.check_puttable([u'内射', u'高清']) is None)

params = g.build_genre_params([u'内射', u'高清'])
check(u'genre[N].tag.tag 参数形', params == [(u'genre[0].tag.tag', u'内射'),
                                             (u'genre[1].tag.tag', u'高清')])


class FakePlex(object):
    """只有 put() 的桩对象，借真实 Plex.put_genres 的安全闸逻辑（只依赖 self.put）。"""

    def __init__(self):
        self.last_path = None

    def put(self, path):
        self.last_path = path
        return b''


fp = FakePlex()
try:
    g.Plex.put_genres(fp, 1, [])
    check(u'put_genres 空列表抛异常', False)
except ValueError:
    check(u'put_genres 空列表抛异常', True)
g.Plex.put_genres(fp, 1, [u'内射'])
check(u'put_genres 正常路径含查询串',
      u'tag.tag' in fp.last_path and fp.last_path.startswith('/library/metadata/1?'))
try:
    g.Plex.put_genres(fp, 1, [u'x' * 3000] * 3)
    check(u'超长 URL 拒绝', False)
except ValueError:
    check(u'超长 URL 拒绝', True)

# ---------- 5) 日文判定 ----------
print(u'\n== 日文判定 ==')
check(u'平假名', g.is_japanese(u'お姉さん'))
check(u'片假名', g.is_japanese(u'パンストモノ'))
check(u'日文专属汉字', g.is_japanese(u'AV女優'))
check(u'中文不算', not g.is_japanese(u'医生'))
check(u'英文不算', not g.is_japanese(u'HD'))
check(u'空值不算', not g.is_japanese(None) and not g.is_japanese(u''))

# ---------- 6) XML 解析 ----------
print(u'\n== XML 解析 ==')
sec_xml = (u'<MediaContainer>'
           u'<Directory key="1" title="电影" type="movie"/>'
           u'<Directory key="2" title="剧集" type="show"/>'
           u'<Directory key="25" title="资料" type="movie"/>'
           u'</MediaContainer>').encode('utf-8')
dirs = ET.fromstring(sec_xml).findall('Directory')
movie_secs = [(int(d.get('key')), d.get('title')) for d in dirs if d.get('type') == 'movie']
check(u'section 发现只取 movie', movie_secs == [(1, u'电影'), (25, u'资料')])

v_xml = (u'<MediaContainer><Video ratingKey="18398" title="T">'
         u'<Genre tag="ハイビジョン"/><Genre tag="巨乳"/>'
         u'<Role tag="演员A"/></Video></MediaContainer>').encode('utf-8')
v = ET.fromstring(v_xml).find('Video')
check(u'详情 genre 提取', [x.get('tag') for x in v.findall('Genre')] == [u'ハイビジョン', u'巨乳'])

# ---------- 7) 派生路径 ----------
print(u'\n== 派生路径 ==')
cfg = g.derive_paths({'plex_url': 'http://nas.local:32400'})
check(u'prefs_path 推导', cfg['prefs_path'].endswith(u'/Preferences.xml')
      and u'nas.local' in cfg['prefs_path'])
check(u'plugin_prefs 推导', cfg['plugin_prefs'].endswith(
    u'/Plug-in Support/Preferences/com.plexapp.agents.metatube.xml'))

print(u'\n===== %d passed, %d failed =====' % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
