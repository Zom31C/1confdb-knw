"""Свойства объектов метаданных, извлекаемые из header_json готовой базы.

Заголовок объекта хранится в meta_object.header_json как есть (порт v8unpack),
поэтому часть сведений доступна без переизвлечения конфигурации: версия и режим
совместимости конфигурации, префикс имён расширения, состав измерений/ресурсов/
реквизитов регистра, его периодичность и режим записи, целевое пространство
имён пакета XDTO.

Соответствие позиций и канонических uuid проверено на реальной базе УНФ
(949 регистров сведений, 123 регистра накопления, 334 пакета XDTO) и на паре
configuration/extension из тестов v8unpack; подробности — в базе знаний
проекта, страницы `register-header-structure` и `configuration-header-props`.

Тип загруженного файла определяется расширением исходника, и оно точнее типа
корневого объекта: .erf и .epf декодирует один класс ExternalDataProcessor
(v8/decoder.py), поэтому корень внешнего отчёта попадает в базу как
ExternalDataProcessor.
"""
import json
import os

# Канонические uuid коллекций реквизитов регистра: узел коллекции в header[0]
# начинается таким uuid, позиция узла у разных типов регистра разная.
# Назначение подтверждено составом типов полей: ресурсы числовые, измерения
# ссылочные (РегистрСведений.ЦеныНоменклатуры, РегистрСведений.ОстаткиТоваров).
# Регистр сведений
IR_RESOURCES = '13134202-f60b-11d5-a3c7-0050bae0a776'
IR_DIMENSIONS = '13134203-f60b-11d5-a3c7-0050bae0a776'
IR_ATTRIBUTES = 'a2207540-1400-11d6-a3c7-0050bae0a776'
IR_FORMS = '13134204-f60b-11d5-a3c7-0050bae0a776'
# Регистр накопления
AR_RESOURCES = 'b64d9a41-1642-11d6-a3c7-0050bae0a776'
AR_ATTRIBUTES = 'b64d9a42-1642-11d6-a3c7-0050bae0a776'
AR_DIMENSIONS = 'b64d9a43-1642-11d6-a3c7-0050bae0a776'
AR_FORMS = 'b64d9a44-1642-11d6-a3c7-0050bae0a776'

REGISTER_COLLECTIONS = {
    IR_RESOURCES: 'Ресурсы',
    IR_DIMENSIONS: 'Измерения',
    IR_ATTRIBUTES: 'Реквизиты',
    AR_RESOURCES: 'Ресурсы',
    AR_ATTRIBUTES: 'Реквизиты',
    AR_DIMENSIONS: 'Измерения',
}

# Порядок групп в паспорте объекта — как в конфигураторе
REGISTER_KINDS = ('Измерения', 'Ресурсы', 'Реквизиты')

# Коды периодичности регистра сведений. Полный набор значений перечисления
# ПериодичностьРегистраСведений — из официальной справки платформы 8.3.26.1498
# (shcntx_ru.hbk; база syntax.db проекта 1c-syntax-db-extractor, тема
# InformationRegisterPeriodicity: внутренние номера значений 1582..1588,
# смещение 1582 и есть код в заголовке). Смещение подтверждено пятью кодами,
# расшифрованными раньше по семантике регистров УНФ (КоэффициентДефлятор=год,
# СведенияОВзносахВПФР=квартал, МинимальнаяОплатаТрудаРФ=месяц, КурсыВалют=день),
# и независимым признаком: все 10 регистров УНФ с кодом 6 подчинены регистратору,
# как того требует описание значения «ПозицияРегистратора».
PERIODICITY = {
    '0': 'непериодический', '1': 'год', '2': 'квартал', '3': 'месяц',
    '4': 'день', '5': 'секунда', '6': 'позиция регистратора',
}

# Регистры, которые платформа всегда подчиняет регистратору (независимого
# режима записи у них нет); периодичности у них тоже нет.
RECORDER_ONLY = frozenset({
    'AccumulationRegister', 'AccountingRegister', 'CalculationRegister',
})

# Позиции скалярных свойств в header[0][1] регистра сведений
IR_PERIODICITY = 18
IR_WRITE_MODE = 19

# Позиции в узле свойств конфигурации header[0][3][1][1]
CFG_VERSION = 15
CFG_NAME_PREFIX = 42

# Пакет XDTO: запись header[0][1] = ['1', CORE, '"целевое пространство имён"'].
# Позиция одинакова у всех 334 пакетов УНФ и значение совпадает с атрибутом
# targetNamespace файла XDTOPackage.bin (проверено сверкой с XML).
XDTO_NAMESPACE = 2

# Тип загруженного файла — по расширению исходника и по типу корневого объекта
FILE_KINDS = {
    '.cf': 'Конфигурация',
    '.cfe': 'Расширение конфигурации',
    '.epf': 'Внешняя обработка',
    '.erf': 'Внешний отчёт',
}

ROOT_KINDS = {
    'Configuration': 'Конфигурация',
    'ConfigurationExtension': 'Расширение конфигурации',
    'ExternalDataProcessor': 'Внешняя обработка',
    'ExternalReport': 'Внешний отчёт',
}

# Родительный падеж названия типа — для строки «Версия …» паспорта
VERSION_NOUNS = {
    'Конфигурация': 'конфигурации',
    'Расширение конфигурации': 'расширения',
    'Внешняя обработка': 'обработки',
    'Внешний отчёт': 'отчёта',
}

# Режим совместимости — свойство конфигурации: у внешних обработок и отчётов
# он не задаётся, а расширение выполняется в режиме основной конфигурации.
WITHOUT_COMPATIBILITY = frozenset({'ExternalDataProcessor', 'ExternalReport'})


def unquote(value):
    """'"3.0.4.4"' -> '3.0.4.4'; не-строки возвращает как есть."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' \
            and value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def compatibility_str(code):
    """'80321' -> '8.3.21'; то, что не похоже на код, возвращает как есть."""
    text = str(code or '').strip()
    if len(text) >= 4 and text.isdigit() and text[0] in '78':
        return f'{text[0]}.{int(text[1:3])}.{int(text[3:])}'
    return text or None


def source_ext(path):
    """Расширение исходного файла ('.cfe') или None."""
    return os.path.splitext(str(path or ''))[1].lower() or None


def kind_of(root_type=None, root_type_ru=None, source_file=None):
    """(тип загруженного файла, его расширение) — ('Внешний отчёт', '.erf').

    Расширение исходника важнее типа корня: корень .erf записывается в базу
    как ExternalDataProcessor, и только '.erf' говорит, что это отчёт.
    """
    ext = source_ext(source_file)
    name = (FILE_KINDS.get(ext) or ROOT_KINDS.get(root_type)
            or root_type_ru or root_type)
    return name, ext


def version_noun(kind_name):
    """'Расширение конфигурации' -> 'расширения' — для строки «Версия …»."""
    return VERSION_NOUNS.get(kind_name, 'конфигурации')


def compatibility_line(root_type, compatibility):
    """Режим совместимости для паспорта базы, с учётом типа корневого объекта.

    У внешних обработок и отчётов такого свойства нет вообще, а расширение
    выполняется в режиме основной конфигурации — значение из его файла
    показывается как справочное, а не как действующий режим.
    """
    if root_type in WITHOUT_COMPATIBILITY:
        return compatibility or 'не задаётся'
    if root_type == 'ConfigurationExtension':
        text = 'наследуется от основной конфигурации'
        return (f'{text}; в файле расширения указан {compatibility}'
                if compatibility else text)
    return compatibility or 'не определён'


def compatibility_short(root_type, compatibility):
    """То же одной фразой для списка баз; None — выводить нечего."""
    if root_type in WITHOUT_COMPATIBILITY:
        return compatibility
    if root_type == 'ConfigurationExtension':
        return (f'{compatibility} (наследуется)' if compatibility
                else 'наследуется от основной конфигурации')
    return compatibility


def _load(header_json):
    try:
        return json.loads(header_json)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Адаптеры баз из ВЫГРУЗКИ В ФАЙЛЫ: там header_json хранит исходный XML
# объекта (dump.build), а не внутренний JSON декодера .cf. Формат определяется
# по первому непробельному символу; состав сведений тот же, но лежит в
# именованных тегах — парсеры проще и надёжнее позиционных.
# ---------------------------------------------------------------------------

def _xml_header(header_json):
    """Узел класса из XML-заголовка или None (это не XML)."""
    text = header_json.lstrip() if isinstance(header_json, str) else ''
    if not text.startswith('<'):
        return None
    from .dump import xmlmeta
    try:
        return xmlmeta.class_element(xmlmeta.loads(text))
    except Exception:  # noqa: BLE001 — битый XML = заголовок не читается
        return None


def _xml_text(el, name):
    for child in el:
        if child.tag == name:
            return child.text or None
    return None


def _xml_props(root):
    for child in root:
        if child.tag == 'Properties':
            return child
    return None


# Периодичность регистра сведений: XML-имя → строка как в cf-базе
PERIODICITY_XML = {
    'Nonperiodical': 'непериодический', 'Year': 'год', 'Quarter': 'квартал',
    'Month': 'месяц', 'Day': 'день', 'Second': 'секунда',
    'RecorderPosition': 'позиция регистратора',
}

# Режим записи регистра: XML-имя → строка как в cf-базе
WRITE_MODE_XML = {
    'Independent': 'независимый',
    'RecordSubordinate': 'подчинение регистратору',
}

# Коллекции полей регистра: тег ChildObjects → группа паспорта
REGISTER_KIND_TAGS = {
    'Dimension': 'Измерения', 'Resource': 'Ресурсы', 'Attribute': 'Реквизиты',
}

# События подписок: английское имя платформы → русское как в конфигураторе.
# Накопано по header_json четырёх cf-баз (УНФ, БП, БП-обновление, ЗУП: 1723
# подписки, 20 имён, нулевая конфликтность) — cf хранит пару '"En_Ru"'.
EVENTS_RU = {
    'BeforeDelete': 'ПередУдалением',
    'BeforeWrite': 'ПередЗаписью',
    'ChoiceDataGetProcessing': 'ОбработкаПолученияДанныхВыбора',
    'FillCheckProcessing': 'ОбработкаПроверкиЗаполнения',
    'Filling': 'ОбработкаЗаполнения',
    'FormGetProcessing': 'ОбработкаПолученияФормы',
    'OnCopy': 'ПриКопировании',
    'OnReceiveDataFromMaster': 'ПриПолученииДанныхОтГлавного',
    'OnReceiveDataFromSlave': 'ПриПолученииДанныхОтПодчиненного',
    'OnSendDataToMaster': 'ПриОтправкеДанныхГлавному',
    'OnSendDataToSlave': 'ПриОтправкеДанныхПодчиненному',
    'OnSendNodeDataToSlave': 'ПриОтправкеДанныхУзлаПодчиненному',
    'OnSetNewCode': 'ПриУстановкеНовогоКода',
    'OnSetNewNumber': 'ПриУстановкеНовогоНомера',
    'OnWrite': 'ПриЗаписи',
    'Posting': 'ОбработкаПроведения',
    'PresentationFieldsGetProcessing':
        'ОбработкаПолученияПолейПредставления',
    'PresentationGetProcessing': 'ОбработкаПолученияПредставления',
    'UndoPosting': 'ОбработкаУдаленияПроведения',
}

# Источники подписок в выгрузке: тип платформы → тип объекта в базе
SUB_SOURCE_CLASS = {
    'CatalogObject': 'Catalog',
    'DocumentObject': 'Document',
    'ExchangePlanObject': 'ExchangePlan',
    'BusinessProcessObject': 'BusinessProcess',
    'TaskObject': 'Task',
    'ChartOfAccountsObject': 'ChartOfAccounts',
    'ChartOfCharacteristicTypesObject': 'ChartOfCharacteristicType',
    'ChartOfCalculationTypesObject': 'ChartOfCalculationTypes',
    'DataProcessorObject': 'DataProcessor',
    'AccumulationRegisterRecordSet': 'AccumulationRegister',
    'InformationRegisterRecordSet': 'InformationRegister',
    'AccountingRegisterRecordSet': 'AccountingRegister',
    'CalculationRegisterRecordSet': 'CalculationRegister',
}

# Имена классов в строках состава/целей («Класс.Имя») → тип объекта в базе:
# платформа для части классов использует имена, отличающиеся от типа
CLASS_ALIAS_NAMES = {
    'ChartOfCharacteristicTypes': 'ChartOfCharacteristicType',
    'ChartOfCharacteristicType': 'ChartOfCharacteristicType',
    'DocumentNumerator': 'DocumentNumerators',
    'DocumentNumerators': 'DocumentNumerators',
    'Sequence': 'Sequences',
    'Sequences': 'Sequences',
}

# Маркер внедрённого при сборке файла (состав плана обмена живёт в отдельном
# Ext/Content.xml — dump.build дописывает его текст к header_json)
INCLUDE_MARKER = '<!--confdb:include:'


def _is_attr(node):
    """Запись реквизита ['2', CORE, TYPEDESC] -> имя, иначе None.

    CORE у полей разных коллекций регистра начинается разным маркером: '3' у
    реквизитов, '0'/'1'/'2' у измерений и ресурсов. Запись с чужим маркером
    принимается, только когда на месте uuid (CORE[1][2]) — тот же критерий, что
    в writer._attr_record: без uuid отличить поле от похожей структуры нечем.
    """
    if not isinstance(node, list) or len(node) < 3 or str(node[0]) != '2':
        return None
    core = node[1]
    if not (isinstance(core, list) and len(core) >= 3 and isinstance(core[2], str)):
        return None
    if str(core[0]) != '3':
        inner = core[1]
        if not (isinstance(inner, list) and len(inner) >= 3
                and is_uuid(inner[2])):
            return None
    return unquote(core[2])


def _attr_names(node, out=None):
    """Имена всех реквизитов внутри узла (обход в глубину)."""
    if out is None:
        out = []
    if isinstance(node, dict):
        for value in node.values():
            _attr_names(value, out)
    elif isinstance(node, list):
        name = _is_attr(node)
        if name is not None:
            out.append(name)
            return out
        for child in node:
            _attr_names(child, out)
    return out


def register_field_kinds(header_json):
    """{имя реквизита: 'Измерения'|'Ресурсы'|'Реквизиты'} для регистра.

    Пустой словарь — коллекции не распознаны (не регистр, неизвестный тип
    регистра или другой формат заголовка): вызывающий код показывает реквизиты
    одним плоским списком, как раньше.
    """
    root = _xml_header(header_json)
    if root is not None:
        kinds = {}
        for child in root:
            if child.tag != 'ChildObjects':
                continue
            for item in child:
                kind = REGISTER_KIND_TAGS.get(item.tag)
                if kind is None:
                    continue
                name = _xml_text(_xml_props(item), 'Name') \
                    if _xml_props(item) is not None else None
                if name:
                    kinds.setdefault(name, kind)
        return kinds
    data = _load(header_json)
    if data is None:
        return {}
    try:
        top = data['header'][0]
    except (KeyError, IndexError, TypeError):
        return {}
    kinds = {}
    for node in top if isinstance(top, list) else []:
        if not isinstance(node, list) or not node or not isinstance(node[0], str):
            continue
        kind = REGISTER_COLLECTIONS.get(node[0].strip('"').lower())
        if kind is None:
            continue
        for name in _attr_names(node):
            kinds.setdefault(name, kind)
    return kinds


def register_props(obj_type, header_json):
    """Строки свойств регистра: периодичность и режим записи.

    Возвращает только то, что подтверждено: у регистра накопления позиция [18]
    булева, а не код периодичности, поэтому периодичность выводится лишь для
    регистра сведений.
    """
    if obj_type in RECORDER_ONLY:
        return ['Режим записи: подчинение регистратору']
    if obj_type != 'InformationRegister':
        return []
    root = _xml_header(header_json)
    if root is not None:
        props = _xml_props(root)
        if props is None:
            return []
        lines = []
        period = PERIODICITY_XML.get(
            (_xml_text(props, 'InformationRegisterPeriodicity') or '').strip())
        if period:
            lines.append(f'Периодичность: {period}')
        mode = WRITE_MODE_XML.get(
            (_xml_text(props, 'WriteMode') or '').strip())
        if mode:
            lines.append(f'Режим записи: {mode}')
        return lines
    data = _load(header_json)
    if data is None:
        return []
    try:
        inner = data['header'][0][1]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(inner, list):
        return []
    lines = []
    code = _scalar(inner, IR_PERIODICITY)
    if code is not None:
        period = PERIODICITY.get(code)
        if period is None:
            # код встречается в реальных конфигурациях, но не расшифрован:
            # важно хотя бы то, что регистр периодический
            period = f'есть, код {code} (не расшифрован)' if code != '0' else 'нет'
        lines.append(f'Периодичность: {period}')
    mode = _scalar(inner, IR_WRITE_MODE)
    if mode == '1':
        lines.append('Режим записи: подчинение регистратору')
    elif mode == '0':
        lines.append('Режим записи: независимый')
    return lines


def xdto_props(obj_type, header_json):
    """Строки свойств пакета XDTO из заголовка: целевое пространство имён.

    Состав типов и свойств пакета в заголовке не хранится — он лежит в файле
    XDTOPackage.bin (открытый XML), и его извлекает write_db.
    """
    if obj_type != 'XDTOPackage':
        return []
    root = _xml_header(header_json)
    if root is not None:
        namespace = _xml_text(_xml_props(root), 'Namespace') \
            if _xml_props(root) is not None else None
        return [f'Пространство имён: {namespace}'] if namespace else []
    data = _load(header_json)
    if data is None:
        return []
    try:
        inner = data['header'][0][1]
    except (KeyError, IndexError, TypeError):
        return []
    if not isinstance(inner, list):
        return []
    namespace = unquote(_scalar(inner, XDTO_NAMESPACE) or '')
    return [f'Пространство имён: {namespace}'] if namespace else []


def _scalar(inner, index):
    """Скалярное значение позиции заголовка или None (нет/не скаляр).

    None, булево значение и пустая строка — не факт о свойстве: позиция в
    заголовке присутствует, но ничего не говорит. Вернуть их строкой значило бы
    выдумать значение — «Периодичность: есть, код None (не расшифрован)».
    """
    if len(inner) <= index or isinstance(inner[index], (list, dict)):
        return None
    value = inner[index]
    if value is None or isinstance(value, bool):
        return None
    value = str(value)
    return value or None


def config_props(header_json):
    """Сведения о конфигурации/расширении из заголовка корневого объекта.

    Возвращает словарь: name, synonym, version, compatibility, name_prefix,
    obj_version — отсутствующие значения не включаются.
    """
    root = _xml_header(header_json)
    if root is not None:
        return _config_props_xml(root)
    data = _load(header_json)
    if not isinstance(data, dict):
        return {}
    out = {}
    name = data.get('name')
    if name:
        out['name'] = unquote(name)
    synonyms = data.get('name2')
    if isinstance(synonyms, dict):
        synonym = synonyms.get('ru')
        if synonym:
            out['synonym'] = unquote(synonym)
    compat = compatibility_str(data.get('compatibility_version'))
    if compat:
        out['compatibility'] = compat
    if data.get('obj_version'):
        out['obj_version'] = str(data['obj_version'])
    props = _config_props_node(data)
    if props is not None:
        version = unquote(_scalar(props, CFG_VERSION) or '')
        if version:
            out['version'] = version
        prefix = unquote(_scalar(props, CFG_NAME_PREFIX) or '')
        if prefix:
            out['name_prefix'] = prefix
    return out


def _config_props_xml(root):
    """config_props по Configuration.xml выгрузки."""
    from .dump import xmlmeta
    props = _xml_props(root)
    out = {}
    if props is None:
        return out
    name = _xml_text(props, 'Name')
    if name:
        out['name'] = name
    synonym = xmlmeta.synonym(root)
    if synonym:
        out['synonym'] = synonym
    version = _xml_text(props, 'Version')
    if version:
        out['version'] = version
    compat = (_xml_text(props, 'CompatibilityMode') or '').strip()
    if compat:
        # 'Version8_3_27' -> '8.3.27'
        digits = compat.replace('Version', '').replace('_', '.')
        out['compatibility'] = digits
        parts = [p for p in digits.split('.') if p.isdigit()]
        if len(parts) >= 3:
            out['obj_version'] = '%s%02d%02d' % (
                parts[0], int(parts[1]), int(parts[2]))
    prefix = _xml_text(props, 'NamePrefix')
    if prefix:
        out['name_prefix'] = prefix
    return out


def _config_props_node(data):
    """Узел свойств конфигурации header[0][3][1][1] (длинный список скаляров)."""
    try:
        node = data['header'][0][3][1][1]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(node, list) or len(node) <= CFG_NAME_PREFIX:
        return None
    return node


# Подписка на событие: header[0][1] = ['1', CORE, ИСТОЧНИКИ, СОБЫТИЕ,
# UUID_ОБРАБОТЧИКА, ИМЯ_МЕТОДА]. Состав и позиции одинаковы у всех 406 подписок
# УНФ; событие — строка '"BeforeWrite_ПередЗаписью"', обработчик — uuid общего
# модуля и имя процедуры в нём.
ES_SOURCES = 2
ES_EVENT = 3
ES_HANDLER = 4
ES_METHOD = 5
ES_LEN = 6

# «Источниковый» uuid объекта — тот, которым на него ссылаются подписки на
# события. Это НЕ meta_object.uuid (из .id.json) и НЕ ключ таблицы .10: ни там,
# ни там этих uuid нет. У справочника/документа/регистра/перечисления он лежит
# в header[0][1][1], у константы — в header[0][1][4] (позиция [1] занята
# записью типа ['27', …]).
SELF_REF_POSITIONS = (1, 4)


def is_uuid(value):
    """Похоже ли значение на uuid: 36 знаков, четыре дефиса."""
    return (isinstance(value, str) and len(value) == 36
            and value.count('-') == 4)


def self_ref_uuid(header_json):
    """«Источниковый» uuid объекта или None — в заголовке его нет.

    По нему подписка на событие ссылается на свой источник, поэтому карта
    uuid -> путь строится обходом meta_object.header_json готовой базы.
    """
    data = _load(header_json)
    if not isinstance(data, dict):
        return None
    try:
        inner = data['header'][0][1]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(inner, list):
        return None
    for pos in SELF_REF_POSITIONS:
        if len(inner) > pos and is_uuid(inner[pos]):
            return inner[pos]
    return None


def event_name(raw):
    """'"BeforeWrite_ПередЗаписью"' -> 'ПередЗаписью' — как в конфигураторе.

    Платформа хранит строку события двумя частями через '_'; наружу идёт только
    русская: английская — внутреннее имя платформы, в конфигураторе его нет.
    """
    text = unquote(raw or '')
    if not text:
        return None
    _en, sep, ru = text.partition('_')
    return ru if sep and ru else text


def _event_subscription_xml(root):
    """event_subscription по XML выгрузки.

    В выгрузке источник — тип платформы ('cfg:DocumentObject.Х'), обработчик —
    точечное имя ('CommonModule.Модуль.Метод'), событие — английское имя.
    Возвращает те же ключи плюс 'source_paths' (пути объектов в стиле БД
    вместо «источниковых» uuid) и 'handler_module' — вызывающий код
    (_build_event_index) использует их напрямую, без карты self_ref_uuid.
    """
    props = _xml_props(root)
    if props is None:
        return {}
    event_en = None
    handler_module = None
    handler_method = None
    source_paths = []
    unresolved = 0
    for child in props:
        if child.tag == 'Event' and child.text:
            event_en = child.text.strip()
        elif child.tag == 'Handler' and child.text:
            parts = child.text.strip().split('.')
            if len(parts) == 3:
                handler_module, handler_method = parts[1], parts[2]
        elif child.tag == 'Source':
            for item in child:
                if item.tag != 'v8:Type' or not item.text:
                    continue
                value = item.text.strip()
                if not value.startswith('cfg:') or '.' not in value:
                    unresolved += 1
                    continue
                family, name = value[4:].split('.', 1)
                cls = SUB_SOURCE_CLASS.get(family)
                if cls is None:
                    unresolved += 1
                    continue
                source_paths.append(f'{cls}/{name}')
    event = EVENTS_RU.get(event_en or '', event_en)
    if not event:
        return {}
    return {
        'event': event,
        'handler_uuid': None,
        'handler_method': handler_method,
        'sources': [],
        'source_paths': source_paths,
        'unresolved_sources': unresolved,
        'handler_module': handler_module,
    }


def event_subscription(obj_type, header_json):
    """Свойства подписки на событие; {} — не подписка или другой формат.

    Ключи: event, handler_uuid, handler_method, sources (список «источниковых»
    uuid в порядке объявления). Пути объектов по uuid определяет вызывающий
    код: в самом заголовке их нет.
    """
    if obj_type != 'EventSubscription':
        return {}
    root = _xml_header(header_json)
    if root is not None:
        return _event_subscription_xml(root)
    data = _load(header_json)
    if not isinstance(data, dict):
        return {}
    try:
        inner = data['header'][0][1]
    except (KeyError, IndexError, TypeError):
        return {}
    if not isinstance(inner, list) or len(inner) < ES_LEN:
        return {}
    sources = inner[ES_SOURCES]
    if (not isinstance(sources, list) or not sources
            or unquote(sources[0]) != 'Pattern'):
        return {}
    event = event_name(inner[ES_EVENT])
    if not event:
        return {}
    handler = inner[ES_HANDLER]
    method = unquote(inner[ES_METHOD])
    return {
        'event': event,
        'handler_uuid': handler if is_uuid(handler) else None,
        'handler_method': method or None,
        'sources': [item[1] for item in sources[1:]
                    if isinstance(item, list) and len(item) > 1
                    and is_uuid(item[1])],
    }


def _exchange_plan_content_xml(header_json):
    """Состав плана обмена из внедрённого Ext/Content.xml: строки «Класс.Имя»."""
    from .dump import xmlmeta
    tail = header_json.split(INCLUDE_MARKER, 1)[1]
    cut = tail.find('-->')
    body = (tail[cut + 3:] if cut >= 0 else tail).lstrip()
    try:
        root = xmlmeta.loads(body)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in root.iter('Item'):
        name = None
        for child in item:
            if child.tag == 'Metadata' and child.text:
                name = child.text.strip()
                break
        if name:
            out.append(name)
    return out


# План обмена: поток .1 декодер кладёт в data['info'] — плоский список
# ['2', КОЛИЧЕСТВО, uuid, флаг, uuid, флаг, …], у всех 27 планов УНФ он
# обёрнут в один дополнительный список. Это состав плана: uuid разрешаются
# обычным meta_object.uuid (9162 из 9163 записей), счётчик равен числу пар.
# Флаг у 109 записей из 9163 равен 1, смысл его не подтверждён — поэтому
# наружу выдаётся только состав, без флага.
EP_FIRST = 2


def exchange_plan_content(obj_type, header_json):
    """uuid объектов состава плана обмена; [] — не план или состава нет.

    В базах из выгрузки состав плана живёт в отдельном Ext/Content.xml, который
    dump.build дописывает к header_json за маркером INCLUDE_MARKER: тогда
    возвращаются строки «Класс.Имя» (не uuid) — вызывающий код различает их
    через is_uuid.
    """
    if obj_type != 'ExchangePlan':
        return []
    if isinstance(header_json, str) and INCLUDE_MARKER in header_json:
        return _exchange_plan_content_xml(header_json)
    data = _load(header_json)
    if not isinstance(data, dict):
        return []
    info = data.get('info')
    while isinstance(info, list) and len(info) == 1 and isinstance(info[0], list):
        info = info[0]
    if not isinstance(info, list) or len(info) <= EP_FIRST:
        return []
    # шаг 2: записи идут парами (uuid, флаг), последний элемент — флаг
    return [info[i] for i in range(EP_FIRST, len(info) - 1, 2)
            if is_uuid(info[i])]
