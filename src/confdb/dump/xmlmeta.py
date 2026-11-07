"""Разбор XML метаданных выгрузки конфигурации в структуры для записи в БД.

Формат — иерархическая выгрузка конфигуратора (MetaDataObject, версия 2.20):
корневой узел класса (`<Catalog uuid="…">`), `<Properties>` со скалярными
свойствами, `<ChildObjects>` с реквизитами/табличными частями/подобъектами.
Пространства имён: значимы только `v8:` (http://v8.1c.ru/8.1/data/core —
типы и локализация) и `xr:` (readable — TypeId/Item); остальные префиксы
отбрасываются вместе с URI (локальные имена уникальны в пределах документа).

Строки типов (`type_str`) и ссылки (`attribute_ref`) строятся в тех же формах,
что и при извлечении из .cf (db.writer._TypeResolver): «Строка(10)», «Число»,
«Ссылка: Catalog/Валюты», состав «A | B», «ОпределяемыйТип: DefinedType/Х (…)».
Отличие в лучшую сторону: платформенные типы, которые бинарный дескриптор .cf
не расшифровывает (type_str NULL), здесь получают имена — «ХранилищеЗначения»,
«УникальныйИдентификатор» и т.п. (278 атрибутов из 49787 в эталонной БП).
"""
import os
import xml.etree.ElementTree as ET

V8_URI = 'http://v8.1c.ru/8.1/data/core'
XR_URI = 'http://v8.1c.ru/8.3/xcf/readable'

# Префикс ссылочного типа в XML → тип объекта в базе (путь '<Тип>/<Имя>').
# ChartOfCharacteristicTypesRef во множественном числе, а тип объекта —
# ChartOfCharacteristicType (единственное), как в QUERY_PREFIXES/BSL_MANAGERS.
REF_CLASS = {
    'CatalogRef': 'Catalog',
    'DocumentRef': 'Document',
    'EnumRef': 'Enum',
    'ChartOfAccountsRef': 'ChartOfAccounts',
    'ChartOfCharacteristicTypesRef': 'ChartOfCharacteristicType',
    'ChartOfCalculationTypesRef': 'ChartOfCalculationTypes',
    'ExchangePlanRef': 'ExchangePlan',
    'BusinessProcessRef': 'BusinessProcess',
    'TaskRef': 'Task',
    'InformationRegisterRecordKey': None,
}

# Платформенные типы v8: — человекочитаемые имена (в .cf-базах type_str NULL)
V8_TYPES = {
    'ValueStorage': 'ХранилищеЗначения',
    'UUID': 'УникальныйИдентификатор',
    'ValueTable': 'ТаблицаЗначений',
    'ValueTree': 'ДеревоЗначений',
    'ValueListType': 'СписокЗначений',
    'FixedArray': 'ФиксированныйМассив',
    'FixedStructure': 'ФиксированнаяСтруктура',
    'FixedMap': 'ФиксированноеСоответствие',
    'StandardPeriod': 'СтандартныйПериод',
    'Type': 'Тип',
    'NumberQualifiers': None,
    'StringQualifiers': None,
    'DateQualifiers': None,
}

# Теги ChildObjects, которые являются полями объекта (meta_attribute):
# реквизиты, измерения/ресурсы регистров, признаки субконто плана счетов,
# колонки журнала документов, атрибуты адресации задачи.
FIELD_TAGS = frozenset((
    'Attribute', 'Dimension', 'Resource', 'AccountingFlag', 'Column',
    'AddressingAttribute',
))

# Подобъекты с собственным uuid, на которые могут указывать права ролей
# (кроме форм/команд — они становятся объектами meta_object сами).
SUBOBJECT_TAGS = {
    'Operation': 'webservice_operation',      # WebService
    'Method': 'httpservice_method',           # HTTPService
    'URLTemplate': 'httpservice_url_template',
    'Parameter': 'webservice_parameter',
    'IntegrationServiceChannel': 'integration_channel',
    'RoutePoint': 'businessprocess_routepoint',
}

REUSE_VALUES = ('DuringRequest', 'DuringSession')


def _clean_tag(tag):
    """Локальное имя тега; v8:/xr: сохраняются, прочие префиксы снимаются."""
    if not tag.startswith('{'):
        return tag
    uri, local = tag[1:].split('}', 1)
    if uri == V8_URI:
        return 'v8:' + local
    if uri == XR_URI:
        return 'xr:' + local
    return local


def load(xml_path):
    """Корневой элемент XML выгрузки с нормализованными именами тегов."""
    root = ET.parse(xml_path).getroot()
    return _normalize(root)


def loads(text):
    """То же из строки (header_json баз из выгрузки)."""
    return _normalize(ET.fromstring(text))


def _normalize(root):
    for el in root.iter():
        el.tag = _clean_tag(el.tag)
    return root


def class_element(root):
    """Узел класса (`<Catalog uuid=…>`) — первый сын MetaDataObject.

    Для файлов без MetaDataObject (Rights.xml, Form.xml, Predefined.xml и т.п.)
    возвращается сам корень.
    """
    if root.tag == 'MetaDataObject':
        for child in root:
            return child
    return root


def _find(el, name):
    for child in el:
        if child.tag == name:
            return child
    return None


def _text(el, name):
    child = _find(el, name) if el is not None else None
    return child.text if child is not None and child.text else None


def properties(obj_el):
    """Узел <Properties> объекта."""
    return _find(obj_el, 'Properties')


def child_objects(obj_el):
    """Узел <ChildObjects> объекта (None, если его нет)."""
    return _find(obj_el, 'ChildObjects')


def object_basics(obj_el):
    """(uuid, name, comment) объекта по узлу класса."""
    props = properties(obj_el)
    return (obj_el.get('uuid'),
            _text(props, 'Name'),
            _text(props, 'Comment'))


def synonym(obj_el, lang='ru'):
    """Синоним объекта на языке lang (первый, если языка нет)."""
    props = properties(obj_el)
    syn = _find(props, 'Synonym') if props is not None else None
    if syn is None:
        return None
    fallback = None
    for item in syn:
        if item.tag != 'v8:item':
            continue
        item_lang = _text(item, 'v8:lang')
        content = _text(item, 'v8:content')
        if item_lang == lang:
            return content
        if fallback is None:
            fallback = content
    return fallback


class Types:
    """Строки типов реквизитов и цели ссылок по XML выгрузки.

    :param dump_dir: корень выгрузки — для раскрытия определяемых типов
        (DefinedTypes\\<Имя>.xml читается по требованию и кэшируется);
    :param known_paths: множество путей объектов в стиле БД ('Catalog/Валюты')
        по сканеру — отличить существующую цель ссылки от удалённой.
        Ссылки возвращаются путями (self.links); uuid и id разрешает запись
        БД одним UPDATE по target_path после вставки всех объектов.
    """

    def __init__(self, dump_dir, known_paths=frozenset()):
        self.dump_dir = dump_dir
        self.known_paths = known_paths
        self._dt_cache = {}
        self.links = []

    # -- публичное ---------------------------------------------------------

    def describe(self, type_el):
        """(type_str, links) по узлу <Type>; links — [path|None] целей ссылок.

        None в links — обобщённая ссылка («Ссылка» без цели): строка
        attribute_ref для неё не создаётся (как и в cf-базе, где uuid цели
        не разрешается ни во что).
        """
        self.links = []
        if type_el is None:
            return None, []
        parts = []
        for value, quals in self._type_entries(type_el):
            part = self._describe_one(value, quals)
            if part and part not in parts:
                parts.append(part)
        text = ' | '.join(parts) if parts else None
        # дедуп ссылок с сохранением порядка
        seen = set()
        dedup = []
        for link in self.links:
            if link not in seen:
                seen.add(link)
                dedup.append(link)
        self.links = dedup
        return text, self.links

    # -- внутреннее ----------------------------------------------------------

    @staticmethod
    def _type_entries(type_el):
        """Пары (значение типа, узел квалификатора) по содержимому <Type>.

        Квалификатор идёт сразу за своим v8:Type/v8:TypeSet; у составных типов
        пар несколько.
        """
        entries = []
        pending = None
        for child in type_el:
            if child.tag in ('v8:Type', 'v8:TypeSet'):
                if pending is not None:
                    entries.append((pending, None))
                pending = (child.text or '').strip()
            elif child.tag in ('v8:StringQualifiers', 'v8:NumberQualifiers',
                               'v8:DateQualifiers'):
                if pending is not None:
                    entries.append((pending, child))
                    pending = None
            # прочее (AllowedLength внутри квалификаторов и т.п.) пропускаем
        if pending is not None:
            entries.append((pending, None))
        return entries

    def _describe_one(self, value, quals):
        if not value:
            return None
        if value.startswith('xs:'):
            return self._primitive(value[3:], quals)
        if value.startswith('v8:'):
            name = V8_TYPES.get(value[3:])
            return name
        if value.startswith('cfg:'):
            return self._cfg_ref(value[4:])
        return None

    @staticmethod
    def _primitive(name, quals):
        if name == 'string':
            length = None
            if quals is not None:
                length = _text(quals, 'v8:Length')
            if length and length.strip('0'):
                return 'Строка(%s)' % length.strip()
            return 'Строка'
        if name == 'decimal':
            return 'Число'
        if name == 'boolean':
            return 'Булево'
        if name == 'dateTime':
            return 'Дата'
        return None

    def _cfg_ref(self, value):
        """cfg:CatalogRef.Имя / cfg:DefinedType.Имя / cfg:AnyRef и т.п."""
        if '.' in value:
            family, name = value.split('.', 1)
        else:
            family, name = value, None
        if family == 'AnyRef' or name is None:
            # обобщённая ссылка без конкретной цели
            self.links.append(None)
            return 'Ссылка'
        if family == 'DefinedType':
            return self._defined_type(name)
        cls = REF_CLASS.get(family, 'unknown')
        if cls:
            path = '%s/%s' % (cls, name)
            if path in self.known_paths:
                self.links.append(path)
                return 'Ссылка: %s' % path
        # цель не найдена в конфигурации (удалённый объект, точка маршрута и т.п.)
        self.links.append(None)
        return 'Ссылка: %s (объект не найден в базе)' % name

    def _defined_type(self, name):
        path = 'DefinedType/%s' % name
        if path in self.known_paths:
            self.links.append(path)
        members = self.dt_members(name, set())
        if members:
            return 'ОпределяемыйТип: %s (%s)' % (path, ' | '.join(members))
        return 'ОпределяемыйТип: %s' % path

    def dt_members(self, name, seen):
        """Члены определяемого типа (список строк) с раскрытием вложенных."""
        if name in seen:
            return []
        seen.add(name)
        type_el = self._dt_type_el(name)
        if type_el is None:
            return []
        members = []
        for value, quals in self._type_entries(type_el):
            if value.startswith('cfg:DefinedType.'):
                inner = 'ОпределяемыйТип: DefinedType/%s' % value.split('.', 1)[1]
                if inner not in members:
                    members.append(inner)
                continue
            part = self._describe_one(value, quals)
            if part and part not in members:
                members.append(part)
        return members

    def _dt_type_el(self, name):
        if name in self._dt_cache:
            return self._dt_cache[name]
        el = None
        path = os.path.join(self.dump_dir, 'DefinedTypes', name + '.xml')
        if os.path.isfile(path):
            try:
                root = class_element(load(path))
                el = _find(properties(root), 'Type')
            except (ET.ParseError, OSError):
                el = None
        self._dt_cache[name] = el
        return el


def fields_and_sections(obj_el, types):
    """Поля и табличные части объекта.

    :return: (attrs, tabulars, commands, enum_values, subobjects)
        attrs — [(name, type_str, links, tabular, uuid)] в порядке объявления;
        tabulars — [(name, uuid)];
        commands — [(name, uuid)] встроенные команды (метаданные команд лежат
            в XML родителя, папка хранит только модуль);
        enum_values — [name] значения перечисления;
        subobjects — [(kind, uuid, name)] подобъекты для прав ролей.
    """
    attrs = []
    tabulars = []
    commands = []
    enum_values = []
    subobjects = []
    children = child_objects(obj_el)
    if children is None:
        return attrs, tabulars, commands, enum_values, subobjects
    seen = set()
    for child in children:
        tag = child.tag
        uuid = child.get('uuid')
        if tag in FIELD_TAGS:
            name, type_str, links, a_uuid = _field(child, types)
            if name and (None, name) not in seen:
                seen.add((None, name))
                attrs.append((name, type_str, links, None, a_uuid or uuid))
        elif tag == 'TabularSection':
            props = properties(child)
            ts_name = _text(props, 'Name')
            if ts_name:
                tabulars.append((ts_name, uuid))
            for f in _section_fields(child):
                name, type_str, links, a_uuid = _field(f, types)
                if name and (ts_name, name) not in seen:
                    seen.add((ts_name, name))
                    attrs.append((name, type_str, links, ts_name,
                                  a_uuid or f.get('uuid')))
        elif tag == 'Command':
            name = _text(properties(child), 'Name')
            if name:
                commands.append((name, uuid))
        elif tag == 'EnumValue':
            name = _text(properties(child), 'Name')
            if name:
                enum_values.append(name)
        elif tag in SUBOBJECT_TAGS:
            name = _text(properties(child), 'Name')
            if uuid and name:
                subobjects.append((SUBOBJECT_TAGS[tag], uuid, name))
                # методы HTTP-сервиса вложены в шаблоны URL: права адресуют их
                # «HTTPService.Х.URLTemplate.Ш.Method.М» — храним подобъект с
                # объединённым именем «Ш.М»
                if tag == 'URLTemplate':
                    nested = child_objects(child)
                    for sub in (nested if nested is not None else ()):
                        if sub.tag != 'Method':
                            continue
                        sub_uuid = sub.get('uuid')
                        sub_name = _text(properties(sub), 'Name')
                        if sub_uuid and sub_name:
                            subobjects.append(
                                ('httpservice_method', sub_uuid,
                                 f'{name}.{sub_name}'))
        # Form/Template как безымянные списки и прочие узлы — не поля
    return attrs, tabulars, commands, enum_values, subobjects


def _section_fields(ts_el):
    """Узлы полей табличной части (её ChildObjects/Attribute…)."""
    children = child_objects(ts_el)
    if children is None:
        return []
    return [c for c in children if c.tag in FIELD_TAGS]


def _field(el, types):
    """(name, type_str, links, uuid) одного поля из его XML-узла."""
    props = properties(el)
    name = _text(props, 'Name')
    type_str, links = types.describe(_find(props, 'Type'))
    return name, type_str, links, el.get('uuid')


def child_name_lists(obj_el):
    """Порядок подобъектов по безымянным спискам ChildObjects.

    :return: {'Form': [имена], 'Template': [имена], 'Subsystem': [имена],
        'Command': [имена]} — для ord форм/макетов/команд/вложенных подсистем.
    """
    out = {}
    children = child_objects(obj_el)
    if children is None:
        return out
    for child in children:
        if child.tag in ('Form', 'Template', 'Subsystem', 'Command') \
                and child.text and not len(child):
            out.setdefault(child.tag, []).append(child.text.strip())
    return out


def common_module_context(obj_el):
    """Контекст выполнения общего модуля — строка как в cf-базе.

    Порядок частей повторяет db.writer._common_module_context: Сервер,
    Внешнее соединение, Клиент (обычное приложение), Клиент (управляемое
    приложение), Вызов сервера, Глобальный, Повторное использование.
    """
    props = properties(obj_el)
    if props is None:
        return None
    names = []
    if _text(props, 'Server') == 'true':
        names.append('Сервер')
    if _text(props, 'ExternalConnection') == 'true':
        names.append('Внешнее соединение')
    if _text(props, 'ClientOrdinaryApplication') == 'true':
        names.append('Клиент (обычное приложение)')
    if _text(props, 'ClientManagedApplication') == 'true':
        names.append('Клиент (управляемое приложение)')
    if _text(props, 'ServerCall') == 'true':
        names.append('Вызов сервера')
    if _text(props, 'Global') == 'true':
        names.append('Глобальный')
    if (_text(props, 'ReturnValuesReuse') or '') in REUSE_VALUES:
        names.append('Повторное использование')
    return ','.join(names) or None


def subsystem_content(obj_el):
    """Состав подсистемы: строки «Класс.Имя» из <Content>."""
    props = properties(obj_el)
    content = _find(obj_el, 'Content')
    if content is None and props is not None:
        content = _find(props, 'Content')
    if content is None:
        return []
    out = []
    for item in content:
        # <xr:Item xsi:type="xr:MDObjectRef">Catalog.Валюты</xr:Item>
        if item.text and item.text.strip():
            out.append(item.text.strip())
    return out


def common_attribute_content(obj_el):
    """Состав общего реквизита: строки «Класс.Имя».

    В отличие от подсистем, у общего реквизита ссылка лежит не текстом в
    <xr:Item>, а во вложенном <xr:Metadata> (плюс <xr:Use> и
    <xr:ConditionalSeparation>).
    """
    content = _find(obj_el, 'Content') or _find(properties(obj_el), 'Content')
    if content is None:
        return []
    out = []
    for item in content:
        if item.tag != 'xr:Item':
            continue
        md = _find(item, 'xr:Metadata')
        text = (md.text if md is not None else item.text) or ''
        if text.strip():
            out.append(text.strip())
    return out


ZERO_UUID = '00000000-0000-0000-0000-000000000000'

# Имя корневого узла предопределённых данных — как в cf-базе (зависит от класса)
PREDEF_ROOT_NAMES = {
    'Catalog': 'Элементы',
    'ChartOfAccounts': 'Счета',
    'ChartOfCharacteristicType': 'Характеристики',
    'ChartOfCalculationTypes': 'ВидыРасчета',
}


def predefined_items(xml_path, root_name='Элементы'):
    """Элементы Ext\\Predefined.xml обходом сверху вниз (как в cf-базе).

    :return: (elements, subcontos)
        elements — [(ord, parent_ord, uuid, name, code, display)]; корневой
            узел (ord=0, parent_ord=None, uuid нулевой) повторяет cf-базу;
        subcontos — [(ord_элемента, ord_вида, имя_ПВХ_элемента, flags)]:
            виды субконто счёта из <ExtDimensionTypes> (имя — строка
            «ChartOfCharacteristicTypes.ПВХ.Элемент», flags — список имён
            включённых признаков учёта).
    """
    if not xml_path or not os.path.isfile(xml_path):
        return [], []
    try:
        root = load(xml_path)
    except (ET.ParseError, OSError):
        return [], []
    rows = [(0, None, ZERO_UUID, root_name, '', '')]
    subcontos = []
    counter = [1]

    def walk(parent_el, parent_ord):
        for item in parent_el:
            if item.tag != 'Item':
                continue
            ord_no = counter[0]
            counter[0] += 1
            rows.append((ord_no, parent_ord, item.get('id'),
                         _text(item, 'Name') or '', _text(item, 'Code') or '',
                         _text(item, 'Description') or ''))
            ext_dims = _find(item, 'ExtDimensionTypes')
            if ext_dims is not None:
                for sub_ord, ext in enumerate(ext_dims):
                    if ext.tag != 'ExtDimensionType':
                        continue
                    ref = ext.get('name') or ''
                    flags = []
                    fl = _find(ext, 'AccountingFlags')
                    if fl is not None:
                        for flag in fl:
                            if flag.tag == 'Flag' and (flag.text or '').strip() == 'true':
                                # ref="ChartOfAccounts.Хозрасчетный.ExtDimensionAccountingFlag.Суммовой"
                                flags.append(flag.get('ref', '').rsplit('.', 1)[-1])
                    if ref:
                        subcontos.append((ord_no, sub_ord, ref, flags))
            children = _find(item, 'ChildItems')
            if children is not None:
                walk(children, ord_no)

    walk(root, 0)
    if len(rows) == 1:
        return [], []
    return rows, subcontos


def exchange_plan_content(xml_path):
    """Состав плана обмена из Ext\\Content.xml: [(«Класс.Имя», auto_record)]."""
    if not xml_path or not os.path.isfile(xml_path):
        return []
    try:
        root = load(xml_path)
    except (ET.ParseError, OSError):
        return []
    out = []
    for item in root.iter('Item'):
        name = _text(item, 'Metadata')
        if name:
            out.append((name.strip(), _text(item, 'AutoRecord')))
    return out


def event_subscription_info(obj_el):
    """Подписка на событие: {'source': […], 'event': […], 'handler': str}."""
    props = properties(obj_el)
    if props is None:
        return {}
    out = {}
    src = _find(props, 'Source')
    if src is not None:
        out['source'] = [x.text.strip() for x in src
                         if x.text and x.text.strip()]
    ev = _find(props, 'Event')
    if ev is not None:
        out['event'] = [x.text.strip() for x in ev
                        if x.text and x.text.strip()]
    handler = _text(props, 'Handler')
    if handler:
        out['handler'] = handler
    return out


def config_children(obj_el):
    """Состав конфигурации: [(tag, name)] по <ChildObjects> Configuration.xml."""
    out = []
    children = child_objects(obj_el)
    if children is None:
        return out
    for child in children:
        if child.text and child.text.strip():
            out.append((child.tag, child.text.strip()))
    return out


def config_props_dict(obj_el):
    """Свойства конфигурации для configuration_info: имя, синоним, версия,
    режим совместимости и т.п."""
    props = properties(obj_el)
    if props is None:
        return {}
    out = {}
    for tag in ('Name', 'Synonym', 'Comment', 'Version', 'CompatibilityMode',
                'DefaultLanguage', 'ScriptVariant', 'ModalityUseMode',
                'MobileApplicationClient', 'Vendor', 'VendorInfo', 'Copyright',
                'ConfigurationInformation', 'BriefInformation'):
        if tag == 'Synonym':
            value = synonym(obj_el)
        else:
            value = _text(props, tag)
        if value:
            out[tag] = value
    return out


def form_kind(obj_el):
    """Вид формы (ManagedForm/OrdinaryForm) из свойств XML формы."""
    return _text(properties(obj_el), 'FormType')


def template_type(obj_el):
    """Тип макета (DataCompositionSchema/SpreadsheetDocument/…)."""
    return _text(properties(obj_el), 'TemplateType')


def skd_queries_from_xml(xml_path):
    """Тексты запросов СКД из Template.xml (теги <query>).

    Использует тот же приём, что db.writer._extract_skd_queries для .cf:
    регулярный поиск по тексту — XML схемы компоновки может быть большим,
    а запросов в нём несколько.
    """
    import re
    try:
        with open(xml_path, 'rb') as f:
            raw = f.read()
    except OSError:
        return []
    if b'<query' not in raw:
        return []
    text = raw.decode('utf-8', errors='replace')
    queries = [xml_unescape(m).strip() for m in
               re.findall(r'<query\b[^>]*>(.*?)</query>', text, re.S)]
    return [q for q in queries if q]


def xml_unescape(text):
    import xml.sax.saxutils
    return xml.sax.saxutils.unescape(text)
