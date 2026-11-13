"""Паритет cf ↔ dump: одна конфигурация, два формата, две базы, один дифф.

Одна и та же логическая конфигурация описывается ДВАЖДЫ:
- `make_cf_tree` — дерево стадии 3 (выход декодера .cf), вход `db.writer.write_db`;
- `make_xml_dump` — иерархическая выгрузка конфигуратора, вход `dump.build_db`.

Общие константы (uuid, имена, типы, тексты модулей и запросов) используются
обеими фикстурами, поэтому любое расхождение строк — реальное различие двух
путей извлечения, а не шум фикстур. Состав конфигурации: справочник
(реквизиты, ссылка на себя, ТЧ с полем, форма с модулем, предопределённые),
общий модуль с контекстом, перечисление с модулем менеджера, отчёт с
СКД-макетом, регистр сведений (ресурс/измерение/реквизит), роль с правами
(объект, реквизит, конфигурация), RLS-условием и именованным шаблоном RLS.

Сравниваемые таблицы перечислены в QUERIES. Осознанно НЕ сравниваются
(документированные отличия dump-базы от cf-базы, README «Отличия dump-базы»):
- meta_object.ord — в cf разреженные позиции бинарного заголовка, в dump
  плотные индексы списков ChildObjects (относительный порядок одинаков);
- meta_object.header_json — внутренний JSON декодера против исходного XML
  (читатели header_props понимают оба формата);
- meta_object.obj_version — в cf версия формата объекта из заголовка, в dump
  редакция конфигурации ('803') у детей и '803xx' у корня;
- attribute_ref.uuid — в cf «источниковый» uuid дескриптора типа (таблица .10),
  в dump uuid объекта-цели; смысл связи несут object_id/target_path (сравниваются);
- role_right.target_flags/sub_index/collection_uuid — флаги записи цели есть
  только в скобкофайле cf;
- role_rights_state.version — '10' (формат скобкофайла) против 'xml';
- source.file/created/file_size/file_sha256 — файл-контейнер против манифеста
  выгрузки (root_type/root_name/root_uuid/extractor_version — сравниваются);
- file, dump_state, dump_file — разный состав и назначение.
"""
import os
import sqlite3

import pytest

from confdb.db.writer import VT_FIELDS_KEY, write_db
from confdb.dump import build_db
from confdb.header_props import (IR_ATTRIBUTES, IR_DIMENSIONS, IR_FORMS,
                                 IR_RESOURCES)
from test_dump import NS, _w
from test_writer import (PREDEF_TYPE, ZERO_UUID, _attr, _cfg_props, _core,
                         _flag_desc, _json, _pbool, _pcol, _pfile, _prow,
                         _pstr, _puuid, _register_header, _write)

# --- общие константы: ОДНА конфигурация в двух форматах -----------------------

CFG_UUID = 'e0e0e0e0-0000-0000-0000-00000000f001'
# внутренний uuid файла-контейнера cf: в базу попасть НЕ должен (корень несёт
# uuid метаданных Configuration — file_uuid, как в dump-базе)
CONTAINER_UUID = 'f0ba0954-0000-0000-0000-00000000f002'
CAT_UUID = 'bbbbbbbb-0000-0000-0000-00000000f003'
ATTR_STR_UUID = 'aaaaaaaa-0000-0000-0000-00000000f011'   # РеквизитСтрока
ATTR_REF_UUID = 'aaaaaaaa-0000-0000-0000-00000000f012'   # РодительСсылка
TS_UUID = 'aaaaaaaa-0000-0000-0000-00000000f013'         # ТЧ Товары
TS_COLL_UUID = 'aaaaaaaa-0000-0000-0000-00000000f014'    # uuid коллекции полей ТЧ
TSF_UUID = 'aaaaaaaa-0000-0000-0000-00000000f015'        # поле Количество
FORM_UUID = 'cccccccc-0000-0000-0000-00000000f016'
CM_UUID = '0a0a0a0a-0000-0000-0000-00000000f017'
ENUM_UUID = 'eeeeeeee-0000-0000-0000-00000000f018'
EV1_UUID = 'eeeeeeee-0000-0000-0000-00000000f019'
EV2_UUID = 'eeeeeeee-0000-0000-0000-00000000f01a'
REPORT_UUID = '0d0d0d0d-0000-0000-0000-00000000f01b'
TMPL_UUID = '0d0d0d0d-0000-0000-0000-00000000f01c'
ROLE_UUID = '7e7e7e7e-0000-0000-0000-00000000f01d'
IR_UUID = '0e0e0e0e-0000-0000-0000-00000000f01e'
IR_RES_UUID = '0e0e0e0e-0000-0000-0000-00000000f01f'     # Ресурс1
IR_DIM_UUID = '0e0e0e0e-0000-0000-0000-00000000f020'     # Измерение1
IR_ATTR_UUID = '0e0e0e0e-0000-0000-0000-00000000f021'    # Реквизит1
PREDEF_UUID = '4a4a4a4a-0000-0000-0000-00000000f022'
# «источниковый» uuid справочника в таблице .10 — только cf, в сравнении не входит
REF_CAT = '11111111-1111-1111-1111-111111111111'
# платформенные uuid видов прав (намайненная биекция, test_dump)
READ_UUID = '1c87578f-9e09-4ec0-a991-5629c87b1588'
VIEW_UUID = 'aa6448f2-be0f-42ea-ba26-1af7f52b5b65'

CAT_OBJ_BSL = 'Процедура ПриЗаписи(Отказ)\n\tА = 1;\nКонецПроцедуры'
FORM_BSL = '&НаКлиенте\nПроцедура Команда1()\nКонецПроцедуры'
CM_BSL = 'Функция Экспортная(А) Экспорт\n\tВозврат А + 1;\nКонецФункции'
ENUM_MGR_BSL = ('Функция ЗначениеПоИмени(Имя) Экспорт\n'
                '\tВозврат Перечисления.Перечисление1[Имя];\nКонецФункции')
SKD_QUERY = ('ВЫБРАТЬ\n\tСправочник1.Ссылка\nИЗ\n\t'
             'Справочник.Справочник1 КАК Справочник1')
RLS_CONDITION = 'ГДЕ Справочник1.ПометкаУдаления = ЛОЖЬ'
RLS_TMPL_NAME = 'ДляСправочника(Справочник)'
RLS_TMPL_TEXT = '// шаблон RLS'

# СКД-запрос: в cf это XML внутри бинарного макета (8-байтовый заголовок +
# текст, см. test_writer.test_extract_skd_queries), в dump — текстовый
# Ext\\Template.xml; извлекающий регулярен один и тот же, текст запроса совпадает
SKD_XML_CF = (
    '<?xml version="1.0"?>'
    '<DataCompositionSchema '
    'xmlns="http://v8.1c.ru/2008/10/data-composition-schema">'
    '<dataSets><dataSet><query>' + SKD_QUERY + '</query></dataSet></dataSets>'
    '</DataCompositionSchema>')
SKD_BIN = b'\x00\x00\x00\x00\x01\x00\x00\x00' + SKD_XML_CF.encode('utf-8')
SKD_XML_DUMP = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<DataCompositionSchema '
    'xmlns="http://v8.1c.ru/8.1/data-composition-system/schema">\n'
    '\t<dataSource><name>Источник</name></dataSource>\n'
    '\t<dataSet xsi:type="DataSetQuery">\n'
    '\t\t<query>' + SKD_QUERY + '</query>\n'
    '\t</dataSet>\n'
    '</DataCompositionSchema>')

# права роли в формате скобкофайла (как Role.0.c1brace): три цели —
# конфигурация (Read=1), справочник (Read=1 с RLS-условием, View=-1) и его
# реквизит (View=1); один именованный шаблон RLS
ROLE_RIGHTS_CFBRACE = (
    '{10,\n'
    '{3,\n'
    '{\n'
    '{1,%s,0,0},\n'
    '{0,%s,1}\n'
    '},\n'
    '{\n'
    '{1,%s,0,0},\n'
    '{1,2,%s,1,%s,-1,1,\n'
    '{%s,\n'
    '{1,\n'
    '{1,"%s",0}\n'
    '}\n'
    '}\n'
    '}\n'
    '},\n'
    '{\n'
    '{1,%s,0,0},\n'
    '{0,%s,1}\n'
    '}\n'
    '},\n'
    '{1,\n'
    '{"%s","%s"}\n'
    '},4294967295,1,0,4294967295}'
) % (CFG_UUID, READ_UUID,
     CAT_UUID, READ_UUID, VIEW_UUID, READ_UUID, RLS_CONDITION,
     ATTR_STR_UUID, VIEW_UUID,
     RLS_TMPL_NAME, RLS_TMPL_TEXT)

RIGHTS_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Rights xmlns="http://v8.1c.ru/8.2/roles" version="2.20">\n'
    '\t<setForNewObjects>false</setForNewObjects>\n'
    '\t<setForAttributesByDefault>true</setForAttributesByDefault>\n'
    '\t<independentRightsOfChildObjects>false'
    '</independentRightsOfChildObjects>\n'
    '\t<object>\n'
    '\t\t<name>Configuration.ТестКонф</name>\n'
    '\t\t<right>\n'
    '\t\t\t<name>Read</name>\n'
    '\t\t\t<value>true</value>\n'
    '\t\t</right>\n'
    '\t</object>\n'
    '\t<object>\n'
    '\t\t<name>Catalog.Справочник1</name>\n'
    '\t\t<right>\n'
    '\t\t\t<name>Read</name>\n'
    '\t\t\t<value>true</value>\n'
    '\t\t\t<restrictionByCondition>\n'
    '\t\t\t\t<condition>%s</condition>\n'
    '\t\t\t</restrictionByCondition>\n'
    '\t\t</right>\n'
    '\t\t<right>\n'
    '\t\t\t<name>View</name>\n'
    '\t\t\t<value>false</value>\n'
    '\t\t</right>\n'
    '\t</object>\n'
    '\t<object>\n'
    '\t\t<name>Catalog.Справочник1.Attribute.РеквизитСтрока</name>\n'
    '\t\t<right>\n'
    '\t\t\t<name>View</name>\n'
    '\t\t\t<value>true</value>\n'
    '\t\t</right>\n'
    '\t</object>\n'
    '\t<restrictionTemplate>\n'
    '\t\t<name>%s</name>\n'
    '\t\t<condition>%s</condition>\n'
    '\t</restrictionTemplate>\n'
    '</Rights>'
) % (RLS_CONDITION, RLS_TMPL_NAME, RLS_TMPL_TEXT)


# --- фикстура 1: дерево стадии 3 (вход write_db) ------------------------------

def make_cf_tree(base):
    """Мини-конфигурация в формате выхода декодера .cf (стадия 3)."""
    _json(os.path.join(base, 'Configuration.json'), {
        'v8unpack': '1.9.0',
        'uuid': CONTAINER_UUID,       # внутренний uuid контейнера
        'file_uuid': CFG_UUID,        # uuid метаданных Configuration
        'root': [['2', CFG_UUID, '']],
        'name': 'ТестКонф', 'comment': 'комментарий',
        'name2': {'ru': 'Тестовая конфигурация'},
        'compatibility_version': '80321',
        'obj_version': '80321',
        'header': [['2', [CFG_UUID], '7',
                    ['9cd510cd-abfc-11d4-9434-004095e12fc7',
                     ['1', _cfg_props()]]]],
    })
    _json(os.path.join(base, 'Configuration.10.json'),
          [['2', 'a', 'b', [['1', [REF_CAT, '"Справочник1"']]], '0']])

    cat_dir = os.path.join(base, 'Catalog', 'Справочник1')
    _json(os.path.join(cat_dir, 'Catalog.json'), {
        'name': 'Справочник1', 'comment': 'коммент справочника',
        'obj_version': '803',
        'header': [
            ['2', _core('РеквизитСтрока', ATTR_STR_UUID),
             ['"Pattern"', ['"S"', '10', '1']]],
            ['2', _core('РодительСсылка', ATTR_REF_UUID),
             ['"Pattern"', ['"#"', REF_CAT]]],
            # табличная часть Товары с полем Количество
            ['1', ['11', TS_COLL_UUID, ['0', _flag_desc(TS_UUID, 'Товары')]],
             '0', ['0'], ['1', '"ru"', '"Товары"']],
            '1',
            [VT_FIELDS_KEY, '1',
             ['8', ['27', ['2', _core('Количество', TSF_UUID),
                           ['"Pattern"', ['"N"', '15', '3', '1']]]], '0']],
        ],
    })
    _json(os.path.join(cat_dir, 'Catalog.id.json'), {'uuid': CAT_UUID})
    _write(os.path.join(cat_dir, 'Catalog.obj.bsl'), CAT_OBJ_BSL)
    # предопределённые: корневой узел «Элементы» и один элемент
    cat_cols = [_pcol(0, [['"#"', PREDEF_TYPE]]), _pcol(1, [['"B"']]),
                _pcol(2, [['"#"', PREDEF_TYPE]]), _pcol(3, [['"S"']]),
                _pcol(4, [['"S"', '9', '1']]), _pcol(5, [['"S"', '150', '1']])]
    _write(os.path.join(cat_dir, 'Предустановленные данные.bin'),
           _pfile(cat_cols, [
               _prow(0, [_puuid(ZERO_UUID), _pbool(1), _puuid(ZERO_UUID),
                         _pstr('Элементы'), _pstr(''), _pstr('')], [
                   _prow(1, [_puuid(PREDEF_UUID), _pbool(0), _puuid(ZERO_UUID),
                             _pstr('ПредЭлемент'), _pstr('001'),
                             _pstr('Предопределённый')])])], tag='0'))

    form_dir = os.path.join(cat_dir, 'CatalogForm', 'ФормаЭлемента')
    _json(os.path.join(form_dir, 'CatalogForm.json'),
          {'name': 'ФормаЭлемента', 'obj_version': '803', 'header': {},
           'form': [[['1', 'x', ['1', 'cccccccc-0000-0000-0000-00000000f017',
                                 '"ПриСозданииНаСервере"', '1', '0']]]]})
    _json(os.path.join(form_dir, 'CatalogForm.id.json'), {'uuid': FORM_UUID})
    _write(os.path.join(form_dir, 'CatalogForm.obj.bsl'), FORM_BSL)
    # содержимое формы: реквизит/команда/параметр (как в реальном elem.json)
    _json(os.path.join(form_dir, 'CatalogForm.elem.json'), {
        'params': [{'name': 'Параметр1',
                    'raw': ['0', '"Параметр1"', ['"Pattern"', ['"S"']], '1']}],
        'props': [{'name': 'Объект', 'id': '1',
                   'raw': ['9', ['1'], '0', '"Объект"', ['1', '0'],
                           ['"Pattern"', ['"#"', 'Родитель']],
                           ['0'], ['0'], ['0'], ['0'], '1', '1']}],
        'commands': [{'name': 'Команда1', 'id': '1',
                      'raw': ['9', ['1', 'uuid'], '"Команда1"', ['t'], ['t'],
                              ['0', '0'], ['0'], ['0'], '"Команда1"', '3']}],
        'tree': [], 'data': {},
    })

    cm_dir = os.path.join(base, 'CommonModule', 'ОбщийМодуль1')
    _json(os.path.join(cm_dir, 'CommonModule.json'), {
        'name': 'ОбщийМодуль1', 'obj_version': '803',
        # флаги rec[2:]: позиция 1 = Сервер → контекст «Сервер»
        'header': [['1', ['81', ZERO_UUID,
                          '0', '1', '0', '0', '0', '0', '0', '0']]],
    })
    _json(os.path.join(cm_dir, 'CommonModule.id.json'), {'uuid': CM_UUID})
    _write(os.path.join(cm_dir, 'CommonModule.obj.bsl'), CM_BSL)

    enum_dir = os.path.join(base, 'Enum', 'Перечисление1')
    _json(os.path.join(enum_dir, 'Enum.json'), {
        'name': 'Перечисление1', 'obj_version': '803',
        'header': [[[[0, _core('Значение1')], 0], [[0, _core('Значение2')], 0]]],
    })
    _json(os.path.join(enum_dir, 'Enum.id.json'), {'uuid': ENUM_UUID})
    # модуль менеджера перечисления (поток mgr — как после правки ext_code)
    _write(os.path.join(enum_dir, 'Enum.mgr.bsl'), ENUM_MGR_BSL)

    rep_dir = os.path.join(base, 'Report', 'Отчет1')
    _json(os.path.join(rep_dir, 'Report.json'),
          {'name': 'Отчет1', 'obj_version': '803', 'header': {}})
    _json(os.path.join(rep_dir, 'Report.id.json'), {'uuid': REPORT_UUID})
    tmpl_dir = os.path.join(rep_dir, 'Template',
                            'ОсновнаяСхемаКомпоновкиДанных')
    _json(os.path.join(tmpl_dir, 'Template.json'),
          {'name': 'ОсновнаяСхемаКомпоновкиДанных', 'obj_version': '803',
           'header': {}})
    _json(os.path.join(tmpl_dir, 'Template.id.json'), {'uuid': TMPL_UUID})
    _write(os.path.join(tmpl_dir, 'Template.bin'), SKD_BIN)

    ir_dir = os.path.join(base, 'InformationRegister', 'РегистрСведений1')
    _json(os.path.join(ir_dir, 'InformationRegister.json'), {
        'name': 'РегистрСведений1', 'obj_version': '803',
        'header': _register_header(
            ['33'] + ['0'] * 17 + ['4', '1', '0'],
            [[IR_RESOURCES, '1',
              _attr('Ресурс1', ['"Pattern"', ['"N"', '15', '3', '1']],
                    uuid=IR_RES_UUID, marker='1')],
             [IR_DIMENSIONS, '1',
              _attr('Измерение1', ['"Pattern"', ['"#"', REF_CAT]],
                    uuid=IR_DIM_UUID)],
             [IR_FORMS, '0'],
             [IR_ATTRIBUTES, '1',
              _attr('Реквизит1', ['"Pattern"', ['"S"', '10', '1']],
                    uuid=IR_ATTR_UUID)]]),
    })
    _json(os.path.join(ir_dir, 'InformationRegister.id.json'),
          {'uuid': IR_UUID})

    role_dir = os.path.join(base, 'Role', 'Роль1')
    _json(os.path.join(role_dir, 'Role.json'), {
        'name': 'Роль1', 'obj_version': '803',
        'name2': {'ru': 'Роль 1'},
        'header': [['1', ['6', ['3', ['1', '0', 'в отдельном файле'],
                                 '"Роль1"', ['1', '"ru"', '"Роль 1"'],
                                 '""', '0', '0', ZERO_UUID, '0'],
                          '1', '0', '0'], '0']],
    })
    _json(os.path.join(role_dir, 'Role.id.json'), {'uuid': ROLE_UUID})
    _write(os.path.join(role_dir, 'Role.0.c1brace'), ROLE_RIGHTS_CFBRACE)

    # HTTP-сервис: RootURL + URL-шаблон + ДВА метода (ревизия 13: глагол —
    # код m[2], не имя метода; шаблон с несколькими методами не теряет их)
    http_dir = os.path.join(base, 'HTTPService', 'Сервис1')
    _json(os.path.join(http_dir, 'HTTPService.json'), {
        'name': 'Сервис1', 'obj_version': '803',
        'header': [['1', ['2', '"api"', ['core'], '0', '20'], '1',
                    ['h0000000-0000-0000-0000-000000000001', '1',
                     [['0', '"/version"', ['core']], '1',
                      ['h0000000-0000-0000-0000-000000000002', '2',
                       [['0', '"ПолучитьВерсию"', '3',
                         ['3', ['1', '0', 'u'], '"ПолучитьВерсию"'], '0'],
                        ['0', '"ДобавитьВерсию"', '11',
                         ['3', ['1', '0', 'u'], '"ДобавитьВерсию"'], '0']]]]]]],
    })
    _json(os.path.join(http_dir, 'HTTPService.id.json'),
          {'uuid': 'h0000000-0000-0000-0000-000000000000'})
    # веб-сервис: Namespace + операция
    ws_dir = os.path.join(base, 'WebService', 'ВебСервис1')
    _json(os.path.join(ws_dir, 'WebService.json'), {
        'name': 'ВебСервис1', 'obj_version': '803',
        'header': [['1', ['4', '"http://test/ns"', ['core']], '1',
                    ['w0000000-0000-0000-0000-000000000001', '1',
                     [['1', ['1', ['0', '0', 'u'], '"Ping"'],
                       ['0', '"http://www.w3.org/2001/XMLSchema"', '"string"'],
                       '1', '1', '"ОбработатьЗапрос"', '1'], '1', []]]]],
    })
    _json(os.path.join(ws_dir, 'WebService.id.json'),
          {'uuid': 'w0000000-0000-0000-0000-000000000000'})


# --- фикстура 2: иерархическая выгрузка (вход build_db) -----------------------

def make_xml_dump(base):
    """Та же конфигурация в формате выгрузки конфигуратора в файлы."""
    _w(base, 'ConfigDumpInfo.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<ConfigDumpInfo xmlns="http://v8.1c.ru/8.3/xcf/dumpinfo" '
       'format="Hierarchical" version="2.20"><ConfigVersions/></ConfigDumpInfo>')
    _w(base, 'Configuration.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Configuration uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>ТестКонф</Name>\n'
       '\t\t\t<Synonym><v8:item><v8:lang>ru</v8:lang>'
       '<v8:content>Тестовая конфигурация</v8:content></v8:item></Synonym>\n'
       '\t\t\t<Comment>комментарий</Comment>\n'
       '\t\t\t<CompatibilityMode>Version8_3_21</CompatibilityMode>\n'
       '\t\t</Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<Catalog>Справочник1</Catalog>\n'
       '\t\t\t<CommonModule>ОбщийМодуль1</CommonModule>\n'
       '\t\t\t<Enum>Перечисление1</Enum>\n'
       '\t\t\t<Report>Отчет1</Report>\n'
       '\t\t\t<Role>Роль1</Role>\n'
       '\t\t\t<InformationRegister>РегистрСведений1'
       '</InformationRegister>\n'
       '\t\t</ChildObjects>\n'
       '\t</Configuration>\n'
       '</MetaDataObject>' % (NS, CFG_UUID))
    _w(base, 'Catalogs/Справочник1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Catalog uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>Справочник1</Name>\n'
       '\t\t\t<Comment>коммент справочника</Comment>\n'
       '\t\t</Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<Attribute uuid="%s">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>РеквизитСтрока</Name>\n'
       '\t\t\t\t\t<Type>\n'
       '\t\t\t\t\t\t<v8:Type>xs:string</v8:Type>\n'
       '\t\t\t\t\t\t<v8:StringQualifiers>\n'
       '\t\t\t\t\t\t\t<v8:Length>10</v8:Length>\n'
       '\t\t\t\t\t\t\t<v8:AllowedLength>Variable'
       '</v8:AllowedLength>\n'
       '\t\t\t\t\t\t</v8:StringQualifiers>\n'
       '\t\t\t\t\t</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Attribute>\n'
       '\t\t\t<Attribute uuid="%s">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>РодительСсылка</Name>\n'
       '\t\t\t\t\t<Type>\n'
       '\t\t\t\t\t\t<v8:Type>cfg:CatalogRef.Справочник1</v8:Type>\n'
       '\t\t\t\t\t</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Attribute>\n'
       '\t\t\t<TabularSection uuid="%s">\n'
       '\t\t\t\t<Properties><Name>Товары</Name></Properties>\n'
       '\t\t\t\t<ChildObjects>\n'
       '\t\t\t\t\t<Attribute uuid="%s">\n'
       '\t\t\t\t\t\t<Properties>\n'
       '\t\t\t\t\t\t\t<Name>Количество</Name>\n'
       '\t\t\t\t\t\t\t<Type><v8:Type>xs:decimal</v8:Type>\n'
       '\t\t\t\t\t\t\t\t<v8:NumberQualifiers>\n'
       '\t\t\t\t\t\t\t\t\t<v8:Digits>15</v8:Digits>\n'
       '\t\t\t\t\t\t\t\t\t<v8:Scale>3</v8:Scale>\n'
       '\t\t\t\t\t\t\t\t</v8:NumberQualifiers>\n'
       '\t\t\t\t\t\t\t</Type>\n'
       '\t\t\t\t\t\t</Properties>\n'
       '\t\t\t\t\t</Attribute>\n'
       '\t\t\t\t</ChildObjects>\n'
       '\t\t\t</TabularSection>\n'
       '\t\t\t<Form>ФормаЭлемента</Form>\n'
       '\t\t</ChildObjects>\n'
       '\t</Catalog>\n'
       '</MetaDataObject>'
       % (NS, CAT_UUID, ATTR_STR_UUID, ATTR_REF_UUID, TS_UUID, TSF_UUID))
    _w(base, 'Catalogs/Справочник1/Ext/ObjectModule.bsl', CAT_OBJ_BSL)
    _w(base, 'Catalogs/Справочник1/Ext/Predefined.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<PredefinedData xmlns="http://v8.1c.ru/8.3/xcf/predef" '
       'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
       'xsi:type="CatalogPredefinedItems" version="2.20">\n'
       '\t<Item id="%s">\n'
       '\t\t<Name>ПредЭлемент</Name>\n'
       '\t\t<Code>001</Code>\n'
       '\t\t<Description>Предопределённый</Description>\n'
       '\t\t<IsFolder>false</IsFolder>\n'
       '\t</Item>\n'
       '</PredefinedData>' % PREDEF_UUID)
    _w(base, 'Catalogs/Справочник1/Forms/ФормаЭлемента.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Form uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>ФормаЭлемента</Name>\n'
       '\t\t\t<FormType>ManagedForm</FormType>\n'
       '\t\t</Properties>\n'
       '\t</Form>\n'
       '</MetaDataObject>' % (NS, FORM_UUID))
    _w(base, 'Catalogs/Справочник1/Forms/ФормаЭлемента/Ext/Form.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" '
       'xmlns:v8="http://v8.1c.ru/8.1/data/core" version="2.20">\n'
       '\t<Attributes>\n'
       '\t\t<Attribute name="Объект" id="1">\n'
       '\t\t\t<Type><v8:Type>cfg:CatalogObject.Справочник1</v8:Type></Type>\n'
       '\t\t\t<MainAttribute>true</MainAttribute>\n'
       '\t\t\t<SavedData>true</SavedData>\n'
       '\t\t</Attribute>\n'
       '\t</Attributes>\n'
       '\t<Commands><Command name="Команда1"><Action>Команда1</Action>'
       '</Command></Commands>\n'
       '\t<Events><Event name="OnCreateAtServer">ПриСозданииНаСервере</Event>'
       '</Events>\n'
       '\t<Parameters><Parameter name="Параметр1">'
       '<Type><v8:Type>xs:string</v8:Type></Type>'
       '<KeyParameter>true</KeyParameter></Parameter></Parameters>\n'
       '</Form>')
    _w(base, 'Catalogs/Справочник1/Forms/ФормаЭлемента/Ext/Form/Module.bsl',
       FORM_BSL)
    _w(base, 'CommonModules/ОбщийМодуль1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<CommonModule uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>ОбщийМодуль1</Name>\n'
       '\t\t\t<Global>false</Global>\n'
       '\t\t\t<ClientManagedApplication>false</ClientManagedApplication>\n'
       '\t\t\t<Server>true</Server>\n'
       '\t\t\t<ExternalConnection>false</ExternalConnection>\n'
       '\t\t\t<ClientOrdinaryApplication>false</ClientOrdinaryApplication>\n'
       '\t\t\t<ServerCall>false</ServerCall>\n'
       '\t\t\t<ReturnValuesReuse>DontUse</ReturnValuesReuse>\n'
       '\t\t</Properties>\n'
       '\t</CommonModule>\n'
       '</MetaDataObject>' % (NS, CM_UUID))
    _w(base, 'CommonModules/ОбщийМодуль1/Ext/Module.bsl', CM_BSL)
    _w(base, 'Enums/Перечисление1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Enum uuid="%s">\n'
       '\t\t<Properties><Name>Перечисление1</Name></Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<EnumValue uuid="%s"><Properties>'
       '<Name>Значение1</Name>'
        '<Synonym><v8:item><v8:lang>ru</v8:lang>'
        '<v8:content>Значение1</v8:content></v8:item></Synonym>'
        '</Properties></EnumValue>\n'
       '\t\t\t<EnumValue uuid="%s"><Properties>'
       '<Name>Значение2</Name>'
        '<Synonym><v8:item><v8:lang>ru</v8:lang>'
        '<v8:content>Значение2</v8:content></v8:item></Synonym>'
        '</Properties></EnumValue>\n'
       '\t\t</ChildObjects>\n'
       '\t</Enum>\n'
       '</MetaDataObject>' % (NS, ENUM_UUID, EV1_UUID, EV2_UUID))
    _w(base, 'Enums/Перечисление1/Ext/ManagerModule.bsl', ENUM_MGR_BSL)
    _w(base, 'Reports/Отчет1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Report uuid="%s">\n'
       '\t\t<Properties><Name>Отчет1</Name></Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<Template>ОсновнаяСхемаКомпоновкиДанных</Template>\n'
       '\t\t</ChildObjects>\n'
       '\t</Report>\n'
       '</MetaDataObject>' % (NS, REPORT_UUID))
    _w(base, 'Reports/Отчет1/Templates/ОсновнаяСхемаКомпоновкиДанных.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Template uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>ОсновнаяСхемаКомпоновкиДанных</Name>\n'
       '\t\t\t<TemplateType>DataCompositionSchema</TemplateType>\n'
       '\t\t</Properties>\n'
       '\t</Template>\n'
       '</MetaDataObject>' % (NS, TMPL_UUID))
    # Template.xml пишем БАЙТАМИ: текстовый режим _w превратил бы \n запроса
    # в CRLF, а cf-сторона хранит макет бинарным (текст побайтно одинаков)
    _write(os.path.join(base, 'Reports', 'Отчет1', 'Templates',
                        'ОсновнаяСхемаКомпоновкиДанных', 'Ext', 'Template.xml'),
           SKD_XML_DUMP.encode('utf-8'))
    _w(base, 'InformationRegisters/РегистрСведений1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<InformationRegister uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>РегистрСведений1</Name>\n'
       '\t\t\t<InformationRegisterPeriodicity>Month'
       '</InformationRegisterPeriodicity>\n'
       '\t\t\t<WriteMode>Independent</WriteMode>\n'
       '\t\t</Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<Resource uuid="%s">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>Ресурс1</Name>\n'
       '\t\t\t\t\t<Type><v8:Type>xs:decimal</v8:Type>\n'
       '\t\t\t\t\t\t<v8:NumberQualifiers><v8:Digits>15</v8:Digits>'
       '<v8:Scale>3</v8:Scale></v8:NumberQualifiers>\n'
       '\t\t\t\t\t</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Resource>\n'
       '\t\t\t<Dimension uuid="%s">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>Измерение1</Name>\n'
       '\t\t\t\t\t<Type><v8:Type>cfg:CatalogRef.Справочник1</v8:Type>'
       '</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Dimension>\n'
       '\t\t\t<Attribute uuid="%s">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>Реквизит1</Name>\n'
       '\t\t\t\t\t<Type><v8:Type>xs:string</v8:Type>\n'
       '\t\t\t\t\t\t<v8:StringQualifiers><v8:Length>10</v8:Length>'
       '<v8:AllowedLength>Variable</v8:AllowedLength>'
       '</v8:StringQualifiers>\n'
       '\t\t\t\t\t</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Attribute>\n'
       '\t\t</ChildObjects>\n'
       '\t</InformationRegister>\n'
       '</MetaDataObject>'
       % (NS, IR_UUID, IR_RES_UUID, IR_DIM_UUID, IR_ATTR_UUID))
    _w(base, 'Roles/Роль1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Role uuid="%s">\n'
       '\t\t<Properties><Name>Роль1</Name></Properties>\n'
       '\t</Role>\n'
       '</MetaDataObject>' % (NS, ROLE_UUID))
    _w(base, 'Roles/Роль1/Ext/Rights.xml', RIGHTS_XML)
    # HTTP-сервис и веб-сервис (ревизия 13) — паритет с cf-фикстурой:
    # два метода шаблона (GET и POST), глагол из атрибута HTTPMethod
    _w(base, 'HTTPServices/Сервис1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<HTTPService uuid="h0000000-0000-0000-0000-000000000000">\n'
       '\t\t<Properties><Name>Сервис1</Name><RootURL>api</RootURL></Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<URLTemplate uuid="h0000000-0000-0000-0000-000000000001">\n'
       '\t\t\t\t<Properties><Name>Версия</Name><Template>/version</Template></Properties>\n'
       '\t\t\t\t<ChildObjects>\n'
       '\t\t\t\t\t<Method uuid="h0000000-0000-0000-0000-000000000002">\n'
       '\t\t\t\t\t\t<Properties><Name>ПолучитьВерсию</Name><HTTPMethod>GET</HTTPMethod>'
       '<Handler>ПолучитьВерсию</Handler></Properties>\n'
       '\t\t\t\t\t</Method>\n'
       '\t\t\t\t\t<Method uuid="h0000000-0000-0000-0000-000000000003">\n'
       '\t\t\t\t\t\t<Properties><Name>ДобавитьВерсию</Name><HTTPMethod>POST</HTTPMethod>'
       '<Handler>ДобавитьВерсию</Handler></Properties>\n'
       '\t\t\t\t\t</Method>\n'
       '\t\t\t\t</ChildObjects>\n'
       '\t\t\t</URLTemplate>\n'
       '\t\t</ChildObjects>\n'
       '\t</HTTPService>\n'
       '</MetaDataObject>' % NS)
    _w(base, 'WebServices/ВебСервис1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<WebService uuid="w0000000-0000-0000-0000-000000000000">\n'
       '\t\t<Properties><Name>ВебСервис1</Name>'
       '<Namespace>http://test/ns</Namespace></Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<Operation uuid="w0000000-0000-0000-0000-000000000001">\n'
       '\t\t\t\t<Properties><Name>Ping</Name><ProcedureName>ОбработатьЗапрос</ProcedureName>'
       '<XDTOReturningValueType>xs:string</XDTOReturningValueType>'
       '<Transactioned>true</Transactioned></Properties>\n'
       '\t\t\t</Operation>\n'
       '\t\t</ChildObjects>\n'
       '\t</WebService>\n'
       '</MetaDataObject>' % NS)


# --- сборка и сравнение -------------------------------------------------------

QUERIES = {
    'source':
        'SELECT root_type, root_name, root_uuid, extractor_version FROM source',
    'meta_object': """
        SELECT o.path, o.type, o.type_ru, o.name, o.uuid,
               COALESCE(o.comment, ''), COALESCE(p.path, '')
        FROM meta_object o
        LEFT JOIN meta_object p ON p.id = o.parent_id
        ORDER BY o.path""",
    'meta_attribute': """
        SELECT o.path, a.ord, a.name, a.type_str, COALESCE(a.tabular, ''),
               COALESCE(a.uuid, '')
        FROM meta_attribute a JOIN meta_object o ON o.id = a.object_id
        ORDER BY o.path, a.ord""",
    'meta_tabular': """
        SELECT o.path, t.ord, t.name, t.uuid
        FROM meta_tabular t JOIN meta_object o ON o.id = t.object_id
        ORDER BY o.path, t.ord""",
    'attribute_ref': """
        SELECT o.path, a.name, COALESCE(a.tabular, ''), r.ord, r.target_path,
               COALESCE(t.path, '')
        FROM attribute_ref r
        JOIN meta_attribute a ON a.id = r.attribute_id
        JOIN meta_object o ON o.id = a.object_id
        LEFT JOIN meta_object t ON t.id = r.object_id
        ORDER BY o.path, a.name, r.ord""",
    'module': """
        SELECT o.path, m.code_name, COALESCE(m.context, ''), m.body
        FROM module m JOIN meta_object o ON o.id = m.object_id
        ORDER BY o.path, m.code_name""",
    'method': """
        SELECT o.path, m.code_name, me.ord, me.kind, me.name, me.signature,
               me.is_export, me.directives, COALESCE(me.description, ''),
               me.line_start, me.line_end, me.body
        FROM method me
        JOIN module m ON m.id = me.module_id
        JOIN meta_object o ON o.id = m.object_id
        ORDER BY o.path, m.code_name, me.ord""",
    'enum_value': """
        SELECT o.path, e.ord, e.name, e.synonym
        FROM enum_value e JOIN meta_object o ON o.id = e.object_id
        ORDER BY o.path, e.ord""",
    'form_attribute': """
        SELECT o.path, f.ord, f.name, f.type_str, f.main_attr, f.saved_data
        FROM form_attribute f JOIN meta_object o ON o.id = f.form_id
        ORDER BY o.path, f.ord""",
    'form_command': """
        SELECT o.path, f.ord, f.name, f.action, f.modifies_data
        FROM form_command f JOIN meta_object o ON o.id = f.form_id
        ORDER BY o.path, f.ord""",
    'form_event': """
        SELECT o.path, f.handler
        FROM form_event f JOIN meta_object o ON o.id = f.form_id
        ORDER BY o.path, f.ord""",
    'form_parameter': """
        SELECT o.path, f.ord, f.name, f.type_str, f.key_param
        FROM form_parameter f JOIN meta_object o ON o.id = f.form_id
        ORDER BY o.path, f.ord""",
    'http_method': """
        SELECT o.path, m.ord, m.url_template, m.http_method, m.handler
        FROM http_method m JOIN meta_object o ON o.id = m.object_id
        ORDER BY o.path, m.ord""",
    'webservice_operation': """
        SELECT o.path, w.ord, w.name, w.procedure_name, w.return_type,
               w.transactioned
        FROM webservice_operation w JOIN meta_object o ON o.id = w.object_id
        ORDER BY o.path, w.ord""",
    'predefined': """
        SELECT o.path, p.ord, p.parent_ord, p.uuid, p.name, p.code, p.display
        FROM predefined p JOIN meta_object o ON o.id = p.object_id
        ORDER BY o.path, p.ord""",
    'skd_query': """
        SELECT o.path, s.ord, s.query
        FROM skd_query s JOIN meta_object o ON o.id = s.object_id
        ORDER BY o.path, s.ord""",
    'role_right': """
        SELECT ro.path, rr.target_uuid, COALESCE(t2.path, ''),
               COALESCE(ta.name, ''), COALESCE(tt.name, ''),
               rr.right_uuid, rr.value, COALESCE(rr.rls_text, '')
        FROM role_right rr
        JOIN meta_object ro ON ro.id = rr.role_id
        LEFT JOIN meta_object t2 ON t2.id = rr.target_object_id
        LEFT JOIN meta_attribute ta ON ta.id = rr.target_attr_id
        LEFT JOIN meta_tabular tt ON tt.id = rr.target_tabular_id
        ORDER BY ro.path, rr.target_uuid, rr.right_uuid""",
    'role_rls_template': """
        SELECT ro.path, t.ord, t.name, t.text
        FROM role_rls_template t JOIN meta_object ro ON ro.id = t.role_id
        ORDER BY ro.path, t.ord""",
    'role_rights_state': """
        SELECT ro.path, s.parsed, s.targets, s.rights, s.rls_templates,
               COALESCE(s.error, '')
        FROM role_rights_state s JOIN meta_object ro ON ro.id = s.role_id
        ORDER BY ro.path""",
}


@pytest.fixture(scope='module')
def bases(tmp_path_factory):
    """Строит обе базы один раз на модуль и отдаёт пару соединений."""
    cf_dir = str(tmp_path_factory.mktemp('cf') / 'stage3')
    xml_dir = str(tmp_path_factory.mktemp('xml') / 'dump')
    os.makedirs(cf_dir)
    os.makedirs(xml_dir)
    make_cf_tree(cf_dir)
    make_xml_dump(xml_dir)
    cf_db = str(tmp_path_factory.mktemp('dbcf') / 'cf.sqlite')
    dump_db = str(tmp_path_factory.mktemp('dbdump') / 'dump.sqlite')
    write_db(cf_dir, cf_db, source_file='parity.cf', build_fts=False)
    build_db(xml_dir, dump_db, workers=1, build_fts=False, log=lambda *a: None)
    c1 = sqlite3.connect(cf_db)
    c2 = sqlite3.connect(dump_db)
    try:
        yield c1, c2
    finally:
        c1.close()
        c2.close()


def _assert_same(table, rows_cf, rows_dump):
    if rows_cf == rows_dump:
        return
    msg = [f'{table}: cf {len(rows_cf)} строк, dump {len(rows_dump)}']
    for i, (a, b) in enumerate(zip(rows_cf, rows_dump)):
        if a != b:
            msg.append(f'первое расхождение, строка {i}:\n  cf:   {a}\n  dump: {b}')
            break
    else:
        tail = (rows_cf + rows_dump)[min(len(rows_cf), len(rows_dump)):]
        msg.append(f'хвост одной из баз: {tail[:5]}')
    raise AssertionError('\n'.join(msg))


@pytest.mark.parametrize('table', sorted(QUERIES))
def test_table_parity(bases, table):
    """Строки таблицы (в сравниваемых колонках) одинаковы в обеих базах."""
    c1, c2 = bases
    _assert_same(table, c1.execute(QUERIES[table]).fetchall(),
                 c2.execute(QUERIES[table]).fetchall())


def test_fixtures_produce_expected_content(bases):
    """Фикстуры непустые: сравнение выше не проходит «вхолостую»."""
    c1, c2 = bases
    for conn in (c1, c2):
        assert conn.execute(
            'SELECT COUNT(*) FROM meta_object').fetchone()[0] == 11
        assert conn.execute(
            'SELECT COUNT(*) FROM module').fetchone()[0] == 4
        assert conn.execute(
            'SELECT COUNT(*) FROM role_right').fetchone()[0] == 4
        assert conn.execute(
            'SELECT COUNT(*) FROM skd_query').fetchone()[0] == 1
        assert conn.execute(
            'SELECT COUNT(*) FROM predefined').fetchone()[0] == 2
        assert conn.execute(
            'SELECT COUNT(*) FROM http_method').fetchone()[0] == 2
        assert conn.execute(
            'SELECT COUNT(*) FROM webservice_operation').fetchone()[0] == 1
        assert conn.execute(
            'SELECT COUNT(*) FROM attribute_ref').fetchone()[0] == 2
        # все ссылки и цели прав разрешены
        assert conn.execute(
            'SELECT COUNT(*) FROM attribute_ref'
            ' WHERE object_id IS NULL').fetchone()[0] == 0
        assert conn.execute(
            'SELECT COUNT(*) FROM role_right'
            ' WHERE target_object_id IS NULL').fetchone()[0] == 0


def test_root_uuid_is_metadata_uuid(bases):
    """Корень обеих баз несёт uuid метаданных Configuration (C2-унификация).

    Внутренний uuid файла-контейнера .cf в базу не попадает, поэтому права на
    конфигурацию разрешаются в корневой объект в обеих базах.
    """
    c1, c2 = bases
    uuids = [r[0] for r in c1.execute('SELECT uuid FROM meta_object')]
    assert CONTAINER_UUID not in uuids
    for conn in (c1, c2):
        assert conn.execute(
            "SELECT uuid FROM meta_object WHERE path=''").fetchone()[0] == CFG_UUID
        row = conn.execute(
            'SELECT target_object_id FROM role_right'
            ' WHERE target_uuid=?', (CFG_UUID,)).fetchone()
        assert row is not None
        assert row[0] == conn.execute(
            "SELECT id FROM meta_object WHERE path=''").fetchone()[0]
