import dataclasses
import enum
import inspect
import threading
import typing as tp
from abc import ABCMeta
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

import jax
import jax.tree_util

from treeo import types, utils

T = tp.TypeVar("T", bound="Tree")
A = tp.TypeVar("A")


class FlattenMode(enum.Enum):
    no_fields = enum.auto()
    normal = enum.auto()
    all_fields = enum.auto()


@dataclass
class _Context(threading.local):
    add_field_info: bool = False
    flatten_mode: tp.Optional[FlattenMode] = None

    def __enter__(self):
        global _CONTEXT
        self._old_context = _CONTEXT
        _CONTEXT = self

    def __exit__(self, *args):
        global _CONTEXT
        _CONTEXT = self._old_context

    @contextmanager
    def update(self, **kwargs):
        fields = vars(self).copy()
        fields.pop("_old_context", None)
        fields.update(kwargs)

        with _Context(**fields):
            yield


_CONTEXT = _Context()


class FieldInfo:
    def __init__(
        self,
        name: tp.Optional[str],
        value: tp.Any,
        kind: tp.Type[tp.Any],
        module: tp.Optional["Tree"],
    ):
        self.name = name
        self.value = value
        self.kind = kind
        self.module = module

    def __repr__(self) -> str:
        return f"FieldInfo(name={self.name!r}, value={self.value!r}, kind={self.kind!r}, module={type(self.module).__name__})"


class TreeMeta(ABCMeta):
    def __call__(cls, *args, **kwargs) -> "Tree":
        obj: Tree = cls.__new__(cls)

        obj._field_metadata = obj._field_metadata.copy()

        if not dataclasses.is_dataclass(cls):
            for field, default_factory in obj._factory_fields.items():
                setattr(obj, field, default_factory())

            for field, default_value in obj._default_field_values.items():
                setattr(obj, field, default_value)

        obj.__init__(*args, **kwargs)

        # auto-annotations
        obj._update_local_metadata()

        return obj


class Tree(metaclass=TreeMeta):
    _field_metadata: tp.Dict[str, types.FieldMetadata]
    _factory_fields: tp.Dict[str, tp.Callable[[], tp.Any]]
    _default_field_values: tp.Dict[str, tp.Any]

    @property
    def field_metadata(self) -> tp.Mapping[str, types.FieldMetadata]:
        return MappingProxyType(self._field_metadata)

    def update_field_metadata(
        self: T,
        field: str,
        node: tp.Optional[bool] = None,
        kind: tp.Optional[type] = None,
        opaque: tp.Optional[bool] = None,
        opaque_is_equal: tp.Union[
            tp.Callable[[utils.Opaque, tp.Any], bool],
            None,
            types.Missing,
        ] = types.MISSING,
    ) -> T:
        module = copy(self)

        field_metadata = module._field_metadata[field]
        updates = {}

        if node is not None:
            updates.update(node=node)

        if kind is not None:
            updates.update(kind=kind)

        if opaque is not None:
            updates.update(opaque=opaque)

        if opaque_is_equal is not types.MISSING:
            updates.update(opaque_is_equal=opaque_is_equal)

        if updates:
            field_metadata = field_metadata.update(**updates)

        module._field_metadata[field] = field_metadata

        return module

    def check_metadata_updates(self):
        """
        Checks for new fields, if found, adds them to the metadata.
        """
        with _CONTEXT.update(flatten_mode=FlattenMode.all_fields):
            jax.tree_flatten(self)

    def _update_local_metadata(self):
        for field, value in vars(self).items():

            if field not in self._field_metadata:
                self._field_metadata[field] = types.FieldMetadata(
                    node=isinstance(value, Tree),
                    kind=type(value),
                    opaque=False,
                    opaque_is_equal=None,
                )

    def __init_subclass__(cls):
        jax.tree_util.register_pytree_node_class(cls)

        # Restore the signature
        sig = inspect.signature(cls.__init__)
        parameters = tuple(sig.parameters.values())
        cls.__signature__ = sig.replace(parameters=parameters[1:])

        annotations = utils._get_all_annotations(cls)
        class_vars = utils._get_all_vars(cls)
        cls._field_metadata = {}
        cls._factory_fields = {}
        cls._default_field_values = {}

        for field, value in class_vars.items():
            if isinstance(value, dataclasses.Field):

                # save defaults
                if value.default is not dataclasses.MISSING:
                    cls._default_field_values[field] = value.default
                elif value.default_factory is not dataclasses.MISSING:
                    cls._factory_fields[field] = value.default_factory

                # extract metadata
                if value.metadata is not None and "node" in value.metadata:
                    cls._field_metadata[field] = types.FieldMetadata(
                        node=value.metadata["node"],
                        kind=value.metadata["kind"],
                        opaque=value.metadata["opaque"],
                        opaque_is_equal=value.metadata["opaque_is_equal"],
                    )

        for field, value in annotations.items():

            if field not in cls._field_metadata:
                is_node = any(issubclass(t, Tree) for t in utils._all_types(value))
                cls._field_metadata[field] = types.FieldMetadata(
                    node=is_node,
                    kind=type(None),
                    opaque=False,
                    opaque_is_equal=None,
                )

    def tree_flatten(self):

        fields = vars(self).copy()

        node_fields = {}
        static_fields = {}

        # auto-annotations
        self._update_local_metadata()

        if _CONTEXT.flatten_mode == FlattenMode.all_fields:
            node_fields = fields
        elif _CONTEXT.flatten_mode == FlattenMode.no_fields:
            static_fields = fields
        else:  # normal or None
            for field, value in fields.items():
                field_annotation = self._field_metadata[field]

                if field_annotation.node:
                    node_fields[field] = value
                elif not field_annotation.node and field_annotation.opaque:
                    static_fields[field] = utils.Opaque(
                        value,
                        opaque_is_equal=field_annotation.opaque_is_equal,
                    )
                else:
                    static_fields[field] = value

        # maybe convert to FieldInfo
        if _CONTEXT.add_field_info:
            for field in node_fields.keys():
                if field in TREE_PRIVATE_FIELDS:
                    continue

                kind = self._field_metadata[field].kind
                # leaves, treedef
                node_fields[field] = jax.tree_map(
                    lambda x: FieldInfo(
                        name=field,
                        value=x,
                        kind=kind,
                        module=self,
                    )
                    if not isinstance(x, Tree)
                    else x,
                    node_fields[field],
                    is_leaf=lambda x: isinstance(x, Tree),
                )

        children = (node_fields,)

        return children, static_fields

    @classmethod
    def tree_unflatten(cls, static_fields, children):

        module = cls.__new__(cls)
        (node_fields,) = children

        if _CONTEXT.add_field_info:
            for field in node_fields.keys():
                node_fields[field] = jax.tree_map(
                    lambda x: x.value if isinstance(x, FieldInfo) else x,
                    node_fields[field],
                    is_leaf=lambda x: isinstance(x, Tree),
                )

        module.__dict__.update(node_fields, **static_fields)

        # extract value from Opaque
        for field, value in static_fields.items():
            if (
                isinstance(value, utils.Opaque)
                and field in module._field_metadata
                and module._field_metadata[field].opaque
            ):
                setattr(module, field, value.value)

        return module


TREE_PRIVATE_FIELDS = {
    x for x in list(vars(Tree)) + list(Tree.__annotations__) if x.startswith("_")
}


def copy(obj: A) -> A:
    """
    Returns a deep copy of the tree, almost equivalent to:
    ```python
    jax.tree_map(lambda x: x, self)
    ```
    but will try to copy static nodes as well.
    """
    with _CONTEXT.update(flatten_mode=FlattenMode.all_fields):
        return jax.tree_map(lambda x: x, obj)
