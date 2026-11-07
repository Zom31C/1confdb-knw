"""Тесты сборки базы знаний из выгрузки конфигурации в файлы (dump-пакет).

Синтетическая мини-выгрузка в формате конфигуратора (Hierarchical 2.20):
справочник с реквизитами/ТЧ/формой/предопределёнными, общий модуль,
перечисление, отчёт с СКД-макетом, роль с правами и RLS.
"""
import os
import sqlite3

import pytest

from confdb.dump import build_db, is_dump_dir

MD = 'http://v8.1c.ru/8.3/MDClasses'
V8 = 'http://v8.1c.ru/8.1/data/core'
XR = 'http://v8.1c.ru/8.3/xcf/readable'
NS = ('xmlns="%s" xmlns:v8="%s" xmlns:xr="%s" xmlns:xs='
      '"http://www.w3.org/2001/XMLSchema" '
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' % (MD, V8, XR))

CFG_UUID = 'e0e0e0e0-0000-0000-0000-000000000001'
CAT_UUID = 'cf336ad9-0000-0000-0000-000000000002'
CAT_ATTR_UUID = 'aaaaaaaa-0000-0000-0000-0000000000a1'
CAT_TS_UUID = 'aaaaaaaa-0000-0000-0000-0000000000a2'
CAT_TS_F_UUID = 'aaaaaaaa-0000-0000-0000-0000000000a3'
CAT_REF_UUID = 'aaaaaaaa-0000-0000-0000-0000000000a4'
FORM_UUID = 'aaaaaaaa-0000-0000-0000-0000000000b1'
CM_UUID = 'aaaaaaaa-0000-0000-0000-0000000000c1'
ENUM_UUID = 'aaaaaaaa-0000-0000-0000-0000000000d1'
EV1_UUID = 'aaaaaaaa-0000-0000-0000-0000000000d2'
EV2_UUID = 'aaaaaaaa-0000-0000-0000-0000000000d3'
REPORT_UUID = 'aaaaaaaa-0000-0000-0000-0000000000e1'
TMPL_UUID = 'aaaaaaaa-0000-0000-0000-0000000000e2'
ROLE_UUID = 'aaaaaaaa-0000-0000-0000-0000000000f1'
PREDEF_UUID = 'aaaaaaaa-0000-0000-0000-0000000000a5'
# uuid вида права Read из намайненной таблицы (RLS-якорь)
READ_UUID = '1c87578f-9e09-4ec0-a991-5629c87b1588'
VIEW_UUID = 'aa6448f2-be0f-42ea-ba26-1af7f52b5b65'


def _w(base, rel, text):
    path = os.path.join(base, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def make_dump_dir(base):
    """Мини-выгрузка конфигурации в формате конфигуратора."""
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
       '\t\t\t<EventSubscription>Подписка1</EventSubscription>\n'
       '\t\t\t<ExchangePlan>ПланОбмена1</ExchangePlan>\n'
       '\t\t</ChildObjects>\n'
       '\t</Configuration>\n'
       '</MetaDataObject>' % (NS, CFG_UUID))
    _w(base, 'Ext/ManagedApplicationModule.bsl',
       'Процедура МодульПриложения()\nКонецПроцедуры')
    _w(base, 'Ext/SessionModule.bsl', '')
    # справочник: строковый реквизит, ссылка на самого себя, ТЧ с полем
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
       % (NS, CAT_UUID, CAT_ATTR_UUID, CAT_REF_UUID, CAT_TS_UUID,
          CAT_TS_F_UUID))
    _w(base, 'Catalogs/Справочник1/Ext/ObjectModule.bsl',
       'Процедура ПриЗаписи(Отказ)\n\tА = 1;\nКонецПроцедуры')
    _w(base, 'Catalogs/Справочник1/Ext/ManagerModule.bsl',
       'Функция МояФункция() Экспорт\n\tВозврат 1;\nКонецФункции')
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
       '<Form xmlns="http://v8.1c.ru/8.3/xcf/logform" version="2.20">\n'
       '\t<Attributes><Attribute><Name>Объект</Name></Attribute></Attributes>\n'
       '</Form>')
    _w(base, 'Catalogs/Справочник1/Forms/ФормаЭлемента/Ext/Form/Module.bsl',
       '&НаКлиенте\nПроцедура Команда1()\nКонецПроцедуры')
    # общий модуль: серверный, экспортный
    _w(base, 'CommonModules/ОбщийМодуль1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<CommonModule uuid="%s">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>ОбщийМодуль1</Name>\n'
       '\t\t\t<Global>false</Global>\n'
       '\t\t\t<ClientManagedApplication>false</ClientManagedApplication>\n'
       '\t\t\t<Server>true</Server>\n'
       '\t\t\t<ExternalConnection>true</ExternalConnection>\n'
       '\t\t\t<ClientOrdinaryApplication>true</ClientOrdinaryApplication>\n'
       '\t\t\t<ServerCall>false</ServerCall>\n'
       '\t\t\t<ReturnValuesReuse>DontUse</ReturnValuesReuse>\n'
       '\t\t</Properties>\n'
       '\t</CommonModule>\n'
       '</MetaDataObject>' % (NS, CM_UUID))
    _w(base, 'CommonModules/ОбщийМодуль1/Ext/Module.bsl',
       'Функция Экспортная(А) Экспорт\n\tВозврат А + 1;\nКонецФункции')
    # перечисление
    _w(base, 'Enums/Перечисление1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Enum uuid="%s">\n'
       '\t\t<Properties><Name>Перечисление1</Name></Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<EnumValue uuid="%s"><Properties>'
       '<Name>Значение1</Name></Properties></EnumValue>\n'
       '\t\t\t<EnumValue uuid="%s"><Properties>'
       '<Name>Значение2</Name></Properties></EnumValue>\n'
       '\t\t</ChildObjects>\n'
       '\t</Enum>\n'
       '</MetaDataObject>' % (NS, ENUM_UUID, EV1_UUID, EV2_UUID))
    # отчёт с СКД-макетом
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
    _w(base, 'Reports/Отчет1/Templates/ОсновнаяСхемаКомпоновкиДанных/'
             'Ext/Template.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<DataCompositionSchema '
       'xmlns="http://v8.1c.ru/8.1/data-composition-system/schema">\n'
       '\t<dataSource><name>Источник</name></dataSource>\n'
       '\t<dataSet xsi:type="DataSetQuery">\n'
       '\t\t<query>ВЫБРАТЬ\n\tСправочник1.Ссылка\nИЗ\n\t'
       'Справочник.Справочник1 КАК Справочник1</query>\n'
       '\t</dataSet>\n'
       '</DataCompositionSchema>')
    # роль: права на справочник (Read true, View false), RLS-шаблон
    _w(base, 'Roles/Роль1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Role uuid="%s">\n'
       '\t\t<Properties><Name>Роль1</Name></Properties>\n'
       '\t</Role>\n'
       '</MetaDataObject>' % (NS, ROLE_UUID))
    _w(base, 'Roles/Роль1/Ext/Rights.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<Rights xmlns="http://v8.1c.ru/8.2/roles" version="2.20">\n'
       '\t<setForNewObjects>false</setForNewObjects>\n'
       '\t<setForAttributesByDefault>true</setForAttributesByDefault>\n'
       '\t<independentRightsOfChildObjects>false'
       '</independentRightsOfChildObjects>\n'
       '\t<object>\n'
       '\t\t<name>Catalog.Справочник1</name>\n'
       '\t\t<right>\n'
       '\t\t\t<name>Read</name>\n'
       '\t\t\t<value>true</value>\n'
       '\t\t\t<restrictionByCondition>\n'
       '\t\t\t\t<condition>ГДЕ Справочник1.ПометкаУдаления = ЛОЖЬ'
       '</condition>\n'
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
       '\t\t<name>ДляСправочника(Справочник)</name>\n'
       '\t\t<condition>// шаблон RLS</condition>\n'
       '\t</restrictionTemplate>\n'
       '</Rights>')
    # регистр сведений: периодичность, режим записи, измерение/ресурс/реквизит
    _w(base, 'InformationRegisters/РегистрСведений1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<InformationRegister uuid="11111111-2222-3333-4444-555555555555">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>РегистрСведений1</Name>\n'
       '\t\t\t<InformationRegisterPeriodicity>Month'
       '</InformationRegisterPeriodicity>\n'
       '\t\t\t<WriteMode>Independent</WriteMode>\n'
       '\t\t</Properties>\n'
       '\t\t<ChildObjects>\n'
       '\t\t\t<Dimension uuid="11111111-0000-0000-0000-0000000000d1">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>Измерение1</Name>\n'
       '\t\t\t\t\t<Type><v8:Type>cfg:CatalogRef.Справочник1</v8:Type>'
       '</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Dimension>\n'
       '\t\t\t<Resource uuid="11111111-0000-0000-0000-0000000000d2">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>Ресурс1</Name>\n'
       '\t\t\t\t\t<Type><v8:Type>xs:decimal</v8:Type>\n'
       '\t\t\t\t\t\t<v8:NumberQualifiers><v8:Digits>10</v8:Digits>'
       '<v8:Scale>2</v8:Scale></v8:NumberQualifiers>\n'
       '\t\t\t\t\t</Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Resource>\n'
       '\t\t\t<Attribute uuid="11111111-0000-0000-0000-0000000000d3">\n'
       '\t\t\t\t<Properties>\n'
       '\t\t\t\t\t<Name>Реквизит1</Name>\n'
       '\t\t\t\t\t<Type><v8:Type>xs:boolean</v8:Type></Type>\n'
       '\t\t\t\t</Properties>\n'
       '\t\t\t</Attribute>\n'
       '\t\t</ChildObjects>\n'
       '\t</InformationRegister>\n'
       '</MetaDataObject>' % NS)
    # подписка на событие
    _w(base, 'EventSubscriptions/Подписка1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<EventSubscription uuid="22222222-3333-4444-5555-666666666666">\n'
       '\t\t<Properties>\n'
       '\t\t\t<Name>Подписка1</Name>\n'
       '\t\t\t<Source>\n'
       '\t\t\t\t<v8:Type>cfg:CatalogObject.Справочник1</v8:Type>\n'
       '\t\t\t</Source>\n'
       '\t\t\t<Event>BeforeWrite</Event>\n'
       '\t\t\t<Handler>CommonModule.ОбщийМодуль1.Экспортная</Handler>\n'
       '\t\t</Properties>\n'
       '\t</EventSubscription>\n'
       '</MetaDataObject>' % NS)
    # план обмена с составом (Ext/Content.xml внедряется в header_json)
    _w(base, 'ExchangePlans/ПланОбмена1.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<ExchangePlan uuid="33333333-4444-5555-6666-777777777777">\n'
       '\t\t<Properties><Name>ПланОбмена1</Name></Properties>\n'
       '\t\t<ChildObjects/>\n'
       '\t</ExchangePlan>\n'
       '</MetaDataObject>' % NS)
    _w(base, 'ExchangePlans/ПланОбмена1/Ext/Content.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<ExchangePlanContent '
       'xmlns="http://v8.1c.ru/8.3/xcf/enums" version="2.20">\n'
       '\t<Item>\n'
       '\t\t<Metadata>Catalog.Справочник1</Metadata>\n'
       '\t\t<AutoRecord>Allow</AutoRecord>\n'
       '\t</Item>\n'
       '\t<Item>\n'
       '\t\t<Metadata>CommonModule.ОбщийМодуль1</Metadata>\n'
       '\t\t<AutoRecord>Deny</AutoRecord>\n'
       '\t</Item>\n'
       '</ExchangePlanContent>')


@pytest.fixture(scope='module')
def db(tmp_path_factory):
    dump = str(tmp_path_factory.mktemp('dump') / 'cfg')
    os.makedirs(dump)
    make_dump_dir(dump)
    path = str(tmp_path_factory.mktemp('db') / 'dump.sqlite')
    build_db(dump, path, workers=1, build_fts=False, log=lambda *a: None)
    conn = sqlite3.connect(path)
    try:
        yield dump, path, conn
    finally:
        conn.close()


def q(db, sql, params=()):
    return db[2].execute(sql, params).fetchall()


def test_is_dump_dir(db):
    assert is_dump_dir(db[0])
    assert not is_dump_dir(os.path.dirname(db[1]))


def test_source_and_root(db):
    _dump, path, conn = db
    row = conn.execute(
        'SELECT file, root_type, root_name, root_uuid, file_sha256,'
        ' file_size FROM source').fetchone()
    assert row[1] == 'Configuration'
    assert row[2] == 'ТестКонф'
    assert row[3] == CFG_UUID
    assert row[4] and len(row[4]) == 64
    assert row[5] > 0
    root = conn.execute(
        "SELECT type, name, uuid, comment, obj_version, parent_id, "
        "header_json FROM meta_object WHERE path=''").fetchone()
    assert root[0] == 'Configuration'
    assert root[1] == 'ТестКонф'
    assert root[3] == 'комментарий'
    assert root[4] == '80321'      # CompatibilityMode Version8_3_21
    assert root[5] is None
    assert '<MetaDataObject' in root[6]     # header_json — исходный XML
    assert conn.execute('PRAGMA user_version').fetchone()[0] >= 9
    # WAL для работы с запущенным сервером
    assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'


def test_objects_paths_and_ord(db):
    rows = dict(q(db, 'SELECT path, type FROM meta_object'))
    assert rows['Catalog/Справочник1'] == 'Catalog'
    assert rows['Catalog/Справочник1/CatalogForm/ФормаЭлемента'] == \
        'CatalogForm'
    assert rows['CommonModule/ОбщийМодуль1'] == 'CommonModule'
    assert rows['Enum/Перечисление1'] == 'Enum'
    assert rows['Report/Отчет1'] == 'Report'
    assert rows['Report/Отчет1/Template/'
                'ОсновнаяСхемаКомпоновкиДанных'] == 'Template'
    assert rows['Role/Роль1'] == 'Role'
    # ord верхнего уровня — порядок ChildObjects конфигурации
    ords = dict(q(db, "SELECT name, ord FROM meta_object WHERE ord IS NOT "
                      "NULL AND parent_id=(SELECT id FROM meta_object "
                      "WHERE path='')"))
    assert ords['Справочник1'] == 0
    assert ords['ОбщийМодуль1'] == 1
    assert ords['Роль1'] == 4
    # форма — ord по списку <Form> в XML родителя
    form_ord = q(db, "SELECT ord FROM meta_object WHERE path="
                     "'Catalog/Справочник1/CatalogForm/ФормаЭлемента'")[0][0]
    assert form_ord == 0


def test_attributes_and_types(db):
    rows = q(db, "SELECT a.name, a.type_str, a.tabular, a.uuid "
                 "FROM meta_attribute a JOIN meta_object o ON o.id=a.object_id "
                 "WHERE o.path='Catalog/Справочник1' ORDER BY a.ord")
    assert rows[0] == ('РеквизитСтрока', 'Строка(10)', None, CAT_ATTR_UUID)
    assert rows[1] == ('РодительСсылка', 'Ссылка: Catalog/Справочник1',
                       None, CAT_REF_UUID)
    assert rows[2] == ('Количество', 'Число', 'Товары', CAT_TS_F_UUID)
    tab = q(db, "SELECT t.name, t.uuid FROM meta_tabular t "
                "JOIN meta_object o ON o.id=t.object_id "
                "WHERE o.path='Catalog/Справочник1'")
    assert tab == [('Товары', CAT_TS_UUID)]


def test_attribute_ref_resolved(db):
    rows = q(db, "SELECT r.uuid, r.object_id, r.target_path FROM "
                 "attribute_ref r JOIN meta_attribute a ON a.id=r.attribute_id "
                 "WHERE a.name='РодительСсылка'")
    assert len(rows) == 1
    uuid, obj_id, target_path = rows[0]
    assert target_path == 'Catalog/Справочник1'
    assert uuid == CAT_UUID           # uuid цели заполнен пост-UPDATE
    cat_id = q(db, "SELECT id FROM meta_object "
                   "WHERE path='Catalog/Справочник1'")[0][0]
    assert obj_id == cat_id


def test_modules_and_methods(db):
    rows = q(db, "SELECT o.path, m.code_name, m.context FROM module m "
                 "JOIN meta_object o ON o.id=m.object_id ORDER BY o.path, "
                 "m.code_name")
    assert ('', 'app', None) in rows
    assert ('Catalog/Справочник1', 'mgr', None) in rows
    assert ('Catalog/Справочник1', 'obj', None) in rows
    assert ('Catalog/Справочник1/CatalogForm/ФормаЭлемента', 'obj', None) \
        in rows
    assert ('CommonModule/ОбщийМодуль1', 'obj',
            'Сервер,Внешнее соединение,Клиент (обычное приложение)') in rows
    meth = q(db, "SELECT t.kind, t.name, t.is_export FROM method t "
                 "JOIN module m ON m.id=t.module_id JOIN meta_object o "
                 "ON o.id=m.object_id WHERE o.path='CommonModule/ОбщийМодуль1'")
    assert meth == [('функция', 'Экспортная', 1)]


def test_enum_and_predefined(db):
    ev = q(db, "SELECT e.ord, e.name FROM enum_value e JOIN meta_object o "
               "ON o.id=e.object_id WHERE o.path='Enum/Перечисление1' "
               "ORDER BY e.ord")
    assert ev == [(0, 'Значение1'), (1, 'Значение2')]
    pre = q(db, "SELECT p.ord, p.parent_ord, p.uuid, p.name, p.code, "
                "p.display FROM predefined p JOIN meta_object o "
                "ON o.id=p.object_id WHERE o.path='Catalog/Справочник1' "
                "ORDER BY p.ord")
    assert pre[0] == (0, None, '0' * 8 + '-0000-0000-0000-000000000000',
                      'Элементы', '', '')
    assert pre[1][3:] == ('ПредЭлемент', '001', 'Предопределённый')


def test_skd_query(db):
    rows = q(db, "SELECT s.ord, s.query FROM skd_query s JOIN meta_object o "
                 "ON o.id=s.object_id WHERE o.path='Report/Отчет1/Template/"
                 "ОсновнаяСхемаКомпоновкиДанных'")
    assert len(rows) == 1
    assert 'Справочник.Справочник1' in rows[0][1]


def test_role_rights(db):
    # право Read=true с RLS на объект; View=false; реквизит View=true
    rows = q(db, "SELECT rr.target_uuid, rr.right_uuid, rr.value, "
                 "rr.target_object_id, rr.target_attr_id, rr.rls_text "
                 "FROM role_right rr JOIN meta_object ro ON ro.id=rr.role_id "
                 "WHERE ro.name='Роль1' ORDER BY rr.id")
    cat_id = q(db, "SELECT id, uuid FROM meta_object "
                   "WHERE path='Catalog/Справочник1'")[0]
    attr_id = q(db, "SELECT id, uuid FROM meta_attribute "
                    "WHERE name='РеквизитСтрока'")[0]
    assert len(rows) == 3
    r0 = rows[0]
    assert r0[0] == cat_id[1] and r0[1] == READ_UUID and r0[2] == '1'
    assert r0[3] == cat_id[0] and r0[4] is None
    assert 'ПометкаУдаления' in r0[5]        # rls_text права Read
    r1 = rows[1]
    assert r1[1] == VIEW_UUID and r1[2] == '-1'   # View=false → '-1'
    r2 = rows[2]
    assert r2[0] == attr_id[1] and r2[4] == attr_id[0] and r2[2] == '1'
    tmpl = q(db, "SELECT t.name, t.text FROM role_rls_template t "
                 "JOIN meta_object ro ON ro.id=t.role_id WHERE ro.name='Роль1'")
    assert tmpl == [('ДляСправочника(Справочник)', '// шаблон RLS')]
    state = q(db, "SELECT version, parsed, targets, rights, rls_templates "
                  "FROM role_rights_state s JOIN meta_object ro "
                  "ON ro.id=s.role_id WHERE ro.name='Роль1'")
    assert state == [('xml', 1, 2, 3, 1)]


def test_dump_state_and_files(db):
    n = q(db, 'SELECT COUNT(*), COUNT(DISTINCT granule) FROM dump_file')[0]
    assert n[0] >= 15 and n[1] >= 6
    row = q(db, 'SELECT dump_dir FROM dump_state WHERE id=1')[0]
    assert row[0] == os.path.abspath(db[0])
    # bsl-файлы — строка без data (тело в module), Form.xml — с data
    bsl = q(db, "SELECT data FROM file WHERE path LIKE '%ObjectModule.bsl'")
    assert bsl and bsl[0][0] is None
    fxml = q(db, "SELECT length(data) FROM file WHERE path LIKE "
                 "'%Forms/ФормаЭлемента/Ext/Form.xml'")
    assert fxml and fxml[0][0] > 0
    # XML самого объекта в file.data не дублируется (он в header_json)
    own = q(db, "SELECT data FROM file WHERE path="
                "'Catalogs/Справочник1.xml'")
    assert own and own[0][0] is None


# ---------------------------------------------------------------------------
# Инкрементальное обновление
# ---------------------------------------------------------------------------

from confdb.dump import update_db  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    dump = str(tmp_path / 'cfg')
    os.makedirs(dump)
    make_dump_dir(dump)
    path = str(tmp_path / 'd.sqlite')
    build_db(dump, path, workers=1, build_fts=False, log=lambda *a: None)
    return dump, path


def _conn(db_path):
    return sqlite3.connect('file:' + db_path.replace(os.sep, '/') + '?mode=ro',
                           uri=True)


def _update(env, **kw):
    kw.setdefault('rebuild_fts', False)
    return update_db(env[0], env[1], workers=1, log=lambda *a: None, **kw)


def test_update_no_changes(env):
    stats = _update(env)
    assert stats['changed_granules'] == []
    assert stats['objects'] == 0


def test_update_module_body(env):
    dump, db = env
    before = _conn(db)
    dv_before = before.execute('PRAGMA data_version').fetchone()[0]
    id_other = before.execute(
        "SELECT id FROM meta_object WHERE path='Enum/Перечисление1'"
    ).fetchone()[0]
    _w(dump, 'CommonModules/ОбщийМодуль1/Ext/Module.bsl',
       'Функция НоваяФункция(А, Б) Экспорт\n\tВозврат А + Б;\nКонецФункции')
    stats = _update(env)
    assert stats['changed_granules'] == ['CommonModule/ОбщийМодуль1']
    after = _conn(db)
    names = [r[0] for r in after.execute(
        "SELECT t.name FROM method t JOIN module m ON m.id=t.module_id "
        "JOIN meta_object o ON o.id=m.object_id "
        "WHERE o.path='CommonModule/ОбщийМодуль1'")]
    assert names == ['НоваяФункция']
    # посторонние объекты не переустанавливались (id стабилен)
    assert after.execute(
        "SELECT id FROM meta_object WHERE path='Enum/Перечисление1'"
    ).fetchone()[0] == id_other
    # hot reload: ro-соединение видит новый commit без переподключения
    # (PRAGMA data_version меняется), старый метод исчез
    dv_after = before.execute('PRAGMA data_version').fetchone()[0]
    assert dv_after != dv_before
    old = before.execute(
        "SELECT COUNT(*) FROM method t JOIN module m ON m.id=t.module_id "
        "JOIN meta_object o ON o.id=m.object_id "
        "WHERE o.path='CommonModule/ОбщийМодуль1' AND t.name='Экспортная'"
    ).fetchone()[0]
    assert old == 0
    before.close()
    after.close()
    # dump_file обновлён
    row = _conn(db).execute(
        "SELECT sha256 FROM dump_file WHERE path='CommonModules/"
        "ОбщийМодуль1/Ext/Module.bsl'").fetchone()
    import hashlib
    with open(os.path.join(dump, 'CommonModules', 'ОбщийМодуль1', 'Ext',
                           'Module.bsl'), 'rb') as f:
        assert row[0] == hashlib.sha256(f.read()).hexdigest()


def test_update_new_object_and_ref(env):
    dump, db = env
    # новый справочник + ссылка на него из существующего
    _w(dump, 'Catalogs/НовыйСправочник.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Catalog uuid="12345678-0000-0000-0000-000000000001">\n'
       '\t\t<Properties><Name>НовыйСправочник</Name></Properties>\n'
       '\t\t<ChildObjects/>\n'
       '\t</Catalog>\n'
       '</MetaDataObject>' % NS)
    _w(dump, 'Catalogs/НовыйСправочник/Ext/ObjectModule.bsl',
       'Процедура П() КонецПроцедуры')
    cat = open(os.path.join(dump, 'Catalogs', 'Справочник1.xml'),
               encoding='utf-8').read()
    cat = cat.replace('cfg:CatalogRef.Справочник1', 'cfg:CatalogRef.НовыйСправочник')
    _w(dump, 'Catalogs/Справочник1.xml', cat)
    cfg = open(os.path.join(dump, 'Configuration.xml'), encoding='utf-8').read()
    cfg = cfg.replace('<Catalog>Справочник1</Catalog>',
                      '<Catalog>Справочник1</Catalog>\n'
                      '\t\t\t<Catalog>НовыйСправочник</Catalog>')
    _w(dump, 'Configuration.xml', cfg)
    stats = _update(env)
    assert set(stats['changed_granules']) == {
        'Catalog/НовыйСправочник', 'Catalog/Справочник1', ''}
    conn = _conn(db)
    assert conn.execute("SELECT COUNT(*) FROM meta_object "
                        "WHERE path='Catalog/НовыйСправочник'").fetchone()[0] == 1
    assert conn.execute("SELECT ord FROM meta_object "
                        "WHERE path='Catalog/НовыйСправочник'"
                        ).fetchone()[0] == 1
    # ссылка переразрешена на новый объект
    row = conn.execute(
        "SELECT r.object_id, r.uuid, r.target_path FROM attribute_ref r "
        "JOIN meta_attribute a ON a.id=r.attribute_id "
        "JOIN meta_object o ON o.id=a.object_id "
        "WHERE o.path='Catalog/Справочник1' AND a.name='РодительСсылка'"
    ).fetchone()
    new_id = conn.execute("SELECT id FROM meta_object "
                          "WHERE path='Catalog/НовыйСправочник'").fetchone()[0]
    assert row[0] == new_id
    assert row[1] == '12345678-0000-0000-0000-000000000001'
    assert row[2] == 'Catalog/НовыйСправочник'
    conn.close()


def test_update_delete_object_nulls_refs(env):
    dump, db = env
    _w(dump, 'Catalogs/НовыйСправочник.xml',
       '<?xml version="1.0" encoding="UTF-8"?>\n'
       '<MetaDataObject %s version="2.20">\n'
       '\t<Catalog uuid="12345678-0000-0000-0000-000000000001">\n'
       '\t\t<Properties><Name>НовыйСправочник</Name></Properties>\n'
       '\t\t<ChildObjects/>\n'
       '\t</Catalog>\n'
       '</MetaDataObject>' % NS)
    cat = open(os.path.join(dump, 'Catalogs', 'Справочник1.xml'),
               encoding='utf-8').read()
    cat = cat.replace('cfg:CatalogRef.Справочник1',
                      'cfg:CatalogRef.НовыйСправочник')
    _w(dump, 'Catalogs/Справочник1.xml', cat)
    _update(env)
    # удаляем новый справочник и возвращаем ссылку обратно
    os.remove(os.path.join(dump, 'Catalogs', 'НовыйСправочник.xml'))
    _w(dump, 'Catalogs/Справочник1.xml', cat.replace(
        'cfg:CatalogRef.НовыйСправочник', 'cfg:CatalogRef.Справочник1'))
    stats = _update(env)
    assert 'Catalog/НовыйСправочник' in stats['changed_granules']
    conn = _conn(db)
    assert conn.execute("SELECT COUNT(*) FROM meta_object WHERE path="
                        "'Catalog/НовыйСправочник'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dump_file WHERE granule="
                        "'Catalog/НовыйСправочник'").fetchone()[0] == 0
    conn.close()


def test_update_role_rights(env):
    dump, db = env
    xml = open(os.path.join(dump, 'Roles', 'Роль1', 'Ext', 'Rights.xml'),
               encoding='utf-8').read()
    xml = xml.replace('<name>Read</name>\n\t\t\t<value>true</value>',
                      '<name>Read</name>\n\t\t\t<value>false</value>')
    _w(dump, 'Roles/Роль1/Ext/Rights.xml', xml)
    stats = _update(env)
    assert stats['changed_granules'] == ['Role/Роль1']
    conn = _conn(db)
    rows = conn.execute(
        "SELECT right_uuid, value FROM role_right rr "
        "JOIN meta_object ro ON ro.id=rr.role_id "
        "WHERE ro.name='Роль1' AND rr.target_object_id IS NOT NULL "
        "AND rr.right_uuid=?", (READ_UUID,)).fetchall()
    assert rows == [(READ_UUID, '-1')]
    # состояние прав перезаписано, а не задвоено
    assert conn.execute("SELECT COUNT(*) FROM role_rights_state s "
                        "JOIN meta_object ro ON ro.id=s.role_id "
                        "WHERE ro.name='Роль1'").fetchone()[0] == 1
    conn.close()


def test_update_target_change_relinks_other_roles(env):
    """Объект-цель переустановлен: права ДРУГОЙ (не менявшейся) роли
    переразрешаются по target_uuid, а не теряются."""
    dump, db = env
    before = _conn(db).execute(
        "SELECT target_object_id FROM role_right WHERE right_uuid=? "
        "AND target_attr_id IS NULL AND target_object_id IS NOT NULL",
        (READ_UUID,)).fetchone()[0]
    assert before is not None
    # правим модуль справочника — гранула цели становится грязной
    _w(dump, 'Catalogs/Справочник1/Ext/ObjectModule.bsl',
       'Процедура ПриЗаписи(Отказ)\n\tБ = 2;\nКонецПроцедуры')
    _update(env)
    conn = _conn(db)
    after = conn.execute(
        "SELECT target_object_id FROM role_right WHERE right_uuid=? "
        "AND target_attr_id IS NULL AND target_object_id IS NOT NULL",
        (READ_UUID,)).fetchone()
    assert after is not None           # право восстановлено
    new_id = conn.execute("SELECT id FROM meta_object "
                          "WHERE path='Catalog/Справочник1'").fetchone()[0]
    assert after[0] == new_id
    # атрибуное право тоже переразрешено
    attr = conn.execute(
        "SELECT target_attr_id FROM role_right WHERE target_attr_id "
        "IS NOT NULL").fetchone()
    assert attr is not None
    conn.close()


def test_update_falls_back_to_full_build(env, tmp_path):
    dump, _db = env
    other = str(tmp_path / 'fresh.sqlite')
    stats = update_db(dump, other, workers=1, rebuild_fts=False,
                      log=lambda *a: None)
    # базы не было — полная сборка
    assert stats.get('changed_granules') is None or 'objects' in stats
    conn = _conn(other)
    assert conn.execute("SELECT COUNT(*) FROM meta_object").fetchone()[0] > 5
    conn.close()


def test_manifest_sha_stable_between_build_and_update(env):
    """Отпечаток source.file_sha256 одинаково считается при сборке и при
    обновлении (общий порядок: сортировка по относительным путям)."""
    import hashlib
    dump, db = env
    # правка с возвратом: содержимое то же — гранулы чистые, отпечаток не меняется
    conn = _conn(db)
    sha_before = conn.execute(
        'SELECT file_sha256 FROM source ORDER BY id LIMIT 1').fetchone()[0]
    conn.close()
    m = hashlib.sha256()
    c2 = _conn(db)
    for path, sha in c2.execute(
            'SELECT path, sha256 FROM dump_file ORDER BY path'):
        m.update(path.encode('utf-8'))
        m.update(b'\0')
        m.update(sha.encode('ascii'))
        m.update(b'\n')
    c2.close()
    assert m.hexdigest() == sha_before
    # обновление без изменений не трогает отпечаток
    _update(env)
    conn = _conn(db)
    assert conn.execute('SELECT file_sha256 FROM source ORDER BY id LIMIT 1'
                        ).fetchone()[0] == sha_before
    conn.close()


# ---------------------------------------------------------------------------
# XML-адаптеры header_props и выдача MCP-сервера на базе из выгрузки
# ---------------------------------------------------------------------------

def _header(db, path):
    return db[2].execute('SELECT header_json FROM meta_object WHERE path=?',
                         (path,)).fetchone()[0]


def test_register_fields_and_props_xml(db):
    from confdb import header_props
    hj = _header(db, 'InformationRegister/РегистрСведений1')
    kinds = header_props.register_field_kinds(hj)
    assert kinds == {'Измерение1': 'Измерения', 'Ресурс1': 'Ресурсы',
                     'Реквизит1': 'Реквизиты'}
    props = header_props.register_props('InformationRegister', hj)
    assert 'Периодичность: месяц' in props
    assert 'Режим записи: независимый' in props
    # атрибуты регистра в meta_attribute
    rows = q(db, "SELECT a.name, a.type_str FROM meta_attribute a "
                 "JOIN meta_object o ON o.id=a.object_id "
                 "WHERE o.path='InformationRegister/РегистрСведений1' "
                 "ORDER BY a.ord")
    assert rows == [('Измерение1', 'Ссылка: Catalog/Справочник1'),
                    ('Ресурс1', 'Число'), ('Реквизит1', 'Булево')]


def test_config_props_xml(db):
    from confdb import header_props
    props = header_props.config_props(_header(db, ''))
    assert props['name'] == 'ТестКонф'
    assert props['synonym'] == 'Тестовая конфигурация'
    assert props['compatibility'] == '8.3.21'
    assert props['obj_version'] == '80321'


def test_event_subscription_xml(db):
    from confdb import header_props
    props = header_props.event_subscription(
        'EventSubscription', _header(db, 'EventSubscription/Подписка1'))
    assert props['event'] == 'ПередЗаписью'
    assert props['handler_module'] == 'ОбщийМодуль1'
    assert props['handler_method'] == 'Экспортная'
    assert props['source_paths'] == ['Catalog/Справочник1']


def test_exchange_plan_content_xml(db):
    from confdb import header_props
    items = header_props.exchange_plan_content(
        'ExchangePlan', _header(db, 'ExchangePlan/ПланОбмена1'))
    assert items == ['Catalog.Справочник1', 'CommonModule.ОбщийМодуль1']
    assert not header_props.is_uuid(items[0])


def test_mcp_cards_on_dump_db(db):
    """MCP-инструменты на базе из выгрузки: паспорт регистра/подписки/плана."""
    from confdb.mcp_server import McpServer
    server = McpServer(db[1])
    try:
        def call(tool, **args):
            resp = server.handle({'jsonrpc': '2.0', 'id': 1,
                                  'method': 'tools/call',
                                  'params': {'name': tool,
                                             'arguments': args}})
            return resp['result']['content'][0]['text']
        card = call('object_card', path='InformationRegister/РегистрСведений1')
        assert 'Периодичность: месяц' in card
        assert 'Измерения: Измерение1' in card
        assert 'Ресурсы: Ресурс1' in card
        sub = call('object_card', path='EventSubscription/Подписка1')
        assert 'Событие: ПередЗаписью' in sub
        assert 'ОбщийМодуль1.Экспортная' in sub
        assert 'Справочник.Справочник1' in sub
        plan = call('object_card', path='ExchangePlan/ПланОбмена1')
        assert 'Состав плана обмена (2)' in plan
        info = call('configuration_info')
        assert 'Тестовая конфигурация' in info
        assert '8.3.21' in info
    finally:
        server.close_db()
