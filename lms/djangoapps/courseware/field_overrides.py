"""
This module provides a :class:`~xblock.field_data.FieldData` implementation
which wraps an other `FieldData` object and provides overrides based on the
user.  The use of providers allows for overrides that are arbitrarily
extensible.  One provider is found in `courseware.student_field_overrides`
which allows for fields to be overridden for individual students.  One can
envision other providers being written that allow for fields to be overridden
base on membership of a student in a cohort, or similar.  The use of an
extensible, modular architecture allows for overrides being done in ways not
envisioned by the authors.

Currently, this module is used in the `module_render` module in this same
package and is used to wrap the `authored_data` when constructing an
`LmsFieldData`.  This means overrides will be in effect for all scopes covered
by `authored_data`, e.g. course content and settings stored in Mongo.
"""
import threading

from abc import ABCMeta, abstractmethod
from contextlib import contextmanager
from django.conf import settings
from xblock.field_data import FieldData
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.inheritance import InheritanceMixin


NOTSET = object()


def resolve_dotted(name):
    """
    Given the dotted name for a Python object, performs any necessary imports
    and returns the object.
    """
    names = name.split('.')
    path = names.pop(0)
    target = __import__(path)
    while names:
        segment = names.pop(0)
        path += '.' + segment
        try:
            target = getattr(target, segment)
        except AttributeError:
            __import__(path)
            target = getattr(target, segment)
    return target


class OverrideFieldData(FieldData):
    """
    A :class:`~xblock.field_data.FieldData` which wraps another `FieldData`
    object and allows for fields handled by the wrapped `FieldData` to be
    overriden by arbitrary providers.

    Providers are configured by use of the Django setting,
    `FIELD_OVERRIDE_PROVIDERS` which should be a tuple of dotted names of
    :class:`FieldOverrideProvider` concrete implementations.  Note that order
    is important for this setting.  Override providers will tried in the order
    configured in the setting.  The first provider to find an override 'wins'
    for a particular field lookup.
    """
    provider_classes = None

    @classmethod
    def wrap(cls, user, wrapped):
        """
        Will return a :class:`OverrideFieldData` which wraps the field data
        given in `wrapped` for the given `user`, if override providers are
        configred.  If no override providers are configured, using the Django
        setting, `FIELD_OVERRIDE_PROVIDERS`, returns `wrapped`, eliminating
        any performance impact of this feature if no override providers are
        configured.
        """
        if cls.provider_classes is None:
            cls.provider_classes = tuple(
                (resolve_dotted(name) for name in
                 settings.FIELD_OVERRIDE_PROVIDERS))

        if cls.provider_classes:
            return cls(user, wrapped)

        return wrapped

    def __init__(self, user, fallback):
        self.fallback = fallback
        self.providers = tuple((cls(user) for cls in self.provider_classes))

    def get_override(self, block, name):
        """
        Checks for an override for the field identified by `name` in `block`.
        Returns the overridden value or `NOTSET` if no override is found.
        """
        if not overrides_disabled():
            for provider in self.providers:
                value = provider.get(block, name, NOTSET)
                if value is not NOTSET:
                    return value
        return NOTSET

    def get(self, block, name):
        value = self.get_override(block, name)
        if value is not NOTSET:
            return value
        return self.fallback.get(block, name)

    def set(self, block, name, value):
        self.fallback.set(block, name, value)

    def delete(self, block, name):
        self.fallback.delete(block, name)

    def has(self, block, name):
        has = self.get_override(block, name)
        if has is NOTSET:
            # If this is an inheritable field and an override is set above,
            # then we want to return False here, so the field_data uses the
            # override and not the original value for this block.
            inheritable = InheritanceMixin.fields.keys()
            if name in inheritable:
                for ancestor in _lineage(block):
                    if self.get_override(ancestor, name) is not NOTSET:
                        return False

        return has is not NOTSET or self.fallback.has(block, name)

    def set_many(self, block, update_dict):
        return self.fallback.set_many(block, update_dict)

    def default(self, block, name):
        # The `default` method is overloaded by the field storage system to
        # also handle inheritance.
        if not overrides_disabled():
            inheritable = InheritanceMixin.fields.keys()
            if name in inheritable:
                for ancestor in _lineage(block):
                    value = self.get_override(ancestor, name)
                    if value is not NOTSET:
                        return value
        return self.fallback.default(block, name)


class _OverridesDisabled(threading.local):
    """
    A thread local used to manage state of overrides being disabled or not.
    """
    disabled = ()


_OVERRIDES_DISABLED = _OverridesDisabled()


@contextmanager
def disable_overrides():
    """
    A context manager which disables field overrides inside the context of a
    `with` statement, allowing code to get at the `original` value of a field.
    """
    prev = _OVERRIDES_DISABLED.disabled
    _OVERRIDES_DISABLED.disabled += (True,)
    yield
    _OVERRIDES_DISABLED.disabled = prev


def overrides_disabled():
    """
    Checks to see whether overrides are disabled in the current context.
    Returns a boolean value.  See `disable_overrides`.
    """
    return bool(_OVERRIDES_DISABLED.disabled)


class FieldOverrideProvider(object):
    """
    Abstract class which defines the interface that a `FieldOverrideProvider`
    must provide.  In general, providers should derive from this class, but
    it's not strictly necessary as long as they correctly implement this
    interface.

    A `FieldOverrideProvider` implementation is only responsible for looking up
    field overrides. To set overrides, there will be a domain specific API for
    the concrete override implementation being used.
    """
    __metaclass__ = ABCMeta

    def __init__(self, user):
        self.user = user

    @abstractmethod
    def get(self, block, name, default):  # pragma no cover
        """
        Look for an override value for the field named `name` in `block`.
        Returns the overridden value or `default` if no override is found.
        """
        raise NotImplementedError


def _lineage(block):
    """
    Returns an iterator over all ancestors of the given block, starting with
    its immediate parent and ending at the root of the block tree.
    """
    parent = block.get_parent()
    while parent:
        yield parent
        parent = parent.get_parent()
