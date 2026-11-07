"""Модель выгрузки конфигурации в файлы: реестры классов и сканер дерева.

Соответствия подобраны так, чтобы база из выгрузки совпадала с базой из .cf
той же конфигурации (эталон: `cf\\БП_РФ` против `_out\\БП_РФ.db`):

- каталог выгрузки (множественное имя) → тип объекта в БД (`CLASS_DIRS`);
  два типа в реестре платформы сами во множественном числе — DocumentNumerators
  и Sequences;
- формы — тип `<Класс>Form`, у обработок особое имя `Form` (как у декодера
  .cf: «Форма внешней обработки»); команды — `<Класс>Command`; макеты объектов
  — тип `Template`; путь в БД: `<Класс>/<Объект>/<ТипПодобъекта>/<Имя>`;
- файлы модулей → `code_name` в таблице module (`MODULE_CODE`): obj/mgr и
  корневые app/802/con/seance — имена потоков декодера .cf.
"""
import os

# Каталог выгрузки (множественное число) → тип объекта в базе.
# Типы — имена классов реестра платформы (v8.metadata_types.MetaDataTypes).
CLASS_DIRS = {
    'AccountingRegisters': 'AccountingRegister',
    'AccumulationRegisters': 'AccumulationRegister',
    'BusinessProcesses': 'BusinessProcess',
    'CalculationRegisters': 'CalculationRegister',
    'Catalogs': 'Catalog',
    'ChartsOfAccounts': 'ChartOfAccounts',
    'ChartsOfCalculationTypes': 'ChartOfCalculationTypes',
    'ChartsOfCharacteristicTypes': 'ChartOfCharacteristicType',
    'CommandGroups': 'CommandGroup',
    'CommonAttributes': 'CommonAttribute',
    'CommonCommands': 'CommonCommand',
    'CommonForms': 'CommonForm',
    'CommonModules': 'CommonModule',
    'CommonPictures': 'CommonPicture',
    'CommonTemplates': 'CommonTemplate',
    'Constants': 'Constant',
    'DataProcessors': 'DataProcessor',
    'DefinedTypes': 'DefinedType',
    'DocumentJournals': 'DocumentJournal',
    'DocumentNumerators': 'DocumentNumerators',
    'Documents': 'Document',
    'Enums': 'Enum',
    'EventSubscriptions': 'EventSubscription',
    'ExchangePlans': 'ExchangePlan',
    'ExternalDataSources': 'ExternalDataSource',
    'FilterCriteria': 'FilterCriterion',
    'FunctionalOptions': 'FunctionalOption',
    'FunctionalOptionsParameters': 'FunctionalOptionsParameter',
    'HTTPServices': 'HTTPService',
    'InformationRegisters': 'InformationRegister',
    'IntegrationServices': 'IntegrationService',
    'Interfaces': 'Interface',
    'Languages': 'Language',
    'Reports': 'Report',
    'Roles': 'Role',
    'ScheduledJobs': 'ScheduledJob',
    'Sequences': 'Sequences',
    'SessionParameters': 'SessionParameter',
    'SettingsStorages': 'SettingsStorage',
    'StyleItems': 'StyleItem',
    'Styles': 'Style',
    'Subsystems': 'Subsystem',
    'Tasks': 'Task',
    'WSReferences': 'WSReference',
    'WebServices': 'WebService',
    'XDTOPackages': 'XDTOPackage',
}

# Классы, у которых формы — подобъекты (каталог Forms внутри папки объекта).
# CommonForm — самостоятельный класс, в этот список не входит. У обработок
# формы есть, но тип подобъекта особый — 'Form' (см. form_type).
FORM_CLASSES = {
    'AccountingRegister', 'AccumulationRegister', 'BusinessProcess',
    'CalculationRegister', 'Catalog', 'ChartOfAccounts',
    'ChartOfCalculationTypes', 'ChartOfCharacteristicType', 'DataProcessor',
    'Document', 'DocumentJournal', 'Enum', 'ExchangePlan',
    'ExternalDataSource', 'FilterCriterion', 'InformationRegister', 'Report',
    'SettingsStorage', 'Task',
}

# Классы с командами (каталог Commands внутри папки объекта)
COMMAND_CLASSES = {
    'AccountingRegister', 'AccumulationRegister', 'BusinessProcess',
    'CalculationRegister', 'Catalog', 'ChartOfAccounts',
    'ChartOfCalculationTypes', 'ChartOfCharacteristicType', 'DataProcessor',
    'Document', 'DocumentJournal', 'Enum', 'ExchangePlan', 'FilterCriterion',
    'InformationRegister', 'Report', 'Task',
}

# Классы с макетами (каталог Templates внутри папки объекта)
TEMPLATE_CLASSES = {
    'AccountingRegister', 'AccumulationRegister', 'BusinessProcess',
    'CalculationRegister', 'Catalog', 'ChartOfAccounts',
    'ChartOfCalculationTypes', 'ChartOfCharacteristicType', 'CommonCommand',
    'CommonForm', 'CommonTemplate', 'DataProcessor', 'Document',
    'DocumentJournal', 'Enum', 'ExchangePlan', 'FilterCriterion',
    'HTTPService', 'InformationRegister', 'IntegrationService', 'Report',
    'SettingsStorage', 'Task', 'WebService',
}


def form_type(obj_type):
    """Тип формы объекта: <Класс>Form; у обработок — Form (как в базе из .cf)."""
    if obj_type == 'DataProcessor':
        return 'Form'
    return obj_type + 'Form'


def command_type(obj_type):
    """Тип команды объекта: <Класс>Command."""
    return obj_type + 'Command'


# Имя файла модуля в выгрузке → code_name в таблице module (имена потоков
# декодера .cf). RecordSetModule регистра и ValueManagerModule константы —
# это obj-поток объекта; ManagerModule — mgr; корневые модули конфигурации —
# app/802/con/seance (проверено по cf-базе: другой набор code_name не встречается).
MODULE_CODE = {
    'ObjectModule.bsl': 'obj',
    'ManagerModule.bsl': 'mgr',
    'RecordSetModule.bsl': 'obj',
    'ValueManagerModule.bsl': 'obj',
    'Module.bsl': 'obj',
    'CommandModule.bsl': 'obj',
    'ManagedApplicationModule.bsl': 'app',
    'OrdinaryApplicationModule.bsl': '802',
    'ExternalConnectionModule.bsl': 'con',
    'SessionModule.bsl': 'seance',
}

# Служебные файлы корня выгрузки (не объект метаданных)
ROOT_SERVICE_FILES = ('ConfigDumpInfo.xml',)


class DumpObject(dict):
    """Один объект/подобъект выгрузки.

    Ключи: kind ('root'|'object'|'form'|'command'|'template'), type, name,
    path (путь в стиле БД: 'Catalog/Валюты', '' — корень), parent_path,
    xml (путь к XML метаданных или None), folder (папка содержимого или None),
    granule (path объекта верхнего уровня — гранула инкрементального
    обновления; '' — корневая гранула).
    """

    __getattr__ = dict.get


def _sub_xml_names(folder, sub_dir):
    """Имена подобъектов по *.xml в подкаталоге (Формы/Макеты/подсистемы)."""
    d = os.path.join(folder, sub_dir)
    if not os.path.isdir(d):
        return []
    return sorted(fn[:-4] for fn in os.listdir(d) if fn.endswith('.xml'))


def _sub_dir_names(folder, sub_dir):
    """Имена подобъектов по подкаталогам (Команды: XML встроен в родителя,
    в папке команды лежит только Ext\\CommandModule.bsl)."""
    d = os.path.join(folder, sub_dir)
    if not os.path.isdir(d):
        return []
    return sorted(fn for fn in os.listdir(d)
                  if os.path.isdir(os.path.join(d, fn)))


def scan(dump_dir):
    """Обход выгрузки: список DumpObject в порядке вставки + файлы по гранулам.

    Порядок: корень, затем объекты верхнего уровня по каталогам классов
    (алфавит), подобъекты сразу после родителя. Порядок `ord` среди братьев
    определяется отдельно по XML (ChildObjects) — не порядком обхода.

    Команды объектов не имеют собственного XML — их метаданные встроены в
    `<ChildObjects>` родителя (узел `<Command uuid=…>`), а папка
    `Commands\\<Имя>\\` хранит только модуль; поэтому у команды xml=None,
    и её свойства достраивает разбор родительского XML. Вложенные подсистемы
    (`Subsystems\\<Р>\\Subsystems\\<Д>.xml`) обходятся рекурсивно.

    :return: (objects, granule_files, warnings)
        objects — список DumpObject;
        granule_files — {granule_path: [rel_path, ...]} все файлы выгрузки,
            каждый принадлежит ровно одной грануле (объект верхнего уровня
            вместе с формами/командами/макетами, либо корень);
        warnings — строки диагностики (неизвестные каталоги/файлы модулей).
    """
    dump_dir = os.path.abspath(dump_dir)
    objects = []
    granule_files = {}
    warnings = []

    def add_granule_file(granule, full):
        rel = os.path.relpath(full, dump_dir).replace(os.sep, '/')
        granule_files.setdefault(granule, []).append(rel)

    def add_tree_files(granule, folder):
        for dirpath, _dirs, files in os.walk(folder):
            for f in sorted(files):
                add_granule_file(granule, os.path.join(dirpath, f))

    # корень: Configuration.xml + Ext\ + служебные файлы
    root_files = [fn for fn in os.listdir(dump_dir)
                  if os.path.isfile(os.path.join(dump_dir, fn))]
    for fn in sorted(root_files):
        add_granule_file('', os.path.join(dump_dir, fn))
    ext_dir = os.path.join(dump_dir, 'Ext')
    if os.path.isdir(ext_dir):
        add_tree_files('', ext_dir)
    objects.append(DumpObject(
        kind='root', type='Configuration', name=None, path='',
        parent_path=None, xml=os.path.join(dump_dir, 'Configuration.xml'),
        folder=ext_dir if os.path.isdir(ext_dir) else None, granule=''))

    def add_object(obj_type, name, parent_path, g_path, granule, xml, folder,
                   kind='object', with_children=True):
        """Один объект выгрузки + рекурсивно его подобъекты."""
        objects.append(DumpObject(
            kind=kind, type=obj_type, name=name, path=g_path,
            parent_path=parent_path, xml=xml,
            folder=folder if folder and os.path.isdir(folder) else None,
            granule=granule))
        # XML объекта верхнего уровня лежит рядом с папкой (в каталоге класса)
        # и обходом папки не покрывается; XML подобъектов (формы, макеты,
        # вложенные подсистемы) лежит ВНУТРИ папки гранулы — их покрывает обход
        if xml and parent_path == '' and os.path.isfile(xml):
            add_granule_file(granule, xml)
        if not with_children or not folder or not os.path.isdir(folder):
            return
        if obj_type in FORM_CLASSES:
            stype = form_type(obj_type)
            for sname in _sub_xml_names(folder, 'Forms'):
                add_object(stype, sname, g_path, f'{g_path}/{stype}/{sname}',
                           granule,
                           os.path.join(folder, 'Forms', sname + '.xml'),
                           os.path.join(folder, 'Forms', sname), kind='form',
                           with_children=False)
        if obj_type in COMMAND_CLASSES:
            stype = command_type(obj_type)
            for sname in _sub_dir_names(folder, 'Commands'):
                add_object(stype, sname, g_path, f'{g_path}/{stype}/{sname}',
                           granule, None,
                           os.path.join(folder, 'Commands', sname),
                           kind='command', with_children=False)
        if obj_type in TEMPLATE_CLASSES:
            for sname in _sub_xml_names(folder, 'Templates'):
                add_object('Template', sname, g_path,
                           f'{g_path}/Template/{sname}', granule,
                           os.path.join(folder, 'Templates', sname + '.xml'),
                           os.path.join(folder, 'Templates', sname),
                           kind='template', with_children=False)
        # вложенные подсистемы: Subsystems\<Д>.xml + папка <Д>
        if obj_type == 'Subsystem':
            for sname in _sub_xml_names(folder, 'Subsystems'):
                add_object('Subsystem', sname, g_path,
                           f'{g_path}/Subsystem/{sname}', granule,
                           os.path.join(folder, 'Subsystems', sname + '.xml'),
                           os.path.join(folder, 'Subsystems', sname))

    known_dirs = set(CLASS_DIRS)
    for entry in sorted(os.listdir(dump_dir)):
        full = os.path.join(dump_dir, entry)
        if not os.path.isdir(full):
            if entry == 'Ext' or entry in ROOT_SERVICE_FILES \
                    or entry == 'Configuration.xml':
                continue
            warnings.append(f'неизвестный файл в корне выгрузки: {entry}')
            continue
        if entry == 'Ext':
            continue
        if entry not in known_dirs:
            warnings.append(f'неизвестный каталог класса: {entry}')
            continue
        obj_type = CLASS_DIRS[entry]
        for fn in sorted(os.listdir(full)):
            if not fn.endswith('.xml'):
                continue
            name = fn[:-4]
            g_path = f'{obj_type}/{name}'
            add_object(obj_type, name, '', g_path, g_path,
                       os.path.join(full, fn), os.path.join(full, name))
            folder = os.path.join(full, name)
            if os.path.isdir(folder):
                add_tree_files(g_path, folder)
    return objects, granule_files, warnings
