"""
Implementation of Postgres composite types in Django.

(c) 2016, Danielle Madeley  <danielle@madeley.id.au>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Takes inspiration from:
 - django-pgfields
 - django-postgres
"""

import logging
from collections import OrderedDict

from django.db import connections, migrations, models
from django.db.backends.postgresql.base import \
    DatabaseWrapper as PostgresDatabaseWrapper
from django.db.backends.signals import connection_created
from django.dispatch import Signal
from psycopg2 import ProgrammingError
from psycopg2.extras import register_composite

LOGGER = logging.getLogger(__name__)

__all__ = ['CompositeType']


class BaseField(models.Field):
    """Base class for the field that relates to this type."""

    Meta = None

    def db_type(self, connection):
        LOGGER.debug("db_type")

        if not isinstance(connection, PostgresDatabaseWrapper):
            raise RuntimeError("Composite types are only available "
                               "for postgres")

        return self.Meta.db_type

    def to_python(self, value):
        LOGGER.debug("to_python: > %s", value)

        if isinstance(value, dict):
            value = self.Meta.model(**value)
        else:
            pass

        LOGGER.debug("to_python: < %s", value)

        return value

    def from_db_value(self, value, expression, connection, context):
        """Convert the DB value into a Python type."""
        LOGGER.debug("from_db_value: > %s (%s)", value, type(value))

        if isinstance(value, tuple):
            value = self.Meta.model(*value)
        else:
            pass

        LOGGER.debug("from_db_value: < %s (%s)", value, type(value))

        return value

    def get_prep_value(self, value):
        LOGGER.debug("get_prep_value: > %s (%s)",
                     value, type(value))

        if isinstance(value, dict):
            # Handle dicts because why not?
            value = tuple(
                field.get_prep_value(value.get(name))
                for name, field in self.Meta.fields
            )
        elif isinstance(value, self.Meta.model):
            value = value.__to_tuple__()
        else:
            pass

        LOGGER.debug("get_prep_value: < %s (%s)",
                     value, type(value))

        return value

    def formfield(self, **kwargs):
        """Form field for address."""
        from .forms import CompositeTypeField, CompositeTypeWidget

        defaults = {
            'form_class': CompositeTypeField,
            'model': self.Meta.model,
            'fields': [
                (name, field.formfield())
                for name, field in self.Meta.fields
            ],
        }
        defaults.update(kwargs)

        return super().formfield(**defaults)


class BaseOperation(migrations.operations.base.Operation):
    """Base class for the DB operation that relates to this type."""

    reversible = True
    Meta = None

    def state_forwards(self, app_label, state):
        pass

    def describe(self):
        return 'Creates type %s' % self.Meta.db_type

    def database_forwards(self, app_label, schema_editor,
                          from_state, to_state):

        fields = [
            '%s %s' % (name, field.db_type(schema_editor.connection))
            for name, field in self.Meta.fields
        ]

        schema_editor.execute(' '.join((
            "CREATE TYPE",
            self.Meta.db_type,
            "AS (%s)" % ', '.join(fields),
        )))

        self.Meta.model.register_composite(schema_editor.connection)

        composite_type_created.send(self.Meta.model,
                                    connection=schema_editor.connection)

    def database_backwards(self, app_label, schema_editor,
                           from_state, to_state):
        schema_editor.execute('DROP TYPE %s' % self.Meta.db_type)


class CompositeTypeMeta(type):
    """Metaclass for Type."""

    @classmethod
    def __prepare__(mcs, name, bases):
        """
        Guarantee the ordering of the declared attrs.

        We need this so that our type doesn't change ordering between
        invocations.
        """
        return OrderedDict()

    def __new__(mcs, name, bases, attrs):
        # Only apply the metaclass to our subclasses
        if name == 'CompositeType':
            return super().__new__(mcs, name, bases, attrs)

        # retrieve any fields from our declaration
        fields = []
        for field_name, value in attrs.copy().items():
            if isinstance(value, models.fields.related.RelatedField):
                raise TypeError("Composite types cannot contain "
                                "related fields")
            elif isinstance(value, models.Field):
                field = attrs.pop(field_name)
                field.set_attributes_from_name(field_name)
                fields.append((field_name, field))

        # retrieve the Meta from our declaration
        meta_obj = attrs.pop('Meta', object())
        try:
            meta_obj.db_type
        except AttributeError:
            raise TypeError("Meta.db_type is required.")

        meta_obj.fields = fields

        # create the field for this Type
        attrs['Field'] = type('%sField' % name,
                              (BaseField,),
                              {'Meta': meta_obj})

        # create the database operation for this type
        attrs['Operation'] = type('Create%sType' % name,
                                  (BaseOperation,),
                                  {'Meta': meta_obj})

        new_cls = super().__new__(mcs, name, bases, attrs)
        new_cls._meta = meta_obj

        meta_obj.model = new_cls

        return new_cls

    def __init__(cls, name, bases, attrs):
        super().__init__(name, bases, attrs)
        if name == 'CompositeType':
            return

        # Register the type on the first database connection
        connection_created.connect(receiver=cls.database_connected,
                                   dispatch_uid=cls._meta.db_type)

    def database_connected(cls, signal, sender, connection, **kwargs):
        """
        Register this type with the database the first time a connection is
        made.
        """
        if isinstance(connection, PostgresDatabaseWrapper):
            # Try to register the type. If the type has not been created in a
            # migration, the registration will fail. The type will be
            # registered as part of the migration, so hopefully the migration
            # will run soon.
            try:
                cls.register_composite(connection)
            except ProgrammingError:
                LOGGER.warning(
                    "Failed to register composite %s. This might be because "
                    "the migration to register it has not run yet")

        # Disconnect the signal now - only need to register types on the
        # initial connection
        connection_created.disconnect(cls.database_connected,
                                      dispatch_uid=cls._meta.db_type)

    def register_composite(cls, connection):
        """
        Register this CompositeType with Postgres.

        If the CompositeType does not yet exist in the database, this will
        fail.  Hopefully a migration will come along shortly and create the
        type in the database. If `retry` is True, this CompositeType will try
        to register itself again after the type is created.
        """

        with connection.temporary_connection() as cur:
            register_composite(cls._meta.db_type, cur, globally=True)


# pylint:disable=invalid-name
composite_type_created = Signal()
# pylint:enable=invalid-name


class CompositeType(object, metaclass=CompositeTypeMeta):
    """
    A new composite type stored in Postgres.
    """

    _meta = None

    def __init__(self, *args, **kwargs):
        if args and kwargs:
            raise RuntimeError("Specify either args or kwargs but not both.")

        # Initialise blank values for anyone expecting them
        for name, _ in self._meta.fields:
            setattr(self, name, None)

        # Unpack any args as if they came from the type
        for (name, _), arg in zip(self._meta.fields, args):
            setattr(self, name, arg)

        for name, value in kwargs.items():
            setattr(self, name, value)

    def __repr__(self):
        return '<%s(%s)>' % (
            type(self).__name__,
            ', '.join('%s=%s' % item for item in self.__to_dict__().items()),
        )

    def __to_tuple__(self):
        return tuple(
            field.get_prep_value(getattr(self, name))
            for name, field in self._meta.fields
        )

    def __to_dict__(self):
        return OrderedDict(
            (name, field.get_prep_value(getattr(self, name)))
            for name, field in self._meta.fields
        )

    class Field(BaseField):
        """
        Placeholder for the field that will be produced for this type.
        """

    class Operation(BaseOperation):
        """
        Placeholder for the DB operation that will be produced for this type.
        """
