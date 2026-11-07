r"""Разбор прав роли из `Role.0.c1brace`.

Формат снят зондами на всех 921 ролях УНФ (страница state3 `role-rights-format`):

    root = [ версия, цели, шаблоны_RLS, маска3, флаг4, флаг5, маска6 ]
    цели = ["N", группа, …]
    группа = [ запись_цели, запись_прав ]          # всегда 2 записи, 35781/35781
    запись_цели = ['1', uuid, флагA, флагB]
                | ['1', uuid, '1', ['-K', uuid_коллекции], флагB]   # подобъект
    запись_прав = ['0', uuid_права, значение, …]                    # 34291
                | ['1', <число пар>, uuid_права, значение, …]       # 1490, с RLS
    RLS права   = … uuid_права, значение, '1', [uuid_права, ['1', ['1', '"текст"', '0']]]
    шаблоны_RLS = ["M", ['"имя шаблона"', '"текст шаблона"'], …]

Записи различаются ПО ПОЗИЦИИ в группе, а не по флагу: флаг '1' бывает и у цели,
и у записи прав (тогда второй элемент — число пар).

Имена прав в конфигурации ОТСУТСТВУЮТ (uuid прав — платформенные константы:
поиск по всем .json дампа не дал ни одного попадания), поэтому парсер
возвращает uuid как есть. Словарь `RIGHT_NAMES` (uuid → имя как в редакторе
прав) ПЕРЕСОБРАН 2026-11-07 по подтверждённым источникам: uuid → eng —
CSP-майнинг `dump/rights_map.py` по паре «выгрузка + cf-база одной
конфигурации» (биекция 64/64, RLS-якоря), eng → rus — официальная справка 8.3
«ПравоДоступа» (таблица `RIGHTS_EN_RU`); редкие uuid иных версий платформы
сохранены с пометкой «(не подтверждено)».

Значения прав: '1' = право установлено, '-1' = снято/не задано явно — та же
sparse-семантика, что у текстовых true/false в `Rights.xml` выгрузки
(подтверждено сопоставлением двух баз одной конфигурации).

Модуль также владеет ОБЩИМ для cf- и dump-конвейеров разрешением целей прав
в базе (`TargetResolver`): по uuid цели (скобкофайл .cf) и по точечному имени
(`Rights.xml` выгрузки), с единым соглашением — `target_object_id` реквизита,
ТЧ и подобъекта есть объект-владелец, так что все права объекта выбираются
одним условием.
"""
import re
from dataclasses import dataclass, field

# uuid вида права -> русское имя как в редакторе прав конфигуратора.
# ПЕРЕСОБРАН 2026-11-07 по подтверждённым источникам: uuid -> eng —
# глобальный CSP-майнинг dump/rights_map.py (биекция 64/64 по паре
# «выгрузка БП_РФ + cf-база», RLS-якоря), eng -> rus — официальная
# справка разработчика 8.3 «Глобальный контекст.ПравоДоступа».
# Прежняя версия словаря была системно перепутана (парный вывод без
# якорей): не используйте старые ревизии как источник имён прав.
# Записи, отсутствующие в майнинге (uuid других версий платформы),
# сохранены с прежними НЕПОДТВЕРЖДЁННЫМИ подписями — их мало, и они
# помечены «(не подтверждено)».
RIGHT_NAMES = {
    '02119c69-f08a-4142-9426-3725d74b7719': 'Automation',  # automation
    '399d7390-8d83-4a57-b4d7-c902c15b701f': 'Администрирование',  # administration
    'fd05f656-7a23-43a4-8996-f480a806fb97': 'АдминистрированиеДанных',  # dataadministration
    '900e3c92-6e18-4874-846a-b28780b5b54c': 'АдминистрированиеРасширенийКонфигурации',  # configurationextensionsadministration
    '10b8ce49-ae3d-4a2e-afe7-1e3648bd59f7': 'АктивныеПользователи',  # activeusers
    'b5f861d3-d9c5-45ec-98bf-0ed4d489a351': 'ВводПоСтроке',  # inputbystring
    'bd33c881-192c-4ef7-a51d-b146e38c5078': 'ВебКлиент',  # webclient
    '07ef4641-f7da-417a-bd75-35c40a17c2f7': 'ВнешнееСоединение',  # externalconnection
    '31c3d4f6-7d02-4654-a14e-06aacafcb4fa': 'Вывод',  # output
    '74fd69fa-368e-4292-956a-65eb2f9877bd': 'Выполнение',  # execute
    '33200740-82b0-4de7-8556-d3fb25ca4328': 'Добавление',  # insert
    '1c799cf9-342d-4bf7-9b6f-951a009228ce': 'ЖурналРегистрации',  # eventlog
    '287b74b8-3a66-4a76-ba27-4f1f6a93770e': 'Изменение',  # update
    'b162ff57-0296-483e-9af8-dc37576802cb': 'ИзменениеИсторииДанных',  # updatedatahistory
    'c4ab1331-e58d-4a46-ad2e-fe6d80b72aa4': 'ИзменениеИсторииДанныхОтсутствующихДанных',  # updatedatahistoryofmissingdata
    '5b3ea0e2-fdb9-41f6-bf6c-25747906b4cb': 'ИзменениеКомментарияВерсииИсторииДанных',  # updatedatahistoryversioncomment
    'a679c969-8ea1-4b8b-9e61-8a414ba448f4': 'ИзменениеНастроекИсторииДанных',  # updatedatahistorysettings
    '3b869658-ebc9-49ff-9bb3-e7c59686f538': 'ИнтерактивнаяАктивация',  # interactiveactivate
    '4d0d77ec-8511-430d-bd77-8407f27bc8f4': 'ИнтерактивнаяОтменаПроведения',  # interactiveundoposting
    'd76b72ba-5388-4b7f-af64-1b351f63a1e1': 'ИнтерактивнаяПометкаУдаления',  # interactivesetdeletionmark
    'e7f9daf9-eac2-4ada-9c26-c380858f3589': 'ИнтерактивнаяПометкаУдаленияПредопределенныхДанных',  # interactivesetdeletionmarkpredefineddata
    '5e664189-f0ee-439c-bdc5-eb81cca41ddf': 'ИнтерактивноеВыполнение',  # interactiveexecute
    'fb88c756-91c9-4351-9cdf-e027879886c6': 'ИнтерактивноеДобавление',  # interactiveinsert
    'b0c0cbfc-f2cc-4b80-8460-5d5d7a599d9d': 'ИнтерактивноеИзменениеПроведенных',  # interactivechangeofposted
    '7b8359dd-7d4e-4bcd-a61c-b4b26eae19c6': 'ИнтерактивноеОткрытиеВнешнихОбработок',  # interactiveopenextdataprocessors
    'eb29e198-c338-4a20-a253-be6fc3dd44d9': 'ИнтерактивноеОткрытиеВнешнихОтчетов',  # interactiveopenextreports
    '21b4742a-d335-4234-bf0f-a3074a0e31ac': 'ИнтерактивноеПроведение',  # interactiveposting
    '5d167fcc-b11f-403a-9a37-1eda64c19df1': 'ИнтерактивноеПроведениеНеОперативное',  # interactivepostingregular
    '798cf688-ad74-44fe-a464-236b49e910e0': 'ИнтерактивноеСнятиеПометкиУдаления',  # interactivecleardeletionmark
    '013a262e-165f-4815-bdae-7a1bed6a68e4': 'ИнтерактивноеСнятиеПометкиУдаленияПредопределенныхДанных',  # interactivecleardeletionmarkpredefineddata
    'b53db6ed-6e5b-4035-8d24-f10083d646ed': 'ИнтерактивноеУдаление',  # interactivedelete
    'fa6dbe86-856a-4ac4-b8ac-bce99f8b8b22': 'ИнтерактивноеУдалениеПомеченных',  # interactivedeletemarked
    '408c56c0-e210-4e2e-8e82-610050a08a39': 'ИнтерактивноеУдалениеПомеченныхПредопределенныхДанных',  # interactivedeletemarkedpredefineddata
    '65e5f92c-40ff-4130-9652-c0e7612d0609': 'ИнтерактивноеУдалениеПредопределенныхДанных',  # interactivedeletepredefineddata
    '84487e82-eb6c-4c51-ae16-3a6db17e886d': 'ИнтерактивныйСтарт',  # interactivestart
    'c6de80da-a4f7-4ce9-bbeb-0b00ea564ec1': 'Использование',  # use
    'f7c6a0bb-bca6-4cd3-9146-832971cd7073': 'КлиентСистемыАналитики',  # analyticssystemclient
    '3c00c6ee-844e-4620-85e4-671e72f114d9': 'МонопольныйРежим',  # exclusivemode
    '4d87a22d-ca7f-40ba-a367-a4eae62f4a7f': 'ОбновлениеКонфигурацииБазыДанных',  # updatedatabaseconfiguration
    'f55a8f7f-2c65-404f-b530-093d9006adba': 'ОтменаПроведения',  # undoposting
    '479a42c0-c3e9-4ae7-bf4a-75cebc14fec4': 'ПереходНаВерсиюИсторииДанных',  # switchtodatahistoryversion
    '499e8968-ca89-43f0-9955-8756058b1b53': 'Получение',  # get
    'e060de25-bffd-42fd-bb09-f3a788d65760': 'Проведение',  # posting
    'aa6448f2-be0f-42ea-ba26-1af7f52b5b65': 'Просмотр',  # view
    '9342b152-a7ae-4c79-9b7b-f4f028a36479': 'ПросмотрИсторииДанных',  # viewdatahistory
    '3762abec-3836-446a-83ce-3e05001bca8b': 'РегистрацияИнформационнойБазыСистемыВзаимодействия',  # collaborationsysteminfobaseregistration
    'b7bab52d-c1b1-4bd8-8276-02db08d42352': 'Редактирование',  # edit
    '8497054a-ffd1-4ca7-bdfe-340b9ddc050a': 'РедактированиеКомментарияВерсииИсторииДанных',  # editdatahistoryversioncomment
    '155a0b35-4343-4047-989b-d385373b063e': 'РежимОсновногоОкнаВстроенноеРабочееМесто',  # mainwindowmodeembeddedworkplace
    'b9b44b51-3ac9-47cd-8b5a-df51afdcceb0': 'РежимОсновногоОкнаКиоск',  # mainwindowmodekiosk
    'd066966a-ff6a-4a41-bd68-6191cab083bc': 'РежимОсновногоОкнаОбычный',  # mainwindowmodenormal
    '818fc6c3-4691-44e3-a80c-e8d424730ead': 'РежимОсновногоОкнаПолноэкранноеРабочееМесто',  # mainwindowmodefullscreenworkplace
    'f6168734-8b8d-4a88-ab39-ef6b51758e83': 'РежимОсновногоОкнаРабочееМесто',  # mainwindowmodeworkplace
    '265eec41-3ce1-4a07-bc3b-253d44c9a4f4': 'РежимТехническогоСпециалиста',  # technicalspecialistmode
    'd8682bbb-7800-4aa0-8590-d3cb11fe2a29': 'СохранениеДанныхПользователя',  # saveuserdata
    '65b6855f-85d5-4d33-ab75-be4485326dd5': 'Старт',  # start
    '29da0973-3b85-40e5-89da-bce02dbab08e': 'ТолстыйКлиент',  # thickclient
    '8fb221e3-0d4f-43f2-ad71-1984cad63375': 'ТонкийКлиент',  # thinclient
    'c0028105-4cc1-41ca-aef1-bfbd8fc8f8c4': 'Удаление',  # delete
    '24abfe06-289a-48c5-8bb4-032c733e45c5': 'УправлениеИтогами',  # totalscontrol
    '1d306db2-d97e-4b57-9b28-5d21e838cd9e': 'Установка',  # set
    '1c87578f-9e09-4ec0-a991-5629c87b1588': 'Чтение',  # read
    '64319ca1-f3d8-472e-82ce-5da233e6daaa': 'ЧтениеИсторииДанных',  # readdatahistory
    '1b762bf9-df7f-4255-bbe6-f7578f41368d': 'ЧтениеИсторииДанныхОтсутствующихДанных',  # readdatahistoryofmissingdata
    '1d306db2-d97e-4b57-9b6f-951a009228ce': 'Получение',  # (не подтверждено)
    '1e50809b-73ed-4935-bb77-2616c4cabdf5': 'ОсновнойРежимОкнаНормальный',  # (не подтверждено)
    '4df6d046-3bf8-4dda-991c-53ba664296a5': 'ИнтерактивноеВыполнение',  # (не подтверждено)
    '669fef9d-9d4c-4333-8237-66351429d935': 'ИнтерактивнаяПометкаУдаленияПредопределённыхДанных',  # (не подтверждено)
    '963624dc-9b02-4c20-a3f0-015ac64c6d81': 'Проведение',  # (не подтверждено)
}

# Английские имена прав (Rights.xml выгрузки, ПравоДоступа) -> русские.
# Источник: официальная справка разработчика 8.3, метод ПравоДоступа.
RIGHTS_EN_RU = {
    'activeusers': 'АктивныеПользователи',
    'administration': 'Администрирование',
    'allfunctionsmode': 'РежимВсеФункции',
    'analyticssystemclient': 'КлиентСистемыАналитики',
    'automation': 'Automation',
    'collaborationsysteminfobaseregistration': 'РегистрацияИнформационнойБазыСистемыВзаимодействия',
    'configurationextensionsadministration': 'АдминистрированиеРасширенийКонфигурации',
    'dataadministration': 'АдминистрированиеДанных',
    'delete': 'Удаление',
    'edit': 'Редактирование',
    'editdatahistoryversioncomment': 'РедактированиеКомментарияВерсииИсторииДанных',
    'eventlog': 'ЖурналРегистрации',
    'exclusivemode': 'МонопольныйРежим',
    'exclusivemodeterminationatsessionstart': 'ЗавершениеМонопольногоРежимаПриНачалеСеанса',
    'execute': 'Выполнение',
    'externalconnection': 'ВнешнееСоединение',
    'get': 'Получение',
    'inputbystring': 'ВводПоСтроке',
    'insert': 'Добавление',
    'interactiveactivate': 'ИнтерактивнаяАктивация',
    'interactivechangeofposted': 'ИнтерактивноеИзменениеПроведенных',
    'interactivecleardeletionmark': 'ИнтерактивноеСнятиеПометкиУдаления',
    'interactivecleardeletionmarkpredefineddata': 'ИнтерактивноеСнятиеПометкиУдаленияПредопределенныхДанных',
    'interactivedelete': 'ИнтерактивноеУдаление',
    'interactivedeletemarked': 'ИнтерактивноеУдалениеПомеченных',
    'interactivedeletemarkedpredefineddata': 'ИнтерактивноеУдалениеПомеченныхПредопределенныхДанных',
    'interactivedeletepredefineddata': 'ИнтерактивноеУдалениеПредопределенныхДанных',
    'interactiveexecute': 'ИнтерактивноеВыполнение',
    'interactiveinsert': 'ИнтерактивноеДобавление',
    'interactiveopenextdataprocessors': 'ИнтерактивноеОткрытиеВнешнихОбработок',
    'interactiveopenextreports': 'ИнтерактивноеОткрытиеВнешнихОтчетов',
    'interactiveposting': 'ИнтерактивноеПроведение',
    'interactivepostingregular': 'ИнтерактивноеПроведениеНеОперативное',
    'interactivesetdeletionmark': 'ИнтерактивнаяПометкаУдаления',
    'interactivesetdeletionmarkpredefineddata': 'ИнтерактивнаяПометкаУдаленияПредопределенныхДанных',
    'interactivestart': 'ИнтерактивныйСтарт',
    'interactiveundoposting': 'ИнтерактивнаяОтменаПроведения',
    'mainwindowmodeembeddedworkplace': 'РежимОсновногоОкнаВстроенноеРабочееМесто',
    'mainwindowmodefullscreenworkplace': 'РежимОсновногоОкнаПолноэкранноеРабочееМесто',
    'mainwindowmodekiosk': 'РежимОсновногоОкнаКиоск',
    'mainwindowmodenormal': 'РежимОсновногоОкнаОбычный',
    'mainwindowmodeworkplace': 'РежимОсновногоОкнаРабочееМесто',
    'mobileclient': 'МобильныйКлиент',
    'output': 'Вывод',
    'posting': 'Проведение',
    'read': 'Чтение',
    'readdatahistory': 'ЧтениеИсторииДанных',
    'readdatahistoryofmissingdata': 'ЧтениеИсторииДанныхОтсутствующихДанных',
    'remotedesktopclient': 'УдаленноеУправлениеРабочимСтоломДругогоПользователя',
    'remotedesktophost': 'ПредоставлениеУдаленногоУправленияРабочимСтоломДругомуПользователю',
    'saveuserdata': 'СохранениеДанныхПользователя',
    'sessionosauthenticationchange': 'ИзменениеАутентификацииОССеанса',
    'sessionstandardauthenticationchange': 'ИзменениеСтандартнойАутентификацииСеанса',
    'set': 'Установка',
    'standardauthenticationchange': 'ИзменениеСтандартнойАутентификации',
    'start': 'Старт',
    'switchtodatahistoryversion': 'ПереходНаВерсиюИсторииДанных',
    'technicalspecialistmode': 'РежимТехническогоСпециалиста',
    'thickclient': 'ТолстыйКлиент',
    'thinclient': 'ТонкийКлиент',
    'totalscontrol': 'УправлениеИтогами',
    'undoposting': 'ОтменаПроведения',
    'update': 'Изменение',
    'updatedatabaseconfiguration': 'ОбновлениеКонфигурацииБазыДанных',
    'updatedatahistory': 'ИзменениеИсторииДанных',
    'updatedatahistoryofmissingdata': 'ИзменениеИсторииДанныхОтсутствующихДанных',
    'updatedatahistorysettings': 'ИзменениеНастроекИсторииДанных',
    'updatedatahistoryversioncomment': 'ИзменениеКомментарияВерсииИсторииДанных',
    'use': 'Использование',
    'view': 'Просмотр',
    'viewdatahistory': 'ПросмотрИсторииДанных',
    'webclient': 'ВебКлиент',
}

UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                     r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')

VALUE_GRANTED = '1'
VALUE_DENIED = '0'
VALUE_UNSET = '-1'


@dataclass
class RightEntry:
    """Одно право одной цели: uuid, значение и текст RLS, если он есть."""

    right_uuid: str
    value: str
    rls_text: str = None

    @property
    def granted(self):
        """True — выдано, False — запрещено, None — не задано/не распознано."""
        if self.value == VALUE_GRANTED:
            return True
        if self.value == VALUE_DENIED:
            return False
        return None

    @property
    def right_name(self):
        """Имя права из словаря или None (тогда показывать uuid с пометкой)."""
        return RIGHT_NAMES.get(self.right_uuid.lower())


@dataclass
class RightTarget:
    """Цель прав: объект метаданных либо его подобъект (реквизит, ТЧ, форма)."""

    uuid: str
    rights: list = field(default_factory=list)
    sub_index: int = None
    collection_uuid: str = None
    flag_a: str = None
    flag_b: str = None

    @property
    def is_subitem(self):
        """True, если цель адресована подобъектом, а не объектом целиком."""
        return self.sub_index is not None


@dataclass
class RoleRights:
    """Права одной роли: версия формата, цели, шаблоны RLS и хвостовые флаги."""

    version: str
    targets: list = field(default_factory=list)
    rls_templates: tuple = ()
    flags: tuple = ()

    def entries(self):
        """Плоский перечень (цель, право) — то, что writer пишет в role_right."""
        for target in self.targets:
            for entry in target.rights:
                yield target, entry


def _text(value):
    """Строка скобкофайла -> обычный текст: снять кавычки и удвоения."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.replace('""', '"')


def _rls_of(node):
    """Текст ограничения доступа из вложенной структуры значения права.

    Наблюдаемая форма: [uuid_права, ['1', ['1', '"ГДЕ …"', '0']]] — текст лежит
    на произвольной глубине, поэтому ищем первую строку в кавычках.
    """
    if isinstance(node, str):
        return _text(node) if node.lstrip().startswith('"') else None
    if isinstance(node, list):
        for item in node:
            found = _rls_of(item)
            if found:
                return found
    return None


def _is_uuid(value):
    return isinstance(value, str) and bool(UUID_RE.match(value))


def _attach_rls(entries, node):
    """Привязать текст ограничения к праву, которое названо в самом блоке.

    Блок RLS — `[uuid_права, ['1', ['1', '"текст"', '0']]]`: uuid права лежит
    ВНУТРИ блока, поэтому текст достаётся уже разобранной паре, а не той, что
    шла перед ним.
    """
    if not isinstance(node, list) or not node or not _is_uuid(node[0]):
        return
    text = _rls_of(node[1] if len(node) > 1 else node)
    if not text:
        return
    key = node[0].lower()
    for entry in reversed(entries):
        if entry.right_uuid == key:
            entry.rls_text = text
            return
    entries.append(RightEntry(key, VALUE_UNSET, text))


def _parse_rights(record):
    """Запись прав -> список RightEntry (пары uuid/значение плюс RLS).

    Раскладка (снята на реальных файлах):
      ['0', uuid, значение, uuid, значение, …]                     — без счётчиков;
      ['1', <число пар>, uuid, значение, …, <число RLS>, блок_RLS…] — со счётчиками.
    Блоки RLS идут списком после пар, каждый со своим uuid права, поэтому
    нестроковые токены разбираются отдельно, а счётчики просто пропускаются.
    """
    items = record[1:]
    if items and str(items[0]).lstrip('-').isdigit():
        items = items[1:]
    out = []
    i = 0
    while i < len(items):
        item = items[i]
        if isinstance(item, list):
            _attach_rls(out, item)
            i += 1
            continue
        if not _is_uuid(item) or i + 1 >= len(items):
            i += 1                      # счётчик RLS, маркер или хвост без значения
            continue
        value = items[i + 1]
        if isinstance(value, list):
            _attach_rls(out, value)
            out.append(RightEntry(item.lower(), VALUE_UNSET))
        else:
            out.append(RightEntry(item.lower(), str(value)))
        i += 2
    return out


def _parse_target(record):
    """Запись цели ['1', uuid, …] -> RightTarget (без прав)."""
    uuid = str(record[1]).lower() if len(record) > 1 else ''
    target = RightTarget(uuid=uuid)
    target.flag_a = record[2] if len(record) > 2 else None
    nested = record[3] if len(record) > 3 else None
    if isinstance(nested, list) and nested:
        try:
            target.sub_index = int(nested[0])
        except (TypeError, ValueError):
            target.sub_index = None
        if len(nested) > 1 and isinstance(nested[1], str):
            target.collection_uuid = nested[1].lower()
        target.flag_b = record[4] if len(record) > 4 else None
    else:
        target.flag_b = nested
    return target


def _is_rights_record(record):
    """Отличить запись прав от записи цели: у прав второй элемент — число пар."""
    if not isinstance(record, list) or len(record) < 2:
        return False
    if record[0] == '0':
        return True
    return record[0] == '1' and str(record[1]).lstrip('-').isdigit()


def parse_role_rights(data):
    """Разобрать структуру из `helper.brace_file_read` в RoleRights.

    Принимает именно список (содержимое скобкофайла), а не путь — так парсер
    тестируется на синтетической фикстуре без дампа конфигурации.
    """
    root = data[0] if (data and isinstance(data[0], list)) else data
    # битый или чужой файл обязан быть ошибкой, а не «пустыми правами»:
    # иначе «данные недоступны» неотличимы от «права роли не заданы»
    if (not isinstance(root, list) or len(root) < 3
            or str(root[0]) not in ('8', '9', '10')):
        raise ValueError(f'нераспознанный формат прав роли: {str(root)[:60]}')
    version = str(root[0]) if root else ''
    targets_raw = root[1] if len(root) > 1 else ['0']
    templates_raw = root[2] if len(root) > 2 else ['0']
    flags = tuple(str(x) for x in root[3:7])

    targets = []
    for group in (targets_raw[1:] if isinstance(targets_raw, list) else []):
        records = group if (group and isinstance(group[0], list)) else [group]
        if not records:
            continue
        target = _parse_target(records[0])
        targets.append(target)
        rest = records[1:]
        if rest and _is_rights_record(rest[0]):
            target.rights.extend(_parse_rights(rest[0]))
            rest = rest[1:]
        # защита: цель без прав, за которой сразу следующая цель
        for record in rest:
            if isinstance(record, list) and record and record[0] == '1':
                targets.append(_parse_target(record))

    templates = []
    for item in (templates_raw[1:] if isinstance(templates_raw, list) else []):
        rows = item if (item and isinstance(item[0], list)) else [item]
        for row in rows:
            if isinstance(row, list) and len(row) >= 2:
                templates.append((_text(row[0]), _text(row[1])))
    return RoleRights(version=version, targets=targets,
                      rls_templates=tuple(templates), flags=flags)


def parse_role_file(directory, file_name='Role.0.c1brace'):
    """Прочитать и разобрать файл прав роли из каталога дампа."""
    from .v8 import helper

    return parse_role_rights(helper.brace_file_read(directory, file_name))


# --- разрешение целей прав в базе (общее cf- и dump-путям) --------------------

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

# сегмент имени цели → kind в meta_subobject
SUBOBJECT_KIND = {
    'Operation': 'webservice_operation',
    'Method': 'httpservice_method',
    'URLTemplate': 'httpservice_url_template',
    'Parameter': 'webservice_parameter',
    'IntegrationServiceChannel': 'integration_channel',
    'RoutePoint': 'businessprocess_routepoint',
}


class TargetResolver:
    """Цель права → id строк базы; общий резолвер обоих конвейеров.

    Два направления по двум форматам источника:
    - `resolve(name)` — точечное имя цели из `Rights.xml` выгрузки
      (`Catalog.Валюты[.Attribute.Код|.TabularSection.Т|.Form.Ф|…]`),
      возвращает (target_uuid, obj_id, attr_id, tab_id, sub_id);
    - `resolve_uuid(uuid)` — uuid цели из `Role.0.c1brace` (.cf), возвращает
      (obj_id, attr_id, tab_id, sub_id); карта uuid лениво строится одним
      проходом по четырём таблицам.

    Соглашение разрешения (единое для cf- и dump-баз): obj_id для реквизита,
    ТЧ и подобъекта — объект-ВЛАДЕЛЕЦ, поэтому все права, затрагивающие
    объект, выбираются одним условием по target_object_id. Неразрешённая цель
    — все None; cf-путь хранит в target_uuid её uuid, dump-путь — имя.
    """

    def __init__(self, conn):
        self.conn = conn
        self._cache = {}
        self._uuid_map = None

    def resolve(self, name):
        if name in self._cache:
            return self._cache[name]
        result = self._resolve(name)
        self._cache[name] = result
        return result

    def resolve_uuid(self, uuid):
        """(obj_id, attr_id, tab_id, sub_id) по uuid цели; всё None, если нет."""
        if self._uuid_map is None:
            self._uuid_map = uuid_targets(self.conn)
        return self._uuid_map.get(uuid.lower(), (None, None, None, None))

    def _resolve(self, name):
        parts = name.split('.')
        if not parts:
            return None
        if parts[0] == 'Configuration':
            # цель «Configuration.<имя конфигурации>» — корневой объект;
            # её права (Администрирование, ВебКлиент, ОсновнойРежимОкна…)
            # имеют собственные uuid видов прав — их покрывает майнинговая
            # таблица dump/rights_map.RIGHT_UUIDS
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
                    return (row[1], obj_id, None, row[0], None)
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
                return (row[1], obj_id, None, None, row[0])
            return None
        if head in SUBOBJECT_KIND and len(rest) == 2:
            row = self.conn.execute(
                'SELECT id, uuid FROM meta_subobject'
                ' WHERE object_id=? AND kind=? AND name=?',
                (obj_id, SUBOBJECT_KIND[head], rest[1])).fetchone()
            if row:
                return (row[1], obj_id, None, None, row[0])
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
            return (row[1], obj_id, row[0], None, None)
        return None


def uuid_targets(conn):
    """Карта uuid цели права -> (объект-владелец, реквизит, ТЧ, подобъект).

    Цель — объект метаданных, его реквизит/поле ТЧ, его табличная часть или
    подобъект (операция веб-сервиса, метод HTTP-сервиса, форма, команда),
    поэтому uuid ищется в четырёх таблицах; незаполненные id остаются None.
    `target_object_id` для реквизита, ТЧ и подобъекта — объект-владелец, так
    что все права, затрагивающие объект, выбираются одним условием. Замер на
    УНФ: 16953 цели разрешаются объектом, 12276 — реквизитом, 520 — табличной
    частью. Права на конфигурацию адресованы uuid метаданных Configuration —
    у cf-корня это `file_uuid` заголовка, который и хранится в
    `meta_object.uuid` (тот же uuid, что у корня dump-базы), поэтому такие
    цели разрешаются объектом; остаток неразрешённых — `StandardAttribute.*`
    (стандартные реквизиты не извлекаются).
    """
    targets = {}
    for uuid, obj_id in conn.execute(
            'SELECT uuid, id FROM meta_object WHERE uuid IS NOT NULL'):
        targets[uuid.lower()] = (obj_id, None, None, None)
    for uuid, attr_id, obj_id in conn.execute(
            'SELECT uuid, id, object_id FROM meta_attribute'
            ' WHERE uuid IS NOT NULL'):
        targets[uuid.lower()] = (obj_id, attr_id, None, None)
    for uuid, tab_id, obj_id in conn.execute(
            'SELECT uuid, id, object_id FROM meta_tabular WHERE uuid IS NOT NULL'):
        targets[uuid.lower()] = (obj_id, None, tab_id, None)
    for uuid, sub_id, obj_id in conn.execute(
            'SELECT uuid, id, object_id FROM meta_subobject WHERE uuid IS NOT NULL'):
        targets[uuid.lower()] = (obj_id, None, None, sub_id)
    return targets
