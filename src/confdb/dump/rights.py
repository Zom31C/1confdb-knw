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

from . import xmlmeta
from .rights_map import RIGHT_UUIDS, RIGHT_UUIDS_BY_NAME

# Имя класса в Rights.xml → тип объекта в БД (классы-исключения во
# множественном числе и ПВХ)
CLASS_ALIAS = {
    'ChartOfCharacteristicTypes': 'ChartOfCharacteristicType',
    'ChartOfCharacteristicType': 'ChartOfCharacteristicType',
    'DocumentNumerator': 'DocumentNumerators',
    'DocumentNumerators': 'DocumentNumerators',
    'Sequence': 'Sequences',
    'Sequences': 'Sequences',
    'ChartOfCalculationTypes': 'ChartOfCalculationTypes',
    'ExternalDataSourceTable': 'ExternalDataSourceTable',
}

VALUE_GRANTED = '1'
VALUE_UNSET = '-1'

# сегмент имени цели → kind в meta_subobject
SUBOBJECT_KIND = {
    'Operation': 'webservice_operation',
    'Method': 'httpservice_method',
    'URLTemplate': 'httpservice_url_template',
    'Parameter': 'webservice_parameter',
    'IntegrationServiceChannel': 'integration_channel',
    'RoutePoint': 'businessprocess_routepoint',
}


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


class TargetResolver:
    """Имя цели из Rights.xml → (target_uuid, obj_id, attr_id, tab_id, sub_id).

    Разрешает составные имена: объект, реквизит, табличная часть и её поле,
    форма/команда/макет (объекты БД), операции веб-сервисов и методы
    HTTP-сервисов (meta_subobject), вложенные подсистемы.
    """

    def __init__(self, conn):
        self.conn = conn
        self._cache = {}

    def resolve(self, name):
        if name in self._cache:
            return self._cache[name]
        result = self._resolve(name)
        self._cache[name] = result
        return result

    def _resolve(self, name):
        parts = name.split('.')
        if not parts:
            return None
        if parts[0] == 'Configuration':
            # цель «Configuration.<имя конфигурации>» — корневой объект;
            # её права (Администрирование, ВебКлиент, ОсновнойРежимОкна…)
            # имеют собственные uuid видов прав — их покрывает майнинговая
            # таблица RIGHT_UUIDS
            row = self.conn.execute(
                "SELECT id, uuid FROM meta_object WHERE path=''").fetchone()
            return (row[1], row[0], None, None, None) if row else None
        if len(parts) < 2:
            return None
        cls = CLASS_ALIAS.get(parts[0], parts[0])
        obj_path = f'{cls}/{parts[1]}'
        row = self.conn.execute(
            'SELECT id, uuid, type FROM meta_object WHERE path=?',
            (obj_path,)).fetchone()
        if row is None:
            return None
        obj_id, obj_uuid, obj_type = row
        rest = parts[2:]
        # вложенные подсистемы: Subsystem.Р.Subsystem.Д[.Subsystem.Е]
        while len(rest) >= 2 and rest[0] == 'Subsystem':
            obj_path = f'{obj_path}/Subsystem/{rest[1]}'
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_object WHERE path=?',
                (obj_path,)).fetchone()
            if row is None:
                return None
            obj_id, obj_uuid = row
            rest = rest[2:]
        if not rest:
            return (obj_uuid, obj_id, None, None, None)
        head = rest[0]
        # Resource/Dimension/AccountingFlag/AddressingAttribute — те же поля
        # объекта (meta_attribute), что и Attribute: регистры и планы счетов
        # в Rights.xml адресуют их своими сегментами
        if head in ('Attribute', 'Resource', 'Dimension', 'AccountingFlag',
                    'AddressingAttribute') and len(rest) == 2:
            return self._attribute(obj_id, obj_uuid, rest[1], None)
        if head == 'TabularSection' and len(rest) >= 2:
            if len(rest) == 2:
                row = self.conn.execute(
                    'SELECT id, uuid FROM meta_tabular'
                    ' WHERE object_id=? AND name=?', (obj_id, rest[1])).fetchone()
                if row:
                    return (row[1], None, None, row[0], None)
                return None
            if len(rest) == 4 and rest[2] in ('Attribute', 'Resource',
                                               'Dimension'):
                return self._attribute(obj_id, obj_uuid, rest[3], rest[1])
            return None
        if head in ('Form', 'Command', 'Template') and len(rest) == 2:
            if head == 'Form':
                sub_type = 'Form' if obj_type == 'DataProcessor' \
                    else obj_type + 'Form'
            elif head == 'Command':
                sub_type = obj_type + 'Command'
            else:
                sub_type = 'Template'
            sub_path = f'{obj_path}/{sub_type}/{rest[1]}'
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_object WHERE path=?',
                (sub_path,)).fetchone()
            if row:
                return (row[1], row[0], None, None, None)
            return None
        if head == 'URLTemplate' and len(rest) == 4 and rest[2] == 'Method':
            # метод шаблона URL: подобъект хранится с объединённым именем
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_subobject'
                ' WHERE object_id=? AND kind=? AND name=?',
                (obj_id, 'httpservice_method',
                 f'{rest[1]}.{rest[3]}')).fetchone()
            if row:
                return (row[1], None, None, None, row[0])
            return None
        if head in SUBOBJECT_KIND and len(rest) == 2:
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_subobject'
                ' WHERE object_id=? AND kind=? AND name=?',
                (obj_id, SUBOBJECT_KIND[head], rest[1])).fetchone()
            if row:
                return (row[1], None, None, None, row[0])
            return None
        return None

    def _attribute(self, obj_id, obj_uuid, attr_name, tabular):
        if tabular is None:
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_attribute'
                ' WHERE object_id=? AND name=? AND tabular IS NULL',
                (obj_id, attr_name)).fetchone()
        else:
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_attribute'
                ' WHERE object_id=? AND name=? AND tabular=?',
                (obj_id, attr_name, tabular)).fetchone()
        if row:
            return (row[1], None, row[0], None, None)
        return None


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
                conn.executemany(
                    'INSERT INTO role_right (role_id, target_uuid,'
                    ' target_object_id, target_attr_id, target_tabular_id,'
                    ' target_subobject_id, sub_index, collection_uuid,'
                    ' target_flags, right_uuid, value, rls_text)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    right_params)
                right_params.clear()
        for ord_no, (tname, ttext) in enumerate(templates):
            tmpl_params.append((role_id, ord_no, tname, ttext))
        state_params.append((role_id, 'xml', 1, len(objects), count,
                             len(templates), None))
        stats['role_rights'] += count
        stats['role_rls_templates'] += len(templates)
    if right_params:
        conn.executemany(
            'INSERT INTO role_right (role_id, target_uuid, target_object_id,'
            ' target_attr_id, target_tabular_id, target_subobject_id,'
            ' sub_index, collection_uuid, target_flags, right_uuid, value,'
            ' rls_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            right_params)
    conn.executemany(
        'INSERT INTO role_rls_template (role_id, ord, name, text)'
        ' VALUES (?, ?, ?, ?)', tmpl_params)
    conn.executemany(
        'INSERT INTO role_rights_state (role_id, version, parsed, targets,'
        ' rights, rls_templates, error) VALUES (?, ?, ?, ?, ?, ?, ?)',
        state_params)
    if unresolved and warnings is not None:
        warnings.append(
            f'права ролей: целей не разрешено по имени: {unresolved}'
            ' (объекты отсутствуют в конфигурации — target_uuid хранит имя)')
