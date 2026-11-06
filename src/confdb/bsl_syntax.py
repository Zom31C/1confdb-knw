"""Синтаксический анализатор языка 1С (BSL) — проверка синтаксиса без платформы.

Собственная реализация на чистом Python (stdlib): лексер + рекурсивный парсер
с восстановлением после ошибок. Чек-лист грамматики — ANTLR-грамматики
BSLParser.g4/BSLLexer.g4 проекта 1c-syntax/bsl-parser (LGPL-3.0); код здесь
написан с нуля, а не перенесён из Java (см. NOTICE.md).

Что проверяется (релиз 1 — только синтаксис):
- структура модуля: объявления Перем, процедур/функций (включая Асинх),
  параметры (Знач, значения по умолчанию, аннотации), Экспорт;
- операторы: присваивание, вызовы, Если/ИначеЕсли/Иначе, Для, Для Каждого,
  Пока, Попытка/Исключение, Возврат, Прервать/Продолжить, ВызватьИсключение,
  Выполнить, Перейти ~Метка и метки ~Метка:, Добавить/УдалитьОбработчик,
  Ждать (внутри Асинх-методов);
- выражения: троичный оператор ?(,,), Новый Тип / Новый(...), строки
  (многострочные через |, склейка литералов), даты 'YYYYMMDD', числа,
  Истина/Ложь/Неопределено/NULL, цепочки обращений через точку и [],
  незакрытые строки и скобки;
- препроцессор: баланс #Если/#ИначеЕсли/#Иначе/#КонецЕсли и
  #Область/#КонецОбласти, корректность выражений #Если ... Тогда, блоки
  #Удаление/#КонецУдаления (содержимое не разбирается, как в BSL LS),
  #Вставка/#КонецВставки, #Использовать, shebang;
- &-директивы компиляции (&НаКлиенте и т.п.) и аннотации расширений
  (&Перед("..."), &После, &Вместо, &ИзменениеИКонтроль, произвольные);
- недопустимые символы (InvalidCharacterInFile).

Осознанные послабления (как в грамматике bsl-parser — она обслуживает редактор
и терпит незавершённый код; цель — ноль ложных срабатываний на живых
конфигурациях):
- точка в конце строки (Х.␤) и «недописанное» обращение Х. — не ошибка
  (incompleteAccess в грамматике); после точки, перенесённой на следующую
  строку, любое слово кроме открывающих/закрывающих оператор считается
  продолжением обращения (как DOT_MODE в BSLLexer);
- ключевое слово сразу после точки (Объект.Если) — имя члена, не оператор;
- Ждать — ключевое слово только внутри Асинх-метода, снаружи — обычный
  идентификатор (как ASYNC_MODE в BSLLexer);
- дата 'YYYYMMDD без закрывающей кавычки лексически допустима (TODO самой
  грамматики), незакрытая строка — ошибка;
- пробелы, неразрывные пробелы (\\xa0), BOM и нулевой ширины символы между
  токенами игнорируются (в BSLLexer они попадают в скрытый канал UNKNOWN);
- вызов-цепочка без результата как отдельный оператор (Х.Св;) не ругается —
  голый идентификатор без вызова/присваивания (Х;) — ошибка;
- Перем внутри метода допускается в любом месте блока (не только в начале).

Семантические проверки (области видимости, разрешения имён по базе знаний,
тексты запросов) — следующие итерации поверх собираемого здесь разбора
(ParseResult.methods/module_vars/events); они НЕ входят в релиз 1.

API:
    check_text(text) -> список Diagnostic (только ошибки синтаксиса);
    analyze(text) -> ParseResult с диагностикой, объявлениями и событиями;
    lex(text) -> (список Token, список Diagnostic) — для тестов/отладки.

Координаты в диагностике — 1-нумерация (строка, колонка), как в method.line_start.
"""
import re

__all__ = ['Diagnostic', 'ParseResult', 'Token', 'analyze', 'check_text', 'lex',
           'MAX_DIAGNOSTICS']

# ---------------------------------------------------------------------------
# Виды токенов
# ---------------------------------------------------------------------------
IDENT = 'IDENT'
NUM = 'NUM'
STR = 'STR'
STR_START = 'STR_START'      # открывающая " без закрытия на этой строке
STR_PART = 'STR_PART'        # |продолжение без закрытия
STR_TAIL = 'STR_TAIL'        # |продолжение" с закрытием
BAR = 'BAR'                  # одиночный |
DATE = 'DATE'                # '20240101'
PROC = 'PROC'
FUNC = 'FUNC'
ENDPROC = 'ENDPROC'
ENDFUNC = 'ENDFUNC'
EXPORT = 'EXPORT'
VAL = 'VAL'
IF = 'IF'
ELSIF = 'ELSIF'
ELSE = 'ELSE'
ENDIF = 'ENDIF'
THEN = 'THEN'
WHILE = 'WHILE'
DO = 'DO'
ENDDO = 'ENDDO'
FOR = 'FOR'
TO = 'TO'
EACH = 'EACH'
IN = 'IN'
TRY = 'TRY'
EXCEPT = 'EXCEPT'
ENDTRY = 'ENDTRY'
RETURN = 'RETURN'
CONTINUE = 'CONTINUE'
BREAK = 'BREAK'
RAISE = 'RAISE'
VAR = 'VAR'
NOT = 'NOT'
OR = 'OR'
AND = 'AND'
NEW = 'NEW'
GOTO = 'GOTO'
EXECUTE = 'EXECUTE'
ADDHANDLER = 'ADDHANDLER'
REMOVEHANDLER = 'REMOVEHANDLER'
ASYNC = 'ASYNC'
AWAIT = 'AWAIT'
TRUE = 'TRUE'
FALSE = 'FALSE'
UNDEFINED = 'UNDEFINED'
NULL = 'NULL'
DOT = 'DOT'
DOT_TRAILING = 'DOT_TRAILING'
LBRACK = 'LBRACK'
RBRACK = 'RBRACK'
LPAREN = 'LPAREN'
RPAREN = 'RPAREN'
COLON = 'COLON'
SEMI = 'SEMI'
COMMA = 'COMMA'
ASSIGN = 'ASSIGN'
PLUS = 'PLUS'
MINUS = 'MINUS'
LE = 'LE'
NE = 'NE'
LT = 'LT'
GE = 'GE'
GT = 'GT'
MUL = 'MUL'
DIV = 'DIV'
MOD = 'MOD'
QUESTION = 'QUESTION'
TILDA = 'TILDA'
AMP = 'AMP'
DIRECTIVE = 'DIRECTIVE'      # &НаКлиенте и т.п. (директива компиляции)
ANNOTATION = 'ANNOTATION'    # &Перед/&После/&Вместо/&ИзменениеИКонтроль/своя
SHEBANG = 'SHEBANG'          # #!... (OneScript)
# токены препроцессора (до фильтрации парсер их не видит)
PP_IF = 'PP_IF'
PP_THEN = 'PP_THEN'
PP_ELSIF = 'PP_ELSIF'
PP_ELSE = 'PP_ELSE'
PP_ENDIF = 'PP_ENDIF'
PP_AND = 'PP_AND'
PP_OR = 'PP_OR'
PP_NOT = 'PP_NOT'
PP_LPAREN = 'PP_LPAREN'
PP_RPAREN = 'PP_RPAREN'
PP_SYMBOL = 'PP_SYMBOL'
PP_ID = 'PP_ID'
PP_REGION = 'PP_REGION'
PP_ENDREGION = 'PP_ENDREGION'
PP_USE = 'PP_USE'
PP_STRING = 'PP_STRING'
PP_NATIVE = 'PP_NATIVE'
PP_STACK = 'PP_STACK'
PP_OTHER = 'PP_OTHER'
EOF = 'EOF'

KEYWORDS = {
    'ПРОЦЕДУРА': PROC, 'PROCEDURE': PROC,
    'ФУНКЦИЯ': FUNC, 'FUNCTION': FUNC,
    'КОНЕЦПРОЦЕДУРЫ': ENDPROC, 'ENDPROCEDURE': ENDPROC,
    'КОНЕЦФУНКЦИИ': ENDFUNC, 'ENDFUNCTION': ENDFUNC,
    'ЭКСПОРТ': EXPORT, 'EXPORT': EXPORT,
    'ЗНАЧ': VAL, 'VAL': VAL,
    'КОНЕЦЕСЛИ': ENDIF, 'ENDIF': ENDIF,
    'КОНЕЦЦИКЛА': ENDDO, 'ENDDO': ENDDO,
    'ЕСЛИ': IF, 'IF': IF,
    'ИНАЧЕЕСЛИ': ELSIF, 'ELSIF': ELSIF,
    'ИНАЧЕ': ELSE, 'ELSE': ELSE,
    'ТОГДА': THEN, 'THEN': THEN,
    'ПОКА': WHILE, 'WHILE': WHILE,
    'ЦИКЛ': DO, 'DO': DO,
    'ДЛЯ': FOR, 'FOR': FOR,
    'ПО': TO, 'TO': TO,
    'КАЖДОГО': EACH, 'EACH': EACH,
    'ИЗ': IN, 'IN': IN,
    'ПОПЫТКА': TRY, 'TRY': TRY,
    'ИСКЛЮЧЕНИЕ': EXCEPT, 'EXCEPT': EXCEPT,
    'КОНЕЦПОПЫТКИ': ENDTRY, 'ENDTRY': ENDTRY,
    'ВОЗВРАТ': RETURN, 'RETURN': RETURN,
    'ПРОДОЛЖИТЬ': CONTINUE, 'CONTINUE': CONTINUE,
    'ПРЕРВАТЬ': BREAK, 'BREAK': BREAK,
    'ВЫЗВАТЬИСКЛЮЧЕНИЕ': RAISE, 'RAISE': RAISE,
    'ПЕРЕМ': VAR, 'VAR': VAR,
    'НЕ': NOT, 'NOT': NOT,
    'ИЛИ': OR, 'OR': OR,
    'И': AND, 'AND': AND,
    'НОВЫЙ': NEW, 'NEW': NEW,
    'ПЕРЕЙТИ': GOTO, 'GOTO': GOTO,
    'ВЫПОЛНИТЬ': EXECUTE, 'EXECUTE': EXECUTE,
    'ДОБАВИТЬОБРАБОТЧИК': ADDHANDLER, 'ADDHANDLER': ADDHANDLER,
    'УДАЛИТЬОБРАБОТЧИК': REMOVEHANDLER, 'REMOVEHANDLER': REMOVEHANDLER,
    'АСИНХ': ASYNC, 'ASYNC': ASYNC,
    'ИСТИНА': TRUE, 'TRUE': TRUE,
    'ЛОЖЬ': FALSE, 'FALSE': FALSE,
    'НЕОПРЕДЕЛЕНО': UNDEFINED, 'UNDEFINED': UNDEFINED,
    'NULL': NULL,
}

# Ждать/Await — ключевое слово только внутри Асинх-метода (ASYNC_MODE лексера)
AWAIT_WORDS = ('ЖДАТЬ', 'AWAIT')

# &-директивы компиляции (прочие слова после & — аннотации, в т.ч. произвольные)
DIRECTIVE_WORDS = frozenset({
    'НАСЕРВЕРЕБЕЗКОНТЕКСТА', 'ATSERVERNOCONTEXT',
    'НАКЛИЕНТЕНАСЕРВЕРЕБЕЗКОНТЕКСТА', 'ATCLIENTATSERVERNOCONTEXT',
    'НАКЛИЕНТЕНАСЕРВЕРЕ', 'ATCLIENTATSERVER',
    'НАКЛИЕНТЕ', 'ATCLIENT',
    'НАСЕРВЕРЕ', 'ATSERVER',
})

# Слова, с которых продолжение обращения после точки+перевод строки начаться
# не может (NOT_CONTINUING_WORDS из BSLLexer): перед ними точка в конце
# строки — недописанное обращение.
NOT_CONTINUING = frozenset({
    'ЕСЛИ', 'ИНАЧЕЕСЛИ', 'ИНАЧЕ', 'КОНЕЦЕСЛИ', 'ТОГДА',
    'ДЛЯ', 'КАЖДОГО', 'ПОКА', 'ЦИКЛ', 'КОНЕЦЦИКЛА',
    'ПОПЫТКА', 'ИСКЛЮЧЕНИЕ', 'КОНЕЦПОПЫТКИ',
    'ВОЗВРАТ', 'ПРОДОЛЖИТЬ', 'ПРЕРВАТЬ', 'ВЫЗВАТЬИСКЛЮЧЕНИЕ', 'ПЕРЕЙТИ',
    'ПЕРЕМ', 'ПРОЦЕДУРА', 'ФУНКЦИЯ', 'КОНЕЦПРОЦЕДУРЫ', 'КОНЕЦФУНКЦИИ',
    'И', 'ИЛИ', 'НЕ', 'АСИНХ', 'ЖДАТЬ',
    'IF', 'ELSIF', 'ELSE', 'ENDIF', 'THEN',
    'FOR', 'EACH', 'WHILE', 'DO', 'ENDDO',
    'TRY', 'EXCEPT', 'ENDTRY',
    'RETURN', 'CONTINUE', 'BREAK', 'RAISE', 'GOTO',
    'VAR', 'PROCEDURE', 'FUNCTION', 'ENDPROCEDURE', 'ENDFUNCTION',
    'AND', 'OR', 'NOT', 'ASYNC', 'AWAIT',
})

# Слова препроцессора после #
PP_WORDS = {
    'ЕСЛИ': PP_IF, 'IF': PP_IF,
    'ТОГДА': PP_THEN, 'THEN': PP_THEN,
    'ИНАЧЕЕСЛИ': PP_ELSIF, 'ELSIF': PP_ELSIF,
    'ИНАЧЕ': PP_ELSE, 'ELSE': PP_ELSE,
    'КОНЕЦЕСЛИ': PP_ENDIF, 'ENDIF': PP_ENDIF,
    'И': PP_AND, 'AND': PP_AND,
    'ИЛИ': PP_OR, 'OR': PP_OR,
    'НЕ': PP_NOT, 'NOT': PP_NOT,
    'ОБЛАСТЬ': PP_REGION, 'REGION': PP_REGION,
    'КОНЕЦОБЛАСТИ': PP_ENDREGION, 'ENDREGION': PP_ENDREGION,
    'ИСПОЛЬЗОВАТЬ': PP_USE, 'USE': PP_USE,
    'NATIVE': PP_NATIVE, 'STACK': PP_STACK,
}

# Символы препроцессора (остальные слова — PP_ID, «неизвестный символ»:
# грамматика допускает его операндом #Если)
PP_SYMBOLS = frozenset({
    'КЛИЕНТ', 'CLIENT', 'НАКЛИЕНТЕ', 'ATCLIENT',
    'СЕРВЕР', 'SERVER', 'НАСЕРВЕРЕ', 'ATSERVER',
    'ВЕБКЛИЕНТ', 'WEBCLIENT', 'ТОНКИЙКЛИЕНТ', 'THINCLIENT',
    'ТОЛСТЫЙКЛИЕНТОБЫЧНОЕПРИЛОЖЕНИЕ', 'THICKCLIENTORDINARYAPPLICATION',
    'ТОЛСТЫЙКЛИЕНТУПРАВЛЯЕМОЕПРИЛОЖЕНИЕ', 'THICKCLIENTMANAGEDAPPLICATION',
    'ВНЕШНЕЕСОЕДИНЕНИЕ', 'EXTERNALCONNECTION',
    'МОБИЛЬНЫЙКЛИЕНТ', 'MOBILECLIENT',
    'МОБИЛЬНОЕПРИЛОЖЕНИЕКЛИЕНТ', 'MOBILEAPPCLIENT',
    'МОБИЛЬНОЕПРИЛОЖЕНИЕСЕРВЕР', 'MOBILEAPPSERVER',
    'МОБИЛЬНЫЙАВТОНОМНЫЙСЕРВЕР', 'MOBILESTANDALONESERVER',
    'LINUX', 'WINDOWS', 'MACOS',
})

# Операторы выражения (плоский список — в грамматике BSL нет приоритетов:
# expression: member (operation member)*)
OPERATIONS = frozenset({PLUS, MINUS, MUL, DIV, MOD, LT, LE, GT, GE,
                        ASSIGN, NE, OR, AND})

# Токены, на которых блок кода останавливается (границы конструкций и методы)
BLOCK_STOP = frozenset({ENDIF, ELSIF, ELSE, ENDDO, EXCEPT, ENDTRY,
                        ENDPROC, ENDFUNC, PROC, FUNC})

# С чего может начинаться выражение
EXPR_START = frozenset({IDENT, NUM, STR, STR_START, DATE, TRUE, FALSE,
                        UNDEFINED, NULL, NEW, QUESTION, LPAREN, NOT, MINUS,
                        PLUS, AWAIT})

# Значения по умолчанию параметра — только константы (constValue в грамматике)
CONST_START = frozenset({NUM, STR, STR_START, DATE, TRUE, FALSE, UNDEFINED,
                         NULL, PLUS, MINUS})

# Невидимые символы, которые BSLLexer прячет в канал UNKNOWN
INVISIBLE = frozenset('\ufeff\u200b\u200c\u200d\u2060')

MAX_DIAGNOSTICS = 100

_RE_END_DELETE = re.compile(r'#[ \t]*(?:КОНЕЦУДАЛЕНИЯ|ENDDELETE)', re.IGNORECASE)


class Token:
    __slots__ = ('kind', 'text', 'line', 'col', 'off', 'end')

    def __init__(self, kind, text, line, col, off, end):
        self.kind = kind
        self.text = text
        self.line = line
        self.col = col
        self.off = off
        self.end = end

    def __repr__(self):
        return 'Token(%s, %r, %d:%d)' % (self.kind, self.text, self.line,
                                         self.col)


class Diagnostic:
    """Одно замечание: код (имя диагностики BSL LS), сообщение, позиция."""

    __slots__ = ('code', 'message', 'line', 'col', 'line_end', 'col_end',
                 'severity')

    def __init__(self, code, message, line, col, line_end=None, col_end=None,
                 severity='ERROR'):
        self.code = code
        self.message = message
        self.line = line
        self.col = col
        self.line_end = line_end if line_end is not None else line
        self.col_end = col_end if col_end is not None else col
        self.severity = severity

    def as_tuple(self):
        return (self.code, self.message, self.line, self.col, self.line_end,
                self.col_end, self.severity)

    def __repr__(self):
        return 'Diagnostic(%s, %r, %d:%d)' % (self.code, self.message,
                                              self.line, self.col)


class ParseResult:
    """Итог разбора: диагностика + объявления + события для семантики."""

    __slots__ = ('diags', 'methods', 'module_vars', 'events', 'truncated')

    def __init__(self):
        self.diags = []
        self.methods = []
        self.module_vars = []
        self.events = []
        self.truncated = False


def _is_ident_start(c):
    return c == '_' or c.isalpha()


def _is_ident_part(c):
    return c == '_' or c.isalnum()


def _is_digit(c):
    return '0' <= c <= '9'


# ---------------------------------------------------------------------------
# Лексер
# ---------------------------------------------------------------------------

class Lexer:
    """Один проход по тексту; повторяет режимы BSLLexer (default, после '.',
    после '&', после '~', после '#', Асинх-режим для Ждать)."""

    def __init__(self, text):
        self.text = text
        self.n = len(text)
        self.i = 0
        self.line = 1
        self.line_start = 0
        self.in_async = False      # ASYNC_MODE: Ждать — ключевое слово
        self.member_mode = False   # DOT_MODE: следующее слово — IDENT
        self.tokens = []
        self.diags = []

    # -- вспомогательное -----------------------------------------------------

    def _emit(self, kind, off, end):
        t = Token(kind, self.text[off:end], self.line, off - self.line_start + 1,
                  off, end)
        self.tokens.append(t)
        return t

    def _error(self, message, off, end=None):
        if end is None:
            end = off + 1
        line = self.line
        col = off - self.line_start + 1
        self.diags.append(Diagnostic('InvalidCharacterInFile', message,
                                     line, col))

    def _skip_newline_track(self, i, end):
        """Обновляет line/line_start после пропуска фрагмента i..end."""
        cnt = self.text.count('\n', i, end)
        if cnt:
            self.line += cnt
            self.line_start = self.text.rfind('\n', i, end) + 1

    def _read_word(self, j):
        """(конец слова) начиная с j, где text[j] — начало идентификатора."""
        text = self.text
        n = self.n
        k = j + 1
        while k < n and _is_ident_part(text[k]):
            k += 1
        return k

    # -- основной цикл -------------------------------------------------------

    def tokenize(self):
        text = self.text
        n = self.n
        while self.i < n:
            c = text[self.i]
            if c == '\n':
                self.line += 1
                self.i += 1
                self.line_start = self.i
                continue
            if c == '\r' or c == ' ' or c == '\t' or c == '\f' or c.isspace() \
                    or c in INVISIBLE:
                self.i += 1
                continue
            if c == '/' and self.i + 1 < n and text[self.i + 1] == '/':
                nl = text.find('\n', self.i)
                self.i = n if nl < 0 else nl
                continue
            if _is_ident_start(c):
                self._lex_word()
            elif _is_digit(c):
                self._lex_number()
            elif c == '"':
                self._lex_string()
            elif c == '|':
                self._lex_bar()
            elif c == "'":
                self._lex_date()
            elif c == '.':
                self._lex_dot()
            elif c == '&':
                self._lex_amp()
            elif c == '~':
                self._lex_tilda()
            elif c == '#':
                self._lex_hash()
            else:
                self._lex_operator()
        self._emit(EOF, n, n)
        return self.tokens, self.diags

    def _lex_word(self):
        text = self.text
        off = self.i
        end = self._read_word(off)
        word = text[off:end]
        up = word.upper()
        self.i = end
        if self.member_mode:
            self.member_mode = False
            self._emit(IDENT, off, end)
            return
        kind = None
        if up in ('АСИНХ', 'ASYNC'):
            kind = ASYNC
            self.in_async = True
        elif up in ('КОНЕЦПРОЦЕДУРЫ', 'ENDPROCEDURE',
                    'КОНЕЦФУНКЦИИ', 'ENDFUNCTION'):
            kind = KEYWORDS[up]
            self.in_async = False
        elif self.in_async and up in AWAIT_WORDS:
            kind = AWAIT
        else:
            kind = KEYWORDS.get(up, IDENT)
        self._emit(kind, off, end)

    def _lex_number(self):
        text = self.text
        n = self.n
        off = self.i
        j = off
        while j < n and _is_digit(text[j]):
            j += 1
        # FLOAT: DIGIT+ '.' DIGIT* — точка поглощается всегда (как в ANTLR:
        # «1.Метод» — FLOAT(1.) + IDENT и синтаксическая ошибка разбора)
        if j < n and text[j] == '.':
            j += 1
            while j < n and _is_digit(text[j]):
                j += 1
        self.i = j
        self._emit(NUM, off, j)

    def _lex_string(self):
        text = self.text
        n = self.n
        off = self.i
        j = off + 1
        closed = False
        while j < n:
            c = text[j]
            if c == '"':
                if j + 1 < n and text[j + 1] == '"':
                    j += 2
                    continue
                j += 1
                closed = True
                break
            if c in '\r\n':
                break
            j += 1
        self.i = j
        self._emit(STR if closed else STR_START, off, j)

    def _lex_bar(self):
        text = self.text
        n = self.n
        off = self.i
        j = off + 1
        while j < n and text[j] not in '\r\n':
            if text[j] == '"':
                if j + 1 < n and text[j + 1] == '"':
                    j += 2
                    continue
                break
            j += 1
        if j < n and text[j] == '"':
            self.i = j + 1
            self._emit(STR_TAIL, off, j + 1)
        elif j > off + 1:
            self.i = j
            self._emit(STR_PART, off, j)
        else:
            self.i = off + 1
            self._emit(BAR, off, off + 1)

    def _lex_date(self):
        text = self.text
        n = self.n
        off = self.i
        j = off + 1
        while j < n and text[j] not in "'\r\n":
            j += 1
        if j < n and text[j] == "'":
            j += 1
        self.i = j
        self._emit(DATE, off, j)

    def _lex_dot(self):
        """DOT / DOT_TRAILING + DOT_MODE (onDot из BSLLexer)."""
        text = self.text
        n = self.n
        off = self.i
        j = off + 1
        # точка непосредственно перед началом идентификатора — член
        if j < n and _is_ident_start(text[j]):
            self.i = j
            self.member_mode = True
            self._emit(DOT, off, j)
            return
        # пробелы/табы в пределах строки (как isTrailingDot: \n или // дают
        # «точку в конце», прочий символ в той же строке — обычную точку)
        k = j
        while k < n and text[k] in ' \t\f':
            k += 1
        newline_case = False
        if k >= n:
            self.i = j
            self._emit(DOT_TRAILING, off, j)
            return
        if text[k] in '\r\n':
            newline_case = True
        elif text[k] == '/' and k + 1 < n and text[k + 1] == '/':
            newline_case = True
        if not newline_case:
            # символ в той же строке, не идентификатор: DOT без member-режима
            # (Х. Если ... — точка «недописанная» для выражения, дальше слово
            # лексится как обычно)
            self.i = j
            self._emit(DOT, off, j)
            return
        # isLineContinuation: за переводом строки слово, которое может быть
        # именем члена → DOT + member-режим; иначе DOT_TRAILING
        m = k
        while m < n:
            c = text[m]
            if c in ' \t\f\r\n' or c in INVISIBLE:
                m += 1
                continue
            if c == '/' and m + 1 < n and text[m + 1] == '/':
                nl = text.find('\n', m)
                m = n if nl < 0 else nl
                continue
            break
        if m < n and _is_ident_start(text[m]):
            w_end = self._read_word(m)
            word_up = text[m:w_end].upper()
            if word_up not in NOT_CONTINUING:
                self.i = j
                self.member_mode = True
                self._emit(DOT, off, j)
                return
        self.i = j
        self._emit(DOT_TRAILING, off, j)

    def _lex_amp(self):
        """& → &-директива / аннотация (ANNOTATION_MODE)."""
        text = self.text
        n = self.n
        off = self.i
        tok_line, tok_col = self.line, off - self.line_start + 1
        j = off + 1
        # пробелы (включая переводы строк — ANNOTATION_WHITE_SPACE) до слова
        while j < n:
            c = text[j]
            if c == '\n':
                j += 1
                continue
            if c.isspace() or c in INVISIBLE:
                j += 1
                continue
            break
        if j < n and _is_ident_start(text[j]):
            w_end = self._read_word(j)
            word = text[j:w_end]
            up = word.upper()
            kind = DIRECTIVE if up in DIRECTIVE_WORDS else ANNOTATION
            self._skip_newline_track(off, w_end)
            self.i = w_end
            t = self._emit(kind, off, w_end)
            t.line = tok_line
            t.col = tok_col
            # нормализованный текст: '&Имя' — как хранит method.directives
            t.text = '&' + word
            return
        self.i = off + 1
        self._emit(AMP, off, off + 1)

    def _lex_tilda(self):
        """~ → TILDA + имя (LABEL_MODE)."""
        text = self.text
        n = self.n
        off = self.i
        self.i = off + 1
        self._emit(TILDA, off, off + 1)
        j = off + 1
        while j < n and (text[j] in ' \t' or text[j] in INVISIBLE):
            j += 1
        if j < n and _is_ident_start(text[j]):
            w_end = self._read_word(j)
            self.i = w_end
            self._emit(IDENT, j, w_end)

    def _lex_hash(self):
        """# → директива препроцессора (PREPROCESSOR_MODE до конца строки)."""
        text = self.text
        n = self.n
        off = self.i
        j = off + 1
        while j < n and text[j] in ' \t':
            j += 1
        if j < n and text[j] == '!':
            # shebang: #! до конца строки
            nl = text.find('\n', j)
            end = n if nl < 0 else nl
            self.i = end
            self._emit(SHEBANG, off, end)
            return
        if j >= n or not _is_ident_start(text[j]):
            self.i = j if j > off + 1 else off + 1
            self._emit(PP_OTHER, off, self.i)
            self._pp_line_mode()
            return
        w_end = self._read_word(j)
        word_up = text[j:w_end].upper()
        if word_up in ('УДАЛЕНИЕ', 'DELETE'):
            self._skip_delete_block(off, w_end)
            return
        if word_up in ('ВСТАВКА', 'INSERT', 'КОНЕЦВСТАВКИ', 'ENDINSERT'):
            # скрытые маркеры: содержимое разбирается как обычный код
            nl = text.find('\n', w_end)
            self.i = n if nl < 0 else nl
            return
        kind = PP_WORDS.get(word_up)
        if kind is None:
            kind = PP_SYMBOL if word_up in PP_SYMBOLS else PP_ID
        self.i = w_end
        self._emit(kind, off, w_end)
        if kind == PP_REGION:
            self._pp_expect_id()
        elif kind == PP_USE:
            self._pp_expect_use()
        self._pp_line_mode()

    def _skip_delete_block(self, off, from_pos):
        """#Удаление ... #КонецУдаления: содержимое не разбирается."""
        text = self.text
        m = _RE_END_DELETE.search(text, from_pos)
        end = m.end() if m else self.n
        if m is None:
            self.diags.append(Diagnostic(
                'ParseError', 'незакрытый блок #Удаление (ожидался '
                '#КонецУдаления)', self.line, off - self.line_start + 1))
        self._skip_newline_track(off, end)
        self.i = end

    def _pp_expect_id(self):
        """Имя #Область — следующее слово как есть (REGION_MODE)."""
        text = self.text
        n = self.n
        j = self.i
        while j < n and text[j] in ' \t':
            j += 1
        if j < n and _is_ident_start(text[j]):
            w_end = self._read_word(j)
            self.i = w_end
            self._emit(PP_ID, j, w_end)

    def _pp_expect_use(self):
        """Аргумент #Использовать: строка или путь из сегментов (USE_MODE)."""
        text = self.text
        n = self.n
        j = self.i
        while j < n and text[j] in ' \t':
            j += 1
        if j < n and text[j] == '"':
            k = j + 1
            while k < n and text[k] not in '"\r\n':
                k += 1
            if k < n:
                k += 1
            self.i = k
            self._emit(PP_STRING, j, k)
            return
        if j < n and (_is_ident_part(text[j]) or text[j] == '-'):
            k = j
            while k < n and (_is_ident_part(text[k]) or text[k] in '-/'):
                k += 1
            self.i = k
            self._emit(PP_ID, j, k)

    def _pp_line_mode(self):
        """Остаток строки директивы — токены препроцессора (до перевода)."""
        text = self.text
        n = self.n
        expect_id = False
        while self.i < n:
            c = text[self.i]
            if c == '\n':
                return
            if c == '\r' or c in ' \t\f' or c.isspace() or c in INVISIBLE:
                self.i += 1
                continue
            if c == '/' and self.i + 1 < n and text[self.i + 1] == '/':
                nl = text.find('\n', self.i)
                self.i = n if nl < 0 else nl
                return
            off = self.i
            if _is_ident_start(c) or (expect_id and _is_ident_part(c)):
                w_end = self._read_word(off)
                if expect_id:
                    # продолжение пути #Использовать (сегменты через /)
                    k = w_end
                    while k < n and text[k] == '/':
                        k2 = k + 1
                        while k2 < n and (_is_ident_part(text[k2])
                                          or text[k2] == '-'):
                            k2 += 1
                        if k2 == k + 1:
                            break
                        k = k2
                    w_end = k
                word_up = text[off:w_end].upper()
                self.i = w_end
                if expect_id:
                    self._emit(PP_ID, off, w_end)
                    expect_id = False
                    continue
                kind = PP_WORDS.get(word_up)
                if kind is None:
                    kind = PP_SYMBOL if word_up in PP_SYMBOLS else PP_ID
                self._emit(kind, off, w_end)
                if kind == PP_REGION:
                    expect_id = True
                elif kind == PP_USE:
                    self._pp_expect_use()
                continue
            if c == '(':
                self.i = off + 1
                self._emit(PP_LPAREN, off, self.i)
                continue
            if c == ')':
                self.i = off + 1
                self._emit(PP_RPAREN, off, self.i)
                continue
            if c == '"':
                k = off + 1
                while k < n and text[k] not in '"\r\n':
                    k += 1
                if k < n:
                    k += 1
                self.i = k
                self._emit(PP_STRING, off, k)
                continue
            self.i = off + 1
            self._emit(PP_OTHER, off, self.i)

    def _lex_operator(self):
        text = self.text
        n = self.n
        off = self.i
        c = text[off]
        two = text[off:off + 2]
        kind = None
        size = 1
        if two == '<=':
            kind = LE
            size = 2
        elif two == '<>':
            kind = NE
            size = 2
        elif two == '>=':
            kind = GE
            size = 2
        elif c == '<':
            kind = LT
        elif c == '>':
            kind = GT
        elif c == '+':
            kind = PLUS
        elif c == '-':
            kind = MINUS
        elif c == '*':
            kind = MUL
        elif c == '/':
            kind = DIV
        elif c == '%':
            kind = MOD
        elif c == '(':
            kind = LPAREN
        elif c == ')':
            kind = RPAREN
        elif c == '[':
            kind = LBRACK
        elif c == ']':
            kind = RBRACK
        elif c == ',':
            kind = COMMA
        elif c == ';':
            kind = SEMI
        elif c == '=':
            kind = ASSIGN
        elif c == '?':
            kind = QUESTION
        elif c == ':':
            kind = COLON
        if kind is None:
            self.i = off + 1
            self._error('недопустимый символ %r' % c, off)
            return
        self.i = off + size
        self._emit(kind, off, off + size)


def lex(text):
    """(токены, лексические диагностики). Токены НЕ фильтрованы: содержат
    PP_* и SHEBANG — их убирает validate_preprocessor."""
    return Lexer(text).tokenize()


# ---------------------------------------------------------------------------
# Препроцессор: балансировка и проверка директив, фильтрация токенов
# ---------------------------------------------------------------------------

_PP_KINDS = frozenset({PP_IF, PP_THEN, PP_ELSIF, PP_ELSE, PP_ENDIF, PP_AND,
                       PP_OR, PP_NOT, PP_LPAREN, PP_RPAREN, PP_SYMBOL, PP_ID,
                       PP_REGION, PP_ENDREGION, PP_USE, PP_STRING, PP_NATIVE,
                       PP_STACK, PP_OTHER, SHEBANG})

# операнды выражения #Если ... Тогда
_PP_OPERANDS = frozenset({PP_SYMBOL, PP_ID, PP_NOT, PP_AND, PP_OR,
                          PP_LPAREN, PP_RPAREN})


def validate_preprocessor(tokens):
    """Проверяет директивы препроцессора и возвращает (фильтрованные токены,
    диагностики). Баланс #Если/#КонецЕсли и #Область/#КонецОбласти — отдельные
    стеки (пересечение областей и #Если допустимо, как в живых конфигурациях).
    """
    diags = []
    if_stack = []    # элементы: [line, col, saw_else]
    region_stack = []
    n = len(tokens)
    i = 0

    def err(msg, t):
        diags.append(Diagnostic('ParseError', msg, t.line, t.col))

    while i < n:
        t = tokens[i]
        k = t.kind
        if k in (PP_IF, PP_ELSIF):
            # собрать директиву до конца строки (токены одной строки)
            j = i + 1
            while j < n and tokens[j].line == t.line and tokens[j].kind in _PP_KINDS:
                j += 1
            body = tokens[i + 1:j]
            ok = True
            depth = 0
            for bt in body:
                if bt.kind == PP_LPAREN:
                    depth += 1
                elif bt.kind == PP_RPAREN:
                    depth -= 1
                elif bt.kind not in _PP_OPERANDS and bt.kind != PP_THEN:
                    ok = False
            if depth != 0:
                ok = False
            if len(body) < 2 or body[-1].kind != PP_THEN:
                err('директива #%s должна заканчиваться «Тогда»'
                    % ('Если' if k == PP_IF else 'ИначеЕсли'), t)
                ok = False
            elif not ok:
                err('некорректное выражение директивы #%s'
                    % ('Если' if k == PP_IF else 'ИначеЕсли'), t)
            if k == PP_IF:
                if_stack.append([t.line, t.col, False])
            else:
                if not if_stack:
                    err('#ИначеЕсли без #Если', t)
                elif if_stack[-1][2]:
                    err('#ИначеЕсли после #Иначе', t)
            i = j
            continue
        if k == PP_ELSE:
            if not if_stack:
                err('#Иначе без #Если', t)
            elif if_stack[-1][2]:
                err('повторное #Иначе', t)
            else:
                if_stack[-1][2] = True
            i += 1
            continue
        if k == PP_ENDIF:
            if not if_stack:
                err('#КонецЕсли без #Если', t)
            else:
                if_stack.pop()
            i += 1
            continue
        if k == PP_REGION:
            if i + 1 < n and tokens[i + 1].kind == PP_ID \
                    and tokens[i + 1].line == t.line:
                i += 1
            else:
                err('#Область без имени', t)
            region_stack.append((t.line, t.col))
            i += 1
            continue
        if k == PP_ENDREGION:
            if not region_stack:
                err('#КонецОбласти без #Область', t)
            else:
                region_stack.pop()
            i += 1
            continue
        if k == PP_USE:
            if i + 1 < n and tokens[i + 1].kind in (PP_STRING, PP_ID) \
                    and tokens[i + 1].line == t.line:
                i += 1
            else:
                err('#Использовать без аргумента', t)
            i += 1
            continue
        if k in (PP_NATIVE, PP_STACK):
            i += 1
            continue
        if k == PP_OTHER:
            err('неизвестная конструкция препроцессора: %s' % t.text.strip(), t)
            i += 1
            continue
        if k == SHEBANG:
            i += 1
            continue
        # PP_THEN вне #Если, PP_SYMBOL/PP_ID/строки/скобки сами по себе
        if k in (PP_SYMBOL, PP_ID, PP_STRING, PP_LPAREN, PP_RPAREN, PP_THEN):
            err('неизвестная директива препроцессора', t)
            i += 1
            continue
        i += 1
    for line, col in region_stack:
        diags.append(Diagnostic('ParseError',
                                'незакрытая #Область (ожидался #КонецОбласти)',
                                line, col))
    for line, col, _ in if_stack:
        diags.append(Diagnostic('ParseError',
                                'незакрытое #Если (ожидался #КонецЕсли)',
                                line, col))
    filtered = [t for t in tokens if t.kind not in _PP_KINDS]
    return filtered, diags


# ---------------------------------------------------------------------------
# Парсер
# ---------------------------------------------------------------------------

class Parser:
    """Рекурсивный спуск по грамматике BSLParser.g4 (плоские выражения,
    восстановление пропуском строки). Собирает объявления и события для
    будущих семантических проверок."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0
        self.loop_depth = 0
        self.result = ParseResult()
        self.sub_locals = None     # список локальных Перем текущего метода

    # -- базовое --------------------------------------------------------------

    def peek(self, ahead=0):
        i = self.pos + ahead
        if i >= len(self.tokens):
            return self.tokens[-1]
        return self.tokens[i]

    def error(self, message, tok=None, code='ParseError'):
        if tok is None:
            tok = self.peek()
        diags = self.result.diags
        if len(diags) >= MAX_DIAGNOSTICS:
            self.result.truncated = True
            return
        # дедупликация: одна позиция — одна диагностика (каскады expect)
        if diags and diags[-1].line == tok.line and diags[-1].col == tok.col \
                and diags[-1].code == code:
            return
        diags.append(Diagnostic(code, message, tok.line, tok.col))

    def expect(self, kind, what):
        t = self.peek()
        if t.kind == kind:
            self.pos += 1
            return t
        self.error('ожидалось %s, найдено %s' % (what, self._name(t)), t)
        return None

    @staticmethod
    def _name(t):
        if t.kind == EOF:
            return 'конец файла'
        return '«%s»' % t.text

    # -- верхний уровень -------------------------------------------------------

    def run(self):
        while True:
            if self.result.truncated:
                break
            t = self.peek()
            k = t.kind
            if k == EOF:
                break
            if k in (PROC, FUNC):
                self.sub(False, [], [])
            elif k == ASYNC:
                self.pos += 1
                self.sub(True, [], [])
            elif k == VAR:
                self.var_decl(True)
            elif k in (DIRECTIVE, ANNOTATION):
                self.declaration_with_decorators()
            elif k == SEMI:
                self.pos += 1
            elif k in (ENDPROC, ENDFUNC):
                self.error('«%s» без процедуры/функции' % t.text, t)
                self.pos += 1
            elif k in (ENDIF, ELSIF, ELSE, ENDDO, EXCEPT, ENDTRY):
                self.error('«%s» вне блока' % t.text, t)
                self.pos += 1
            else:
                before = self.pos
                self.statement()
                if self.pos == before:
                    self._stuck(t)
        return self.result

    def _stuck(self, t):
        """Нет разбора оператора: сообщение и пропуск до следующей строки."""
        self.error('не удалось разобрать оператор', t)
        self.pos += 1
        while self.peek().kind != EOF and self.peek().line <= t.line:
            self.pos += 1

    def declaration_with_decorators(self):
        dirs = []
        anns = []
        while self.peek().kind in (DIRECTIVE, ANNOTATION):
            t = self.peek()
            self.pos += 1
            if t.kind == DIRECTIVE:
                dirs.append(t.text)
            else:
                anns.append(t.text)
                if self.peek().kind == LPAREN:
                    self._skip_balanced_parens()
        t = self.peek()
        if t.kind == VAR:
            self.var_decl(True)
        elif t.kind in (PROC, FUNC):
            self.sub(False, dirs, anns)
        elif t.kind == ASYNC:
            self.pos += 1
            self.sub(True, dirs, anns)
        else:
            self.error('после &-директивы/аннотации ожидается объявление '
                       'метода или переменной', t)

    def _skip_balanced_parens(self):
        """Пропуск (...) — параметры аннотации разбираются облегчённо."""
        self.pos += 1  # (
        depth = 1
        while depth and self.peek().kind != EOF:
            t = self.peek()
            self.pos += 1
            if t.kind == LPAREN:
                depth += 1
            elif t.kind == RPAREN:
                depth -= 1

    # -- объявления -------------------------------------------------------------

    def sub(self, is_async, dirs, anns):
        t = self.peek()
        if t.kind not in (PROC, FUNC):
            self.error('после «Асинх» ожидалось «Процедура» или «Функция»', t)
            return
        self.pos += 1
        kind = 'процедура' if t.kind == PROC else 'функция'
        name_tok = self.peek()
        if name_tok.kind != IDENT:
            self.error('ожидалось имя метода, найдено %s' % self._name(name_tok),
                       name_tok)
            self._resync_sub()
            return
        self.pos += 1
        params = []
        if self.peek().kind == LPAREN:
            self.pos += 1
            params = self.param_list()
        else:
            self.error('ожидалось «(» после имени метода', self.peek())
            self._resync_sub()
        is_export = False
        if self.peek().kind == EXPORT:
            self.pos += 1
            is_export = True
        if self.peek().kind == SEMI:   # послабление: ';' после заголовка
            self.pos += 1
        self.sub_locals = []
        self.code_block()
        end_line = self.peek().line
        closer = ENDPROC if t.kind == PROC else ENDFUNC
        closer_name = 'КонецПроцедуры' if t.kind == PROC else 'КонецФункции'
        p = self.peek()
        if p.kind == closer:
            self.pos += 1
            end_line = p.line
        elif p.kind in (ENDPROC, ENDFUNC):
            self.error('ожидался «%s», найден «%s»' % (closer_name, p.text), p)
            self.pos += 1
            end_line = p.line
        else:
            self.error('ожидался «%s»' % closer_name, p)
        locals_ = list(self.sub_locals or [])
        self.sub_locals = None
        self.result.methods.append({
            'name': name_tok.text,
            'kind': kind,
            'is_export': is_export,
            'is_async': is_async,
            'params': params,
            'directives': dirs,
            'annotations': anns,
            'line_start': t.line,
            'line_end': end_line,
            'locals': locals_,
        })

    def _resync_sub(self):
        """Восстановление внутри заголовка метода: до ) или границы метода."""
        while self.peek().kind not in (RPAREN, ENDPROC, ENDFUNC, PROC, FUNC,
                                       EOF):
            self.pos += 1
        if self.peek().kind == RPAREN:
            self.pos += 1

    def param_list(self):
        params = []
        if self.peek().kind == RPAREN:      # пустой список ()
            self.pos += 1
            return params
        while True:
            t = self.peek()
            if t.kind == EOF or t.kind in BLOCK_STOP:
                self.error('ожидалось «)» в списке параметров', t)
                return params
            while self.peek().kind in (DIRECTIVE, ANNOTATION):
                self.pos += 1
                if self.peek().kind == LPAREN:
                    self._skip_balanced_parens()
            by_value = False
            if self.peek().kind == VAL:
                self.pos += 1
                by_value = True
            nt = self.peek()
            if nt.kind != IDENT:
                self.error('ожидалось имя параметра, найдено %s'
                           % self._name(nt), nt)
                if nt.kind == RPAREN:
                    self.pos += 1
                elif nt.kind != EOF and nt.kind not in BLOCK_STOP:
                    while self.peek().kind not in (COMMA, RPAREN, EOF) \
                            and self.peek().kind not in BLOCK_STOP:
                        self.pos += 1
                    if self.peek().kind == COMMA:
                        self.pos += 1
                        continue
                    if self.peek().kind == RPAREN:
                        self.pos += 1
                return params
            self.pos += 1
            default = None
            if self.peek().kind == ASSIGN:
                self.pos += 1
                default = self.const_value()
            params.append({'name': nt.text, 'by_value': by_value,
                           'default': default})
            t2 = self.peek()
            if t2.kind == COMMA:
                self.pos += 1
                continue
            if t2.kind == RPAREN:
                self.pos += 1
                return params
            self.error('ожидалось «,» или «)» в списке параметров', t2)
            if t2.kind == EOF or t2.kind in BLOCK_STOP:
                return params
            self.pos += 1

    def const_value(self):
        """Значение по умолчанию параметра: только константа (constValue)."""
        t = self.peek()
        if t.kind in (PLUS, MINUS):
            self.pos += 1
            t = self.peek()
        if t.kind in CONST_START:
            if t.kind == STR_START:
                self.string_literal()
                return '…'
            self.pos += 1
            return t.text
        self.error('значение по умолчанию должно быть константой (число, '
                   'строка, дата, Истина/Ложь/Неопределено/NULL)', t)
        if t.kind in EXPR_START:
            self.expression()
        else:
            self.pos += 1
        return None

    def var_decl(self, is_module):
        self.pos += 1  # Перем
        while True:
            nt = self.peek()
            if nt.kind != IDENT:
                self.error('ожидалось имя переменной, найдено %s'
                           % self._name(nt), nt)
                break
            self.pos += 1
            is_export = False
            if self.peek().kind == EXPORT:
                self.pos += 1
                is_export = True
            if is_module:
                self.result.module_vars.append(
                    {'name': nt.text, 'is_export': is_export, 'line': nt.line})
            elif self.sub_locals is not None:
                self.sub_locals.append(nt.text)
                self.result.events.append(('localvar', nt.text, nt.line))
            if self.peek().kind == COMMA:
                self.pos += 1
                continue
            break
        if self.peek().kind == SEMI:
            self.pos += 1

    # -- блоки и операторы ------------------------------------------------------

    def code_block(self):
        while True:
            if self.result.truncated:
                return
            t = self.peek()
            if t.kind == EOF or t.kind in BLOCK_STOP:
                return
            before = self.pos
            self.statement()
            if self.pos == before:
                self._stuck(t)

    def statement(self):
        t = self.peek()
        k = t.kind
        if k == SEMI:
            self.pos += 1
            return
        if k == IF:
            self.if_stmt()
        elif k == WHILE:
            self.while_stmt()
        elif k == FOR:
            self.for_stmt()
        elif k == TRY:
            self.try_stmt()
        elif k == RETURN:
            self.pos += 1
            has_expr = self.peek().kind in EXPR_START
            if has_expr:
                self.expression()
            self.result.events.append(('return', has_expr, t.line))
        elif k in (CONTINUE, BREAK):
            self.pos += 1
            self.result.events.append(
                ('break' if k == BREAK else 'continue', self.loop_depth, t.line))
        elif k == RAISE:
            self.pos += 1
            if self.peek().kind == LPAREN:
                self.call_args()
            elif self.peek().kind in EXPR_START:
                self.expression()
        elif k == EXECUTE:
            self.pos += 1
            if self.peek().kind == LPAREN:
                self.call_args()
            elif self.peek().kind in EXPR_START:
                self.expression()
                while self.peek().kind == COMMA:
                    self.pos += 1
                    self.expression()
            else:
                self.error('«Выполнить» без аргумента', t)
        elif k == GOTO:
            self.pos += 1
            if self.expect(TILDA, '«~»'):
                nt = self.expect(IDENT, 'имя метки')
                if nt is not None:
                    self.result.events.append(('goto', nt.text, nt.line))
        elif k in (ADDHANDLER, REMOVEHANDLER):
            self.pos += 1
            self.expression()
            if self.expect(COMMA, '«,»'):
                self.expression()
            self.result.events.append(
                ('handler', 'add' if k == ADDHANDLER else 'remove', t.line))
        elif k == AWAIT:
            self.pos += 1
            self.expression()
        elif k == TILDA:
            self.label_stmt(t)
        elif k == VAR:
            self.var_decl(False)
        elif k == IDENT:
            self.assign_or_call(t)
        # прочее — не оператор; прогресс обеспечивает code_block._stuck

    def label_stmt(self, t):
        self.pos += 1  # ~
        nt = self.peek()
        if nt.kind != IDENT:
            self.error('после «~» ожидалось имя метки', nt)
            return
        self.pos += 1
        if self.peek().kind == COLON:
            self.pos += 1
            self.result.events.append(('label', nt.text, nt.line))
            return
        # послабление: ~Имя( — вызов (в грамматике только метки/goto)
        if self.peek().kind == LPAREN:
            parts = ['~' + nt.text]
            self.chain(parts, nt)
            return
        self.error('ожидалось «:» после имени метки', self.peek())

    def if_stmt(self):
        self.pos += 1  # Если
        self.expression()
        self.expect(THEN, '«Тогда»')
        self.code_block()
        while self.peek().kind == ELSIF:
            self.pos += 1
            self.expression()
            self.expect(THEN, '«Тогда»')
            self.code_block()
        if self.peek().kind == ELSE:
            self.pos += 1
            self.code_block()
        self.expect(ENDIF, '«КонецЕсли»')

    def while_stmt(self):
        self.pos += 1  # Пока
        self.expression()
        self.expect(DO, '«Цикл»')
        self.loop_depth += 1
        self.code_block()
        self.loop_depth -= 1
        self.expect(ENDDO, '«КонецЦикла»')

    def for_stmt(self):
        self.pos += 1  # Для
        if self.peek().kind == EACH:
            self.pos += 1
            vt = self.expect(IDENT, 'имя переменной цикла')
            self.expect(IN, '«Из»')
            self.expression()
            if vt is not None:
                self.result.events.append(('assign', (vt.text,), vt.line))
        else:
            vt = self.expect(IDENT, 'имя переменной цикла')
            self.expect(ASSIGN, '«=»')
            self.expression()
            self.expect(TO, '«По»')
            self.expression()
            if vt is not None:
                self.result.events.append(('assign', (vt.text,), vt.line))
        self.expect(DO, '«Цикл»')
        self.loop_depth += 1
        self.code_block()
        self.loop_depth -= 1
        self.expect(ENDDO, '«КонецЦикла»')

    def try_stmt(self):
        self.pos += 1  # Попытка
        self.code_block()
        self.expect(EXCEPT, '«Исключение»')
        self.code_block()
        self.expect(ENDTRY, '«КонецПопытки»')

    # -- вызовы и присваивания ---------------------------------------------------

    def assign_or_call(self, tok):
        self.pos += 1  # IDENT
        parts = [tok.text]
        called, incomplete = self.chain(parts, tok)
        if self.peek().kind == ASSIGN:
            self.pos += 1
            self.expression()
            self.result.events.append(('assign', tuple(parts), tok.line))
        else:
            self.result.events.append(
                ('chain', tuple(parts), called, tok.line))
            if not called and not incomplete and len(parts) == 1:
                self.error('ожидался вызов метода или присваивание', tok)
        if self.peek().kind == SEMI:
            self.pos += 1

    def chain(self, parts, first_tok):
        """Хвост обращений: .член, [индекс], (вызов), недописанная точка.

        parts — накапливаемый путь (первое имя уже добавлено вызывающим).
        Возвращает (закончился_вызовом, недописанное_обращение)."""
        called = False
        incomplete = False
        while True:
            t = self.peek()
            if t.kind == LPAREN:
                self.call_args()
                called = True
            elif t.kind == DOT:
                self.pos += 1
                t2 = self.peek()
                if t2.kind == IDENT:
                    self.pos += 1
                    parts.append(t2.text)
                    called = False
                else:
                    incomplete = True
                    break
            elif t.kind == DOT_TRAILING:
                self.pos += 1
                incomplete = True
                break
            elif t.kind == LBRACK:
                self.pos += 1
                self.expression()
                self.expect(RBRACK, '«]»')
                called = False
            else:
                break
        return called, incomplete

    def call_args(self):
        self.pos += 1  # (
        if self.peek().kind == RPAREN:
            self.pos += 1
            return
        while True:
            t = self.peek()
            if t.kind == EOF or t.kind in BLOCK_STOP:
                self.error('ожидалось «)» в аргументах вызова', t)
                return
            if t.kind == RPAREN:
                self.pos += 1
                return
            if t.kind == COMMA:      # пропущенный (пустой) параметр
                self.pos += 1
                continue
            before = self.pos
            self.expression()
            if self.pos == before:
                self.pos += 1        # прогресс после уже сообщённой ошибки
            t2 = self.peek()
            if t2.kind == COMMA:
                self.pos += 1
                continue
            if t2.kind == RPAREN:
                self.pos += 1
                return
            self.error('ожидалось «,» или «)» в аргументах вызова', t2)
            if t2.kind == EOF or t2.kind in BLOCK_STOP:
                return
            self.pos += 1

    # -- выражения ---------------------------------------------------------------

    def expression(self):
        ok = self.member()
        while ok and self.peek().kind in OPERATIONS:
            self.pos += 1
            ok = self.member()
        return ok

    def member(self):
        t = self.peek()
        k = t.kind
        if k in (NOT, MINUS, PLUS):
            self.pos += 1
            return self.member()
        if k in (NUM, DATE, TRUE, FALSE, UNDEFINED, NULL):
            self.pos += 1
            return True
        if k in (STR, STR_START):
            self.string_literal()
            return True
        if k == QUESTION:
            self.ternary()
            return True
        if k == NEW:
            self.new_expr()
            return True
        if k == LPAREN:
            self.pos += 1
            self.expression()
            if self.expect(RPAREN, '«)»'):
                self.chain([None], t)
            return True
        if k == AWAIT:
            self.pos += 1
            return self.expression()
        if k == IDENT:
            self.pos += 1
            parts = [t.text]
            called, _ = self.chain(parts, t)
            self.result.events.append(('chain', tuple(parts), called, t.line))
            return True
        self.error('ожидалось выражение, найдено %s' % self._name(t), t)
        return False

    def string_literal(self):
        while True:
            t = self.peek()
            if t.kind == STR:
                self.pos += 1
                self.result.events.append(('str', t.off, t.end, t.line))
                continue
            if t.kind == STR_START:
                self.pos += 1
                self.result.events.append(('str', t.off, t.end, t.line))
                closed = False
                while self.peek().kind in (STR_PART, STR_TAIL, BAR):
                    if self.peek().kind == STR_TAIL:
                        closed = True
                    self.pos += 1
                if not closed:
                    self.error('незакрытый строковый литерал', t)
                continue
            break

    def ternary(self):
        t = self.peek()
        self.pos += 1  # ?
        self.expect(LPAREN, '«(»')
        self.expression()
        self.expect(COMMA, '«,»')
        self.expression()
        self.expect(COMMA, '«,»')
        self.expression()
        self.expect(RPAREN, '«)»')
        self.chain([None], t)

    def new_expr(self):
        t = self.peek()
        self.pos += 1  # Новый
        name = None
        if self.peek().kind == IDENT:
            name = self.peek().text
            self.pos += 1
        if self.peek().kind == LPAREN:
            self.call_args()
        elif name is None:
            self.error('после «Новый» ожидалось имя типа или «(»', self.peek())
        self.result.events.append(('new', name, t.line))
        self.chain([None], t)


# ---------------------------------------------------------------------------
# Входные функции
# ---------------------------------------------------------------------------

def analyze(text):
    """Полный разбор: ParseResult (диагностика, методы, переменные, события)."""
    tokens, diags = lex(text)
    filtered, pp_diags = validate_preprocessor(tokens)
    parser = Parser(filtered)
    result = parser.run()
    all_diags = diags + pp_diags + result.diags
    all_diags.sort(key=lambda d: (d.line, d.col))
    result.diags = all_diags
    return result


def check_text(text):
    """Список Diagnostic по тексту модуля/фрагмента кода (пустой = синтаксис
    корректен)."""
    return analyze(text).diags
