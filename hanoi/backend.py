import zlib
from abc import ABCMeta
from collections import defaultdict

try:
    from redis import Redis
except ImportError:
    # Redis not available.
    # We expect our user to use only MemoryBackend
    pass


class Feature(object):

    __slots__ = ['name', 'field', 'percentage', 'enabled', 'variants']

    def __init__(self, name, field=None, percentage=100, variants=None):
        try:
            if not isinstance(name, basestring):
                raise AttributeError("Feature name should be a string")
        except:
            # python3
            if not isinstance(name, str):
                raise AttributeError("Feature name should be a string")

        self.name = name
        self.field = field
        self.percentage = self._validate_percentage(percentage)
        self.enabled = True
        self.variants = variants

    def _validate_percentage(self, percentage):
        try:
            value = int(percentage)
        except ValueError:
            raise AttributeError("Percentage should be a valid number")

        if 0 > value or value > 100:
            raise AttributeError("Percentage should be a number between 0 and 100")
        return value

    def get_item_id(self, item):
        """
        Return the item id to be used.
        """
        if self.field is None:
            return str(item)
        else:
            _field = getattr(item, self.field)
            if hasattr(_field, '__call__'):
                return _field()
            else:
                return _field

    def __setattr__(self, name, value):
        """
        Validate two fields while setting them:
        - percentage should be an integer between 0 and 100
        - name is read only
        """
        if name == 'percentage':
            value = self._validate_percentage(value)
        elif name == 'name' and hasattr(self, name):
            raise RuntimeError("Unable to update the Feature name")

        object.__setattr__(self, name, value)

    def __repr__(self):
        return "<{0}> applying {1} to {2}% users".format(
            self.name,
            self.field,
            self.percentage
        )


class MemoryBackEnd(object):

    def __init__(self):
        self.funcs = {}
        self.reg = defaultdict(lambda: [])
        self.rules = {}

    def get_functionalities(self):
        return list(self.funcs)

    def add_functionality(self, fn):
        self.funcs[fn.name] = fn

    def get_functionality(self, name):
        return self.funcs.get(name)

    def add(self, name, item):
        self.reg[name].append(item)

    def set_rule(self, name, rule):
        self.rules[name] = rule

    def set_percentage(self, name, percentage):
        self.funcs[name].percentage = percentage

    def is_enabled(self, name, item=None):
        func_is_enabled = name in self.funcs and self.funcs[name].enabled
        if item is None:
            # Global funcionality enabled?
            return func_is_enabled

        if not func_is_enabled:
            # Avoid additional lookup as the functionality is globally disabled
            return func_is_enabled

        flag = self.funcs[name].percentage == 100
        flag = flag or item in self.reg[name]
        flag = flag or (name in self.rules and self.rules[name].search(str(item)) is not None)
        if not flag and self.funcs[name].percentage > 0:
            try:  # python 3
                val = bytes(self.funcs[name].get_item_id(item), 'utf-8')
            except:
                val = self.funcs[name].get_item_id(item)
            flag = zlib.crc32(val) % 100 <= self.funcs[name].percentage
        return flag

    def disable(self, name):
        self.funcs[name].enabled = False

    def enable(self, name, enable_to_all=False):
        self.funcs[name].enabled = True
        if enable_to_all:
            self.funcs[name].percentage = 100

    def toggle(self, name):
        self.funcs[name].enabled = not self.funcs[name].enabled

    def variant(self, name, item):
        if not self.is_enabled(name, item):
            return None
        f = self.get_functionality(name)
        try:  # python 3
            item = bytes(item, 'utf-8')
        except:
            pass

        return f.variants[zlib.crc32(item) % len(f.variants)]


class RedisAbstractBackEnd(object):
    """
    Having two classes implementing a Hanoi BackEnd in REDIS means duplicating
    a lot of code. This class is defined as abstract for the sake of clarity
    that should not be directly used but by means of a subclass.
    """
    __metaclass__ = ABCMeta

    PREFIX = "h:{0}"

    def __init__(self, obj=None):
        if obj is None:
            self._redis = Redis()
        elif isinstance(obj, (list, tuple)):
            host, port = obj[0], obj[1]
            db = obj[2] if len(obj) >= 3 else 0
            self._redis = Redis(host=host, port=port, db=db)
        else:
            self._redis = obj

        self._prefix_len = len(self.PREFIX.format(''))

        # TODO: rules should be stored in REDIS as well
        self.rules = {}

    def _get_func_key(self, name):
        return self.PREFIX.format(name)


class RedisBackEnd(RedisAbstractBackEnd):
    """
    Implements a BackEnd using REDIS as system storage.
    - It creates a new key per functionality that should be stored.

    - To retrieve the list of functionalities, it searches Redis DB using KEYS.
        Take this into account as it might create a performance issue if the amount
        of functionalities is huge: http://redis.io/commands/keys

    - Specific functionality information (percentage, users) is stored using a String.
        Users could be stored via SET, but it would imply a O(N) operation to retrieve
        all the users.

    Another approach to store the information:
    - serialize the functionality names, percentage in a String (to avoid using KEYS `pattern`)
    - store the users in a SET and warn about retrieving every user. We might even deprecate the
    `get_functionality` operation as the main purpose is just check if a functionality is enabled
    for a specific user.
    """

    @classmethod
    def unserialize_feature(cls, name, value):
        if value:
            enabled, percentage, field, users, variants = value.split("|")
            variants = variants.split(',') or None
        else:
            percentage = 100
            users = field = variants = None
            enabled = '1'

        f = Feature(name, field if field != '' else None, percentage, variants)
        f.enabled = enabled == '1'

        return f, users.split(",") if users else []

    def get_functionalities(self):
        func = self._redis.keys(self._get_func_key('*'))  # HACK: get every functionality
        return [x[self._prefix_len:].decode('utf-8') for x in func]

    def add_functionality(self, fn, users=None):
        fn_info = ['1' if fn.enabled else '0',
            str(fn.percentage),
            fn.field or '',
            ','.join(users) if users is not None else ''
        ]

        if fn.variants is not None:
            var_info = ','.join(fn.variants)
            fn_info.append(var_info)
        else:
            fn_info.append('')

        self._redis.set(
            self._get_func_key(fn.name),
            "|".join(fn_info)
        )

    def _get_functionality(self, name):
        redis_value = self._redis.get(self._get_func_key(name))
        if redis_value:
            return self.unserialize_feature(name, redis_value.decode('utf-8'))
        else:
            return None, []

    def get_functionality(self, name):
        return self._get_functionality(name)[0]

    def _add(self, name, item):
        func, users = self._get_functionality(name)
        if func:
            if item not in users:
                users.append(func.get_item_id(item))
                self.add_functionality(func, users)
            else:
                pass  # Avoid duplicating users
        else:
            # Functionality does not exist
            raise ValueError("Functionality <%s> does not exist" % name)

    def add(self, name, item):
        self._add(name, item)

    def set_rule(self, name, rule):
        self.rules[name] = rule

    def set_percentage(self, name, percentage):
        func, users = self._get_functionality(name)
        func.percentage = percentage
        self.add_functionality(func, users)

    def is_enabled(self, name, item=None):
        func_is_enabled = name in self.get_functionalities()
        if not func_is_enabled:
            # Stop if functionality not even exist
            return False

        functionality, users = self._get_functionality(name)
        func_is_enabled = functionality.enabled

        if item is None:
            # Global funcionality enabled?
            return func_is_enabled

        if not func_is_enabled:
            # Avoid additional lookup as the functionality is globally disabled
            return func_is_enabled

        flag = functionality.percentage == 100
        flag = flag or functionality.get_item_id(item) in users
        flag = flag or (name in self.rules and self.rules[name].search(str(item)) is not None)
        if not flag and functionality.percentage > 0:
            try:  # python 3
                item = bytes(item, 'utf-8')
            except:
                pass
            flag = zlib.crc32(item) % 100 <= functionality.percentage
        return flag

    def disable(self, name):
        func, users = self._get_functionality(name)
        func.enabled = False
        self.add_functionality(func, users)

    def enable(self, name, enable_to_all=False):
        func, users = self._get_functionality(name)
        func.enabled = True
        if enable_to_all:
            func.percentage = 100
        self.add_functionality(func, users)

    def toggle(self, name):
        func, users = self._get_functionality(name)
        func.enabled = not func.enabled
        self.add_functionality(func, users)

    def variant(self, name, item):
        if not self.is_enabled(name, item):
            return None
        f, _ = self._get_functionality(name)
        try:  # python 3
            item = bytes(item, "utf-8")
        except:
            pass

        return f.variants[zlib.crc32(item) % len(f.variants)]


class RedisHighPerfBackEnd(RedisAbstractBackEnd):
    """
    Implements a BackEnd using REDIS as system storage.
    Per each functionality to be tackled:
        - It creates a STRING with the relevant Functionality
          information (enabled, field, percentage)
        - It will use a SET with the `whitelisted` identifiers

    - get_functionalities is not implemented

    Use this implementation for better performance.
    """

    SET_PREFIX = 'h:users:{0}'

    @classmethod
    def unserialize_feature(cls, name, value):
        """
        Destructure Feature information from the serialized format
        """
        if value:
            enabled, percentage, field, variants = value.split("|")
            variants = variants.split(',') or None
        else:
            enabled = '1'
            percentage = 100
            field = variants = None

        f = Feature(name, field if field != '' else None, percentage, variants)
        f.enabled = enabled == '1'

        return f

    def get_functionalities(self):
        raise NotImplementedError('get_functionalities unavailable in RedisHighPerfBackEnd')

    def add_functionality(self, fn, users=None):
        fn_info = ['1' if fn.enabled else '0', str(fn.percentage), fn.field or '']

        if fn.variants is not None:
            # Every variant should include a name and (optionally) a percentage
            # Missing percentage dictates same weight for every variant
            # TODO: define percentages. Currently they're by default equally distributed
            #default_weight = str(100 / len(fn.variants))
            #var_info = ','.join([v + '-' + default_weight for v in fn.variants])
            var_info = ','.join(fn.variants)
            fn_info.append(var_info)
        else:
            fn_info.append('')

        self._redis.set(
            self._get_func_key(fn.name),
            "|".join(fn_info)
        )
        if users:
            self._redis.sadd(self.SET_PREFIX.format(fn.name), *users)

    def _get_functionality(self, name):
        redis_value = self._redis.get(self._get_func_key(name))
        if redis_value:
            return self.unserialize_feature(name, redis_value.decode('utf-8'))
        else:
            return None

    def get_functionality(self, name):
        return self._get_functionality(name)

    def add(self, name, item):
        func = self._get_functionality(name)
        if func:
            self._redis.sadd(
                self.SET_PREFIX.format(func.name),
                func.get_item_id(item)
            )
        else:
            raise ValueError("Functionality <%s> does not exist" % name)

    def set_rule(self, name, rule):
        self.rules[name] = rule

    def set_percentage(self, name, percentage):
        func = self._get_functionality(name)
        func.percentage = percentage
        self.add_functionality(func)

    def is_enabled(self, name, item=None):
        functionality = self._get_functionality(name)
        if not functionality:
            # Stop if functionality not even exist
            return False

        func_is_enabled = functionality.enabled

        if item is None:
            # Global funcionality enabled?
            return func_is_enabled

        if not func_is_enabled:
            # Avoid additional lookup as the functionality is globally disabled
            return func_is_enabled

        flag = functionality.percentage == 100
        flag = flag or self._allowed_user(functionality, item)
        flag = flag or (name in self.rules and self.rules[name].search(str(item)) is not None)
        if not flag and functionality.percentage > 0:
            try:  # python 3
                item = bytes(item, 'utf-8')
            except:
                pass
            flag = zlib.crc32(item) % 100 <= functionality.percentage
        return flag

    def _allowed_user(self, functionality, user):
        return self._redis.sismember(
            self.SET_PREFIX.format(functionality.name),
            functionality.get_item_id(user)
        )

    def disable(self, name):
        func = self._get_functionality(name)
        func.enabled = False
        self.add_functionality(func)

    def enable(self, name, enable_to_all=False):
        func = self._get_functionality(name)
        func.enabled = True
        if enable_to_all:
            func.percentage = 100
        self.add_functionality(func)

    def toggle(self, name):
        func = self._get_functionality(name)
        func.enabled = not func.enabled
        self.add_functionality(func)

    def variant(self, name, item):
        if not self.is_enabled(name, item):
            return None
        f = self._get_functionality(name)
        try:  # python 3
            item = bytes(item, "utf-8")
        except:
            pass

        return f.variants[zlib.crc32(item) % len(f.variants)]
