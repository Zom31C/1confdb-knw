"""Права ролей из текстового `Roles\\<Имя>\\Ext\\Rights.xml` выгрузки.

Формат (version 2.20, xmlns http://v8.1c.ru/8.2/roles):

    <Rights>
      <setForNewObjects>false</setForNewObjects>
      <setForAttributesByDefault>true</setForAttributesByDefault>
      <independentRightsOfChildObjects>false</independentRightsOfChildObjects>
      <object>
        <name>Catalog.Валюты[.Attribute.Код|.TabularSection.Т[.Attribute.Ф]
              |.Form.Ф|.Command.К|.Template.Т|.Operation.О|.Method.М]</name>
        <right>
          <name>Read</name>
          <value>true|false</value>
          [<restrictionByCondition><condition>ТЕКСТ RLS</condition>…</restrictionByCondition>]
        </right>
      </object>
      <restrictionTemplate><name>Имя</name><condition>Текст</condition></restrictionTemplate>
    </Rights>

Соответствие базе из .cf (db.writer._write_role_rights + confdb.rights):
- value: true → '1' (право установлено), false → '-1' (не установлено) —
  sparse-семантика та же: отсутствие строки означает «право не задано»;
- target_uuid — uuid разрешённой цели (объект/реквизит/ТЧ/подобъект);
  имя цели хранится в target_uuid, если цель не найдена (информативнее
  пустоты, счётчик неразрешённых — в role_rights_state.error);
- right_uuid — uuid вида права: в .cf права адресованы uuid, в выгрузке —
  английскими именами; соответствие — статическая таблица RIGHT_UUIDS
  (накопана по паре «выгрузка + cf-база одной конфигурации», см.
  _tmp/mine_right_uuids.py). Не найденное имя пишется как есть (lowercase),
  такие строки заметны и не ломают разрешение целей.
"""
import os
import xml.etree.ElementTree as ET

from ..db import writer as W
from ..rights import CLASS_ALIAS, SUBOBJECT_KIND, TargetResolver  # noqa: F401
from . import xmlmeta
from .rights_map import RIGHT_UUIDS, RIGHT_UUIDS_BY_NAME

VALUE_GRANTED = '1'
VALUE_UNSET = '-1'


def parse_rights_xml(path):
    """(objects, rls_templates, flags) из Rights.xml.

    objects — [(target_name, [(right_eng, value, rls_text), ...])];
    rls_templates — [(name, text)]; flags — три флага формата (dict).
    """
    root = xmlmeta.load(path)
    flags = {
        'setForNewObjects': xmlmeta._text(root, 'setForNewObjects'),
        'setForAttributesByDefault':
            xmlmeta._text(root, 'setForAttributesByDefault'),
        'independentRightsOfChildObjects':
            xmlmeta._text(root, 'independentRightsOfChildObjects'),
    }
    objects = []
    for obj in root:
        if obj.tag != 'object':
            continue
        name = xmlmeta._text(obj, 'name')
        if not name:
            continue
        entries = []
        for right in obj:
            if right.tag != 'right':
                continue
            rname = xmlmeta._text(right, 'name')
            if not rname:
                continue
            value = 'true' if xmlmeta._text(right, 'value') == 'true' else 'false'
            rls = None
            rbc = xmlmeta._find(right, 'restrictionByCondition')
            if rbc is not None:
                rls = xmlmeta._text(rbc, 'condition')
            entries.append((rname, value, rls))
        if entries:
            objects.append((name.strip(), entries))
    templates = []
    for tmpl in root:
        if tmpl.tag != 'restrictionTemplate':
            continue
        tname = xmlmeta._text(tmpl, 'name')
        ttext = xmlmeta._text(tmpl, 'condition')
        if tname or ttext:
            templates.append((tname or '', ttext or ''))
    return objects, templates, flags


def right_uuid_of(eng_name, target_class=None):
    """uuid вида права по английскому имени из Rights.xml.

    Сначала точное соответствие (класс цели, имя) — uuid видов прав у части
    семейств различаются (три uuid «Чтения» в confdb.rights.RIGHT_NAMES);
    затем глобальное имя→uuid, если оно единственное. Иначе — само имя
    (lowercase): строка остаётся различимой, но не джойнится со словарём.
    """
    low = eng_name.lower()
    if target_class:
        hit = RIGHT_UUIDS.get((target_class, low))
        if hit:
            return hit
    hit = RIGHT_UUIDS_BY_NAME.get(low)
    if hit:
        return hit
    return low


def write_role_rights(conn, dump_dir, roles, path_id, stats, warnings=None):
    """Права всех ролей выгрузки — в role_right/role_rls_template/state.

    :param roles: [(role_id, role_path)] из фазы 1;
    :param path_id: не используется напрямую (цели разрешаются SQL-ом), но
        передаётся для единообразия фаз.
    """
    resolver = TargetResolver(conn)
    right_params = []
    tmpl_params = []
    state_params = []
    unresolved = 0
    for role_id, role_path in roles:
        role_name = role_path.rsplit('/', 1)[-1]
        xml_path = os.path.join(dump_dir, 'Roles', role_name, 'Ext',
                                'Rights.xml')
        if not os.path.isfile(xml_path):
            state_params.append((role_id, None, 0, 0, 0, 0,
                                 'нет файла Ext/Rights.xml'))
            stats['role_rights_failed'] += 1
            continue
        try:
            objects, templates, _flags = parse_rights_xml(xml_path)
        except (ET.ParseError, OSError) as err:
            state_params.append((role_id, None, 0, 0, 0, 0, str(err)[:300]))
            stats['role_rights_failed'] += 1
            continue
        count = 0
        for target_name, entries in objects:
            resolved = resolver.resolve(target_name)
            if resolved is None:
                unresolved += 1
                target_uuid, obj_id, attr_id, tab_id, sub_id = \
                    target_name, None, None, None, None
            else:
                target_uuid, obj_id, attr_id, tab_id, sub_id = resolved
            target_class = target_name.split('.', 1)[0]
            for eng, value, rls in entries:
                right_params.append((
                    role_id, target_uuid, obj_id, attr_id, tab_id, sub_id,
                    None, None, None,
                    # класс — как в Rights.xml (майнинговая таблица keyed
                    # теми же именами: ChartOfCharacteristicTypes и т.п.)
                    right_uuid_of(eng, target_class),
                    VALUE_GRANTED if value == 'true' else VALUE_UNSET,
                    rls))
                count += 1
            if len(right_params) >= 20000:
                conn.executemany(W.INSERT_SQL['role_right'], right_params)
                right_params.clear()
        for ord_no, (tname, ttext) in enumerate(templates):
            tmpl_params.append((role_id, ord_no, tname, ttext))
        state_params.append((role_id, 'xml', 1, len(objects), count,
                             len(templates), None))
        stats['role_rights'] += count
        stats['role_rls_templates'] += len(templates)
    if right_params:
        conn.executemany(W.INSERT_SQL['role_right'], right_params)
    conn.executemany(W.INSERT_SQL['role_rls_template'], tmpl_params)
    conn.executemany(W.INSERT_SQL['role_rights_state'], state_params)
    if unresolved and warnings is not None:
        warnings.append(
            f'права ролей: целей не разрешено по имени: {unresolved}'
            ' (объекты отсутствуют в конфигурации — target_uuid хранит имя)')
