"""Структурные исключения декодера confdb (происхождение кода — NOTICE.md).

ExtException несёт стек действий/трассировок для диагностики сбоя разбора
объекта; режим --skip-errors печатает его и продолжает разбор.
"""
import sys
import traceback
from os import path


class ExtException(Exception):
    _code = 3000
    _message = None

    def __new__(cls, *args, **kwargs):
        parent = kwargs.get('parent')
        if parent:
            # прокидываем ошибку наверх: сохраняем класс родительской ошибки
            if isinstance(parent, ExtException) \
                    and parent.__class__ != ExtException \
                    and kwargs.get('message') is None \
                    and cls == ExtException:
                return super().__new__(parent.__class__)
        return super().__new__(cls)

    def __init__(self, *, parent=None, code=None, message=None, detail=None, action=None, dump=None,
                 skip_traceback=0, stack=None):
        self.skip_traceback = skip_traceback
        self.code = code
        self.message = message
        self.detail = detail

        self.dump = {} if dump is None else dump
        self.stack = [] if stack is None else stack
        self.action = action

        self.new_msg = bool(message)
        if parent:
            if isinstance(parent, ExtException):  # прокидываем ошибку наверх
                self.add_parent_to_stack(parent)
                if parent.dump:
                    for key, value in parent.dump.items():
                        if key not in self.dump:
                            self.dump[key] = value
                if not message:
                    self.message = parent.message
                    self.detail = parent.detail
                    self.code = parent.code
                if not self.dump:
                    self.dump = parent.dump
            elif isinstance(parent, Exception):
                if not message:
                    self.message = 'Unknown Error'
                if not detail:
                    self.detail = str(parent)
                self.add_sys_exc_to_stack(self.action)
            else:
                raise NotImplementedError(f'ExtException parent={type(parent)}')
        if not self.message:
            self.message = self._message

    def add_parent_to_stack(self, parent):
        if not isinstance(parent, ExtException):
            return None

        if parent.stack:
            self.stack.extend(parent.stack)

        if not parent.action or (self.stack and self.stack[-1].get('action') == parent.action):
            return

        stack1 = dict(action=parent.action)
        if parent.new_msg:
            stack1['message'] = parent.message
            if parent.detail:
                stack1['detail'] = parent.detail
        if not parent.stack:
            data = self.get_sys_exc_info()
            if data:
                stack1['traceback'] = data['traceback']

        self.stack.append(stack1)

    def add_sys_exc_to_stack(self, action):
        try:
            data = self.get_sys_exc_info()
        except IndexError:
            return
        if not data:
            return
        if action:
            data['action'] = action
        self.stack.append(data)

    def get_sys_exc_info(self):
        exc_info = sys.exc_info()
        if exc_info[2] is None:
            return None
        last_call = traceback.extract_tb(exc_info[2], limit=self.skip_traceback + 1)
        last_call = last_call[self.skip_traceback]
        if isinstance(exc_info[1], ExtException):
            message = getattr(exc_info[1], 'message')
            detail = getattr(exc_info[1], 'detail')
        else:
            message = exc_info[0].__name__
            detail = str(exc_info[1])
        return {
            'message': message,
            'detail': detail,
            'traceback': f'{path.basename(path.dirname(last_call.filename))}/{path.basename(last_call.filename)}, '
                         f'{last_call.name}, line {last_call.lineno}'
        }

    @property
    def title(self):
        title = ''
        if self.code:
            title += f'{self.code}: '
        if self.message:
            title += str(self.message)
        if self.detail:
            title += f' - {self.detail}'
        return title

    def __str__(self):
        res = f'{self.title}'
        if self.action:
            res += f'\n Action: {self.action}; '
        if self.dump:
            res += '\n Dump:'
            for elem in self.dump:
                res += f'\n  - {elem}={self.dump[elem]}; '
        if self.stack:
            res += '\n Stack:'
            for stack in self.stack:
                _action = stack.get('action', '')
                message = stack.get('message', '')
                detail = stack.get('detail', '')
                res += '\n  -'
                if _action:
                    res += f' {_action}'
                if message:
                    res += f' {message} {detail}'

                stack_props = ''
                for elem in stack:
                    if elem not in ('action', 'message', 'detail'):
                        stack_props += f'{elem}={stack[elem]}; '
                if stack_props:
                    res += f"({stack_props})"
        return res


class ExtNotImplemented(ExtException):
    _code = 8000
    _message = "Not implemented"