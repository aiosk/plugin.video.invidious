# -*- coding: utf-8 -*-


# lifted from: https://github.com/ytdl-org/youtube-dl/blob/master/youtube_dl/jsinterp.py
# LICENSE: The Unlicense, https://github.com/ytdl-org/youtube-dl/blob/master/LICENSE
# AUTHORS: https://github.com/ytdl-org/youtube-dl/blob/master/AUTHORS


from __future__ import unicode_literals

import json
import operator
import re
import time

try:
    import collections.abc as compat_collections_abc
except ImportError:
    import collections as compat_collections_abc

try:
    compat_str = unicode  # Python 2
except NameError:
    compat_str = str

from .find import MatchError, __find__

MutableMapping = compat_collections_abc.MutableMapping


# ------------------------------------------------------------------------------
# JSInterpreter

def remove_quotes(s):
    if s is None or len(s) < 2:
        return s
    for quote in ('"', "'", ):
        if s[0] == quote and s[-1] == quote:
            return s[1:-1]
    return s


class Nonlocal:
    pass

_OPERATORS = [
    ('|', operator.or_),
    ('^', operator.xor),
    ('&', operator.and_),
    ('>>', operator.rshift),
    ('<<', operator.lshift),
    ('-', operator.sub),
    ('+', operator.add),
    ('%', operator.mod),
    ('/', operator.truediv),
    ('*', operator.mul),
]
_ASSIGN_OPERATORS = [(op + '=', opfunc) for op, opfunc in _OPERATORS]
_ASSIGN_OPERATORS.append(('=', (lambda cur, right: right)))

_NAME_RE = r'[a-zA-Z_$][a-zA-Z_$0-9]*'

_MATCHING_PARENS = dict(zip(*zip('()', '{}', '[]')))


class JS_Break(Exception):
    def __init__(self):
        Exception.__init__(self, 'Invalid break')


class JS_Continue(Exception):
    def __init__(self):
        Exception.__init__(self, 'Invalid continue')


class LocalNameSpace(MutableMapping):
    def __init__(self, *stack):
        self.stack = tuple(stack)

    def __getitem__(self, key):
        for scope in self.stack:
            if key in scope:
                return scope[key]
        raise KeyError(key)

    def __setitem__(self, key, value):
        for scope in self.stack:
            if key in scope:
                scope[key] = value
                break
        else:
            self.stack[0][key] = value
        return value

    def __delitem__(self, key):
        raise NotImplementedError('Deleting is not supported')

    def __iter__(self):
        for scope in self.stack:
            for scope_item in iter(scope):
                yield scope_item

    def __len__(self, key):
        return len(iter(self))

    def __repr__(self):
        return 'LocalNameSpace%s' % (self.stack, )


class JSInterpreter(object):
    def __init__(self, code, objects=None):
        if objects is None:
            objects = {}
        self.code = code
        self._functions = {}
        self._objects = objects
        self.__named_object_counter = 0

    def _named_object(self, namespace, obj):
        self.__named_object_counter += 1
        name = '__youtube_dl_jsinterp_obj%s' % (self.__named_object_counter, )
        namespace[name] = obj
        return name

    @staticmethod
    def _separate(expr, delim=',', max_split=None):
        if not expr:
            return
        counters = {k: 0 for k in _MATCHING_PARENS.values()}
        start, splits, pos, delim_len = 0, 0, 0, len(delim) - 1
        for idx, char in enumerate(expr):
            if char in _MATCHING_PARENS:
                counters[_MATCHING_PARENS[char]] += 1
            elif char in counters:
                counters[char] -= 1
            if char != delim[pos] or any(counters.values()):
                pos = 0
                continue
            elif pos != delim_len:
                pos += 1
                continue
            yield expr[start: idx - delim_len]
            start, pos = idx + 1, 0
            splits += 1
            if max_split and splits >= max_split:
                break
        yield expr[start:]

    @staticmethod
    def _separate_at_paren(expr, delim):
        separated = list(JSInterpreter._separate(expr, delim, 1))
        if len(separated) < 2:
            raise Exception('No terminating paren {0} in {1}'.format(delim, expr))
        return separated[0][1:].strip(), separated[1].strip()

    def interpret_statement(self, stmt, local_vars, allow_recursion=100):
        if allow_recursion < 0:
            raise Exception('Recursion limit reached')

        sub_statements = list(self._separate(stmt, ';'))
        stmt = (sub_statements or ['']).pop()
        for sub_stmt in sub_statements:
            ret, should_abort = self.interpret_statement(sub_stmt, local_vars, allow_recursion - 1)
            if should_abort:
                return ret

        should_abort = False
        stmt = stmt.lstrip()
        stmt_m = re.match(r'var\s', stmt)
        if stmt_m:
            expr = stmt[len(stmt_m.group(0)):]
        else:
            return_m = re.match(r'return(?:\s+|$)', stmt)
            if return_m:
                expr = stmt[len(return_m.group(0)):]
                should_abort = True
            else:
                # Try interpreting it as an expression
                expr = stmt

        v = self.interpret_expression(expr, local_vars, allow_recursion)
        return v, should_abort

    def interpret_expression(self, expr, local_vars, allow_recursion):
        expr = expr.strip()
        if expr == '':  # Empty expression
            return None

        if expr.startswith('{'):
            inner, outer = self._separate_at_paren(expr, '}')
            inner, should_abort = self.interpret_statement(inner, local_vars, allow_recursion - 1)
            if not outer or should_abort:
                return inner
            else:
                expr = json.dumps(inner) + outer

        if expr.startswith('('):
            inner, outer = self._separate_at_paren(expr, ')')
            inner = self.interpret_expression(inner, local_vars, allow_recursion)
            if not outer:
                return inner
            else:
                expr = json.dumps(inner) + outer

        if expr.startswith('['):
            inner, outer = self._separate_at_paren(expr, ']')
            name = self._named_object(local_vars, [
                self.interpret_expression(item, local_vars, allow_recursion)
                for item in self._separate(inner)])
            expr = name + outer

        m = re.match(r'try\s*', expr)
        if m:
            if expr[m.end()] == '{':
                try_expr, expr = self._separate_at_paren(expr[m.end():], '}')
            else:
                try_expr, expr = expr[m.end() - 1:], ''
            ret, should_abort = self.interpret_statement(try_expr, local_vars, allow_recursion - 1)
            if should_abort:
                return ret
            return self.interpret_statement(expr, local_vars, allow_recursion - 1)[0]

        m = re.match(r'(?:(?P<catch>catch)|(?P<for>for)|(?P<switch>switch))\s*\(', expr)
        md = m.groupdict() if m else {}
        if md.get('catch'):
            # We ignore the catch block
            _, expr = self._separate_at_paren(expr, '}')
            return self.interpret_statement(expr, local_vars, allow_recursion - 1)[0]

        elif md.get('for'):
            def raise_constructor_error(c):
                raise Exception(
                    'Premature return in the initialization of a for loop in {0!r}'.format(c))

            constructor, remaining = self._separate_at_paren(expr[m.end() - 1:], ')')
            if remaining.startswith('{'):
                body, expr = self._separate_at_paren(remaining, '}')
            else:
                m = re.match(r'switch\s*\(', remaining)  # FIXME
                if m:
                    switch_val, remaining = self._separate_at_paren(remaining[m.end() - 1:], ')')
                    body, expr = self._separate_at_paren(remaining, '}')
                    body = 'switch(%s){%s}' % (switch_val, body)
                else:
                    body, expr = remaining, ''
            start, cndn, increment = self._separate(constructor, ';')
            if self.interpret_statement(start, local_vars, allow_recursion - 1)[1]:
                raise_constructor_error(constructor)
            while True:
                if not self.interpret_expression(cndn, local_vars, allow_recursion):
                    break
                try:
                    ret, should_abort = self.interpret_statement(body, local_vars, allow_recursion - 1)
                    if should_abort:
                        return ret
                except JS_Break:
                    break
                except JS_Continue:
                    pass
                if self.interpret_statement(increment, local_vars, allow_recursion - 1)[1]:
                    raise_constructor_error(constructor)
            return self.interpret_statement(expr, local_vars, allow_recursion - 1)[0]

        elif md.get('switch'):
            switch_val, remaining = self._separate_at_paren(expr[m.end() - 1:], ')')
            switch_val = self.interpret_expression(switch_val, local_vars, allow_recursion)
            body, expr = self._separate_at_paren(remaining, '}')
            items = body.replace('default:', 'case default:').split('case ')[1:]
            for default in (False, True):
                matched = False
                for item in items:
                    case, stmt = [i.strip() for i in self._separate(item, ':', 1)]
                    if default:
                        matched = matched or case == 'default'
                    elif not matched:
                        matched = (case != 'default'
                                   and switch_val == self.interpret_expression(case, local_vars, allow_recursion))
                    if not matched:
                        continue
                    try:
                        ret, should_abort = self.interpret_statement(stmt, local_vars, allow_recursion - 1)
                        if should_abort:
                            return ret
                    except JS_Break:
                        break
                if matched:
                    break
            return self.interpret_statement(expr, local_vars, allow_recursion - 1)[0]

        # Comma separated statements
        sub_expressions = list(self._separate(expr))
        expr = sub_expressions.pop().strip() if sub_expressions else ''
        for sub_expr in sub_expressions:
            self.interpret_expression(sub_expr, local_vars, allow_recursion)

        for m in re.finditer(r'''(?x)
                (?P<pre_sign>\+\+|--)(?P<var1>%(_NAME_RE)s)|
                (?P<var2>%(_NAME_RE)s)(?P<post_sign>\+\+|--)''' % globals(), expr):
            var = m.group('var1') or m.group('var2')
            start, end = m.span()
            sign = m.group('pre_sign') or m.group('post_sign')
            ret = local_vars[var]
            local_vars[var] += 1 if sign[0] == '+' else -1
            if m.group('pre_sign'):
                ret = local_vars[var]
            expr = expr[:start] + json.dumps(ret) + expr[end:]

        for op, opfunc in _ASSIGN_OPERATORS:
            m = re.match(r'''(?x)
                (?P<out>%s)(?:\[(?P<index>[^\]]+?)\])?
                \s*%s
                (?P<expr>.*)$''' % (_NAME_RE, re.escape(op)), expr)
            if not m:
                continue
            right_val = self.interpret_expression(m.group('expr'), local_vars, allow_recursion)

            if m.groupdict().get('index'):
                lvar = local_vars[m.group('out')]
                idx = self.interpret_expression(m.group('index'), local_vars, allow_recursion)
                if not isinstance(idx, int):
                    raise Exception('List indices must be integers: %s' % (idx, ))
                cur = lvar[idx]
                val = opfunc(cur, right_val)
                lvar[idx] = val
                return val
            else:
                cur = local_vars.get(m.group('out'))
                val = opfunc(cur, right_val)
                local_vars[m.group('out')] = val
                return val

        if expr.isdigit():
            return int(expr)

        if expr == 'break':
            raise JS_Break()
        elif expr == 'continue':
            raise JS_Continue()

        var_m = re.match(
            r'(?!if|return|true|false|null)(?P<name>%s)$' % _NAME_RE,
            expr)
        if var_m:
            return local_vars[var_m.group('name')]

        try:
            return json.loads(expr)
        except ValueError:
            pass

        m = re.match(
            r'(?P<in>%s)\[(?P<idx>.+)\]$' % _NAME_RE, expr)
        if m:
            val = local_vars[m.group('in')]
            idx = self.interpret_expression(m.group('idx'), local_vars, allow_recursion)
            return val[idx]

        def raise_expr_error(where, op, exp):
            raise Exception('Premature {0} return of {1} in {2!r}'.format(where, op, exp))

        for op, opfunc in _OPERATORS:
            separated = list(self._separate(expr, op))
            if len(separated) < 2:
                continue
            right_val = separated.pop()
            left_val = op.join(separated)
            left_val, should_abort = self.interpret_statement(
                left_val, local_vars, allow_recursion - 1)
            if should_abort:
                raise_expr_error('left-side', op, expr)
            right_val, should_abort = self.interpret_statement(
                right_val, local_vars, allow_recursion - 1)
            if should_abort:
                raise_expr_error('right-side', op, expr)
            return opfunc(left_val or 0, right_val)

        m = re.match(
            r'(?P<var>%s)(?:\.(?P<member>[^(]+)|\[(?P<member2>[^]]+)\])\s*' % _NAME_RE,
            expr)
        if m:
            variable = m.group('var')
            nl = Nonlocal()

            nl.member = remove_quotes(m.group('member') or m.group('member2'))
            arg_str = expr[m.end():]
            if arg_str.startswith('('):
                arg_str, remaining = self._separate_at_paren(arg_str, ')')
            else:
                arg_str, remaining = None, arg_str

            def assertion(cndn, msg):
                """ assert, but without risk of getting optimized out """
                if not cndn:
                    raise Exception('{0} {1}: {2}'.format(nl.member, msg, expr))

            def eval_method():
                # nonlocal member
                member = nl.member
                if variable == 'String':
                    obj = compat_str
                elif variable in local_vars:
                    obj = local_vars[variable]
                else:
                    if variable not in self._objects:
                        self._objects[variable] = self.extract_object(variable)
                    obj = self._objects[variable]

                if arg_str is None:
                    # Member access
                    if member == 'length':
                        return len(obj)
                    return obj[member]

                # Function call
                argvals = [
                    self.interpret_expression(v, local_vars, allow_recursion)
                    for v in self._separate(arg_str)]

                if obj == compat_str:
                    if member == 'fromCharCode':
                        assertion(argvals, 'takes one or more arguments')
                        return ''.join(map(chr, argvals))
                    raise Exception('Unsupported string method %s' % (member, ))

                if member == 'split':
                    assertion(argvals, 'takes one or more arguments')
                    assertion(argvals == [''], 'with arguments is not implemented')
                    return list(obj)
                elif member == 'join':
                    assertion(isinstance(obj, list), 'must be applied on a list')
                    assertion(len(argvals) == 1, 'takes exactly one argument')
                    return argvals[0].join(obj)
                elif member == 'reverse':
                    assertion(not argvals, 'does not take any arguments')
                    obj.reverse()
                    return obj
                elif member == 'slice':
                    assertion(isinstance(obj, list), 'must be applied on a list')
                    assertion(len(argvals) == 1, 'takes exactly one argument')
                    return obj[argvals[0]:]
                elif member == 'splice':
                    assertion(isinstance(obj, list), 'must be applied on a list')
                    assertion(argvals, 'takes one or more arguments')
                    index, howMany = map(int, (argvals + [len(obj)])[:2])
                    if index < 0:
                        index += len(obj)
                    add_items = argvals[2:]
                    res = []
                    for i in range(index, min(index + howMany, len(obj))):
                        res.append(obj.pop(index))
                    for i, item in enumerate(add_items):
                        obj.insert(index + i, item)
                    return res
                elif member == 'unshift':
                    assertion(isinstance(obj, list), 'must be applied on a list')
                    assertion(argvals, 'takes one or more arguments')
                    for item in reversed(argvals):
                        obj.insert(0, item)
                    return obj
                elif member == 'pop':
                    assertion(isinstance(obj, list), 'must be applied on a list')
                    assertion(not argvals, 'does not take any arguments')
                    if not obj:
                        return
                    return obj.pop()
                elif member == 'push':
                    assertion(argvals, 'takes one or more arguments')
                    obj.extend(argvals)
                    return obj
                elif member == 'forEach':
                    assertion(argvals, 'takes one or more arguments')
                    assertion(len(argvals) <= 2, 'takes at-most 2 arguments')
                    f, this = (argvals + [''])[:2]
                    return [f((item, idx, obj), this=this) for idx, item in enumerate(obj)]
                elif member == 'indexOf':
                    assertion(argvals, 'takes one or more arguments')
                    assertion(len(argvals) <= 2, 'takes at-most 2 arguments')
                    idx, start = (argvals + [0])[:2]
                    try:
                        return obj.index(idx, start)
                    except ValueError:
                        return -1

                if isinstance(obj, list):
                    member = int(member)
                    nl.member = member
                return obj[member](argvals)

            if remaining:
                return self.interpret_expression(
                    self._named_object(local_vars, eval_method()) + remaining,
                    local_vars, allow_recursion)
            else:
                return eval_method()

        m = re.match(r'^(?P<func>%s)\((?P<args>[a-zA-Z0-9_$,]*)\)$' % _NAME_RE, expr)
        if m:
            fname = m.group('func')
            argvals = tuple([
                int(v) if v.isdigit() else local_vars[v]
                for v in self._separate(m.group('args'))])
            if fname in local_vars:
                return local_vars[fname](argvals)
            elif fname not in self._functions:
                self._functions[fname] = self.extract_function(fname)
            return self._functions[fname](argvals)

        if expr:
            raise Exception('Unsupported JS expression %r' % expr)

    def extract_object(self, objname):
        _FUNC_NAME_RE = r'''(?:[a-zA-Z$0-9]+|"[a-zA-Z$0-9]+"|'[a-zA-Z$0-9]+')'''
        obj = {}
        obj_m = re.search(
            r'''(?x)
                (?<!this\.)%s\s*=\s*{\s*
                    (?P<fields>(%s\s*:\s*function\s*\(.*?\)\s*{.*?}(?:,\s*)?)*)
                }\s*;
            ''' % (re.escape(objname), _FUNC_NAME_RE),
            self.code)
        fields = obj_m.group('fields')
        # Currently, it only supports function definitions
        fields_m = re.finditer(
            r'''(?x)
                (?P<key>%s)\s*:\s*function\s*\((?P<args>[a-z,]+)\){(?P<code>[^}]+)}
            ''' % _FUNC_NAME_RE,
            fields)
        for f in fields_m:
            argnames = f.group('args').split(',')
            obj[remove_quotes(f.group('key'))] = self.build_function(argnames, f.group('code'))

        return obj

    def extract_function_code(self, funcname):
        """ @returns argnames, code """
        func_m = re.search(
            r'''(?x)
                (?:function\s+%(f_n)s|[{;,]\s*%(f_n)s\s*=\s*function|var\s+%(f_n)s\s*=\s*function)\s*
                \((?P<args>[^)]*)\)\s*
                (?P<code>\{(?:(?!};)[^"]|"([^"]|\\")*")+\})''' % {'f_n': re.escape(funcname), },
            self.code)
        code, _ = self._separate_at_paren(func_m.group('code'), '}')  # refine the match
        if func_m is None:
            raise Exception('Could not find JS function %r' % funcname)
        return func_m.group('args').split(','), code

    def extract_function(self, funcname):
        return self.extract_function_from_code(*self.extract_function_code(funcname))

    def extract_function_from_code(self, argnames, code, *global_stack):
        local_vars = {}
        while True:
            mobj = re.search(r'function\((?P<args>[^)]*)\)\s*{', code)
            if mobj is None:
                break
            start, body_start = mobj.span()
            body, remaining = self._separate_at_paren(code[body_start - 1:], '}')
            name = self._named_object(
                local_vars,
                self.extract_function_from_code(
                    [x.strip() for x in mobj.group('args').split(',')],
                    body, local_vars, *global_stack))
            code = code[:start] + name + remaining
        return self.build_function(argnames, code, local_vars, *global_stack)

    def call_function(self, funcname, *args):
        return self.extract_function(funcname)(args)

    def build_function(self, argnames, code, *global_stack):
        global_stack = list(global_stack) or [{}]
        local_vars = global_stack.pop(0)

        def resf(args, **kwargs):
            local_vars.update(dict(zip(argnames, args)))
            local_vars.update(kwargs)
            var_stack = LocalNameSpace(local_vars, *global_stack)
            for stmt in self._separate(code.replace('\n', ''), ';'):
                ret, should_abort = self.interpret_statement(stmt, var_stack)
                if should_abort:
                    break
            return ret
        return resf


# ------------------------------------------------------------------------------
# Solver

__patterns__ = (
    r'\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    r'\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*encodeURIComponent\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    r'\bm=(?P<sig>[a-zA-Z0-9$]{2,})\(decodeURIComponent\(h\.s\)\)',
    r'\bc&&\(c=(?P<sig>[a-zA-Z0-9$]{2,})\(decodeURIComponent\(c\)\)',
    r'(?:\b|[^a-zA-Z0-9$])(?P<sig>[a-zA-Z0-9$]{2,})\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\);[a-zA-Z0-9$]{2}\.[a-zA-Z0-9$]{2}\(a,\d+\)',
    r'(?:\b|[^a-zA-Z0-9$])(?P<sig>[a-zA-Z0-9$]{2,})\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)',
    r'(?P<sig>[a-zA-Z0-9$]+)\s*=\s*function\(\s*a\s*\)\s*{\s*a\s*=\s*a\.split\(\s*""\s*\)',
    # Obsolete patterns
    #r'(["\'])signature\1\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'\.sig\|\|(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'yt\.akamaized\.net/\)\s*\|\|\s*.*?\s*[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?:encodeURIComponent\s*\()?\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'\b[cs]\s*&&\s*[adf]\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'\b[a-zA-Z0-9]+\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'\bc\s*&&\s*a\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\(',
    #r'\bc\s*&&\s*[a-zA-Z0-9]+\.set\([^,]+\s*,\s*\([^)]*\)\s*\(\s*(?P<sig>[a-zA-Z0-9$]+)\('
)

def findName(js):
    for pattern in __patterns__:
        try:
            return __find__(pattern, js).group('sig')
        except MatchError:
            continue
    raise MatchError('signature function name')


class Solver(object):

    def __init__(self, js, expire=3600):
        self.__expire__ = time.time() + expire
        self.__solve__ = JSInterpreter(js).extract_function(findName(js))

    def extractUrl(self, cipher):
        url = cipher['url'][0]
        if 'signature' in url:
            return url
        if 's' not in cipher:
            if ('&sig=' in url) or ('&lsig=' in url):
                return url
            raise Exception('missing signature')
        return '&'.join(
            (url, '='.join((cipher['sp'][0], self.__solve__(cipher['s']))))
        )

