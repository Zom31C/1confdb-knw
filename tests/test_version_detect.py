"""Определение парсера по файлам версии (version/root у cf, configinfo у cfe).

Плавающий баг (жалоба 2026-11-19): в выгрузках в режиме совместимости номер
версии платформы во внутреннем блоке замещается флагом '1' (реальная версия
может лежать рядом, {1,80312}). Раньше cfe-ветка принимала только '803…' и
падала «Не удалось определить парсер»; cf-ветка слепo маппила '1' в 8.2.
Здесь закреплена терпимость к обоим случаям без потери формата 8.3.
"""
import pytest

from confdb.v8.decoder import Decoder, _find_platform_version
from confdb.v8.meta_object import Configuration, ConfigurationExtension
from confdb.v8.metadata_types import MetaDataTypes

CONFIG_UUID = MetaDataTypes.Configuration.value


def _cfe(inner):
    """configinfo расширения: [ ['0', ['216','0', ВНУТРЕННИЙ_БЛОК]] ]."""
    return [['0', ['216', '0', inner]]]


def _cf(inner):
    """version/root/header конфигурации с внутренним блоком версии inner."""
    version = [[['216', '0', inner]]]
    root = [['2', '12733ee5-e3bd-4574-a78c-bb9510280b1b', 'b64']]
    header = [['0', 'x', 'y', [CONFIG_UUID]]]
    return version, root, header


def _handler_cf(inner):
    version, root, header = _cf(inner)
    opts = {}
    h = Decoder.get_handler_by_version_file(
        version=version, root=root, header=header, options=opts)
    return h, opts


def _handler_cfe(inner):
    opts = {}
    h = Decoder.get_handler_by_version_file(configinfo=_cfe(inner), options=opts)
    return h, opts


# --- поисковик версии платформы ---

def test_find_platform_version_flat_and_nested():
    assert _find_platform_version(['80316', '0']) == '80316'
    assert _find_platform_version(['1', '80312']) == '80312'
    assert _find_platform_version(['216', '0', ['80316', '0']]) == '80316'
    assert _find_platform_version(['1', '0']) is None
    assert _find_platform_version(['1']) is None
    assert _find_platform_version(['80316', '0'], '803') == '80316'
    assert _find_platform_version(['80212', '0'], '803') is None


# --- cfe (configinfo) ---

def test_cfe_normal_version():
    h, opts = _handler_cfe(['80316', '0'])
    assert isinstance(h, ConfigurationExtension)
    assert opts['obj_version'] == '80316'


def test_cfe_compat_flag_with_real_version():
    """{1,80312}: флаг совместимости + реальная версия рядом → 8.3, не ошибка."""
    h, opts = _handler_cfe(['1', '80312'])
    assert isinstance(h, ConfigurationExtension)
    assert opts['obj_version'] == '80312'


def test_cfe_compat_flag_only_defaults_803():
    """{1,0}: версии платформы нет — расширения всегда 8.3, берём базовый '803'."""
    h, opts = _handler_cfe(['1', '0'])
    assert isinstance(h, ConfigurationExtension)
    assert opts['obj_version'] == '803'


def test_cfe_single_flag_defaults_803():
    h, opts = _handler_cfe(['1'])
    assert isinstance(h, ConfigurationExtension)
    assert opts['obj_version'] == '803'


def test_cfe_malformed_configinfo_raises():
    with pytest.raises(Exception, match='Не удалось определить парсер'):
        Decoder.get_handler_by_version_file(
            configinfo=[['0', ['не_число']]], options={})


# --- cf (version/root) ---

def test_cf_normal_version():
    h, opts = _handler_cf(['80326', '0'])
    assert isinstance(h, Configuration)
    assert opts['obj_version'] == '803'


def test_cf_compat_flag_with_real_version_is_803():
    """{1,80312}: режим совместимости, реальная версия 8.3 — НЕ маппить в 8.2."""
    h, opts = _handler_cf(['1', '80312'])
    assert isinstance(h, Configuration)
    assert opts['obj_version'] == '803'


def test_cf_legacy_flag_1_is_802():
    h, opts = _handler_cf(['1', '0'])
    assert isinstance(h, Configuration)
    assert opts['obj_version'] == '802'


def test_cf_legacy_flag_2_is_802():
    h, opts = _handler_cf(['2', '0'])
    assert isinstance(h, Configuration)
    assert opts['obj_version'] == '802'


def test_cf_802_version_number():
    h, opts = _handler_cf(['80212', '0'])
    assert isinstance(h, Configuration)
    assert opts['obj_version'] == '802'