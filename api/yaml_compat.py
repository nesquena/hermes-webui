"""PyYAML-compatible ``safe_load``/``safe_dump``/``dump`` with a ruamel.yaml fallback.

Hermes Agent's managed dependency environment ships ruamel.yaml only, so WebUI code
imports YAML through this module instead of assuming PyYAML is installed.

The ruamel fallback reproduces PyYAML's behaviour, because the files it reads were
written by PyYAML (and by the Agent's ``hermes_yaml``, which is also YAML 1.1):

* Loading uses PyYAML's own implicit-typing rules (``on``/``off``/``yes``/``no`` are
  booleans, bare ``y``/``n`` stay strings, ``010`` is octal, dates are dates), a
  repeated key keeps its last value, and repeated ``<<`` merge keys merge in order.
  ruamel's defaults differ on each point, and every WebUI load site treats a parse
  error as an empty config, so a later save would overwrite the user's file.
* Dumping uses ruamel's YAML 1.1 resolver (as ``hermes_yaml`` does), which quotes
  every string PyYAML or the Agent could read back as a non-string, so saving
  ``tool_progress: 'off'`` keeps it a string. No ``%YAML`` directive is written.
"""

from __future__ import annotations

import io
import re

try:
    import yaml as _pyyaml
except ImportError:
    _pyyaml = None
    import ruamel.yaml  # noqa: F401  (neither backend -> ImportError, as a bare ``import yaml`` would)

BACKEND = "pyyaml" if _pyyaml is not None else "ruamel"

_YAML11 = (1, 1)

# PyYAML's implicit resolvers (yaml/resolver.py), verbatim, in PyYAML's order.
_PYYAML_IMPLICIT = (
    ("tag:yaml.org,2002:bool",
     r"""^(?:yes|Yes|YES|no|No|NO
        |true|True|TRUE|false|False|FALSE
        |on|On|ON|off|Off|OFF)$""",
     "yYnNtTfFoO"),
    ("tag:yaml.org,2002:float",
     r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
     "-+0123456789."),
    ("tag:yaml.org,2002:int",
     r"""^(?:[-+]?0b[0-1_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+
        |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$""",
     "-+0123456789"),
    ("tag:yaml.org,2002:merge", r"^(?:<<)$", "<"),
    ("tag:yaml.org,2002:null",
     r"""^(?: ~
        |null|Null|NULL
        | )$""",
     ("~", "n", "N", "")),
    ("tag:yaml.org,2002:timestamp",
     r"""^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]
        |[0-9][0-9][0-9][0-9] -[0-9][0-9]? -[0-9][0-9]?
         (?:[Tt]|[ \t]+)[0-9][0-9]?
         :[0-9][0-9] :[0-9][0-9] (?:\.[0-9]*)?
         (?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$""",
     "0123456789"),
    ("tag:yaml.org,2002:value", r"^(?:=)$", "="),
)

_classes: dict = {}


def _ruamel_classes():
    """Build (once) the ruamel resolver/constructor subclasses; None parts if unavailable."""
    if _classes:
        return _classes
    try:
        from ruamel.yaml.constructor import ConstructorError, SafeConstructor
        from ruamel.yaml.nodes import MappingNode, SequenceNode
        from ruamel.yaml.resolver import VersionedResolver
    except ImportError:  # minimal/stub ruamel without these modules
        _classes.update(dump_resolver=None, load_resolver=None, constructor=None)
        return _classes

    class _Yaml11DumpResolver(VersionedResolver):
        @property
        def processing_version(self):
            return _YAML11

    table: dict = {}
    for tag, pattern, first in _PYYAML_IMPLICIT:
        rx = re.compile(pattern, re.X)
        for ch in first:
            table.setdefault(ch, []).append((tag, rx))

    class _PyYamlLoadResolver(VersionedResolver):
        @property
        def versioned_resolver(self):
            return table

    class _PyYamlConstructor(SafeConstructor):
        def check_mapping_key(self, node, key_node, mapping, key, value):
            return True  # a repeated key keeps its last value

        def flatten_mapping(self, node):
            # PyYAML's SafeConstructor.flatten_mapping: repeated `<<` keys merge in order
            # and explicit keys override merged ones.
            merge = []
            index = 0
            while index < len(node.value):
                key_node, value_node = node.value[index]
                if key_node.tag == "tag:yaml.org,2002:merge":
                    del node.value[index]
                    if isinstance(value_node, MappingNode):
                        self.flatten_mapping(value_node)
                        merge.extend(value_node.value)
                    elif isinstance(value_node, SequenceNode):
                        submerge = []
                        for subnode in value_node.value:
                            if not isinstance(subnode, MappingNode):
                                raise ConstructorError(
                                    "while constructing a mapping", node.start_mark,
                                    f"expected a mapping for merging, but found {subnode.id}",
                                    subnode.start_mark)
                            self.flatten_mapping(subnode)
                            submerge.append(subnode.value)
                        submerge.reverse()
                        for value in submerge:
                            merge.extend(value)
                    else:
                        raise ConstructorError(
                            "while constructing a mapping", node.start_mark,
                            "expected a mapping or list of mappings for merging, "
                            f"but found {value_node.id}", value_node.start_mark)
                elif key_node.tag == "tag:yaml.org,2002:value":
                    key_node.tag = "tag:yaml.org,2002:str"
                    index += 1
                else:
                    index += 1
            if merge:
                node.value = merge + node.value

    _classes.update(dump_resolver=_Yaml11DumpResolver, load_resolver=_PyYamlLoadResolver,
                    constructor=_PyYamlConstructor)
    return _classes


def _ruamel(*, default_flow_style=False, allow_unicode=True, sort_keys=True, indent=None, width=None):
    from ruamel.yaml import YAML

    # A fresh instance per call: ruamel YAML objects are not thread-safe.
    y = YAML(typ="safe", pure=True)
    resolver = _ruamel_classes()["dump_resolver"]
    if resolver is not None:
        y.Resolver = resolver
    y.default_flow_style = default_flow_style
    y.allow_unicode = allow_unicode
    y.representer.sort_base_mapping_type_on_output = sort_keys
    if width is not None:
        y.width = width
    return y


def safe_load(stream):
    if _pyyaml is not None:
        return _pyyaml.safe_load(stream)
    from ruamel.yaml import YAML

    y = YAML(typ="safe", pure=True)
    y.version = _YAML11  # YAML 1.1 construction (octal, sexagesimal) like PyYAML
    classes = _ruamel_classes()
    if classes["load_resolver"] is not None:
        y.Resolver = classes["load_resolver"]
        y.Constructor = classes["constructor"]
    return y.load(stream)


def _ruamel_dump(data, stream, **options):
    y = _ruamel(**options)
    if stream is not None:
        y.dump(data, stream)
        return None
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


def safe_dump(data, stream=None, *, default_flow_style=False, allow_unicode=False,
              sort_keys=True, indent=None, width=None):
    if _pyyaml is not None:
        return _pyyaml.safe_dump(
            data, stream, default_flow_style=default_flow_style, allow_unicode=allow_unicode,
            sort_keys=sort_keys, indent=indent, width=width,
        )
    return _ruamel_dump(data, stream, default_flow_style=default_flow_style,
                        allow_unicode=allow_unicode, sort_keys=sort_keys, width=width)


def dump(data, stream=None, *, default_flow_style=False, allow_unicode=False,
         sort_keys=True, indent=None, width=None):
    """``yaml.dump`` on the PyYAML backend, so existing call sites are byte-identical.

    WebUI only dumps plain dict/list/scalar config trees, so the ruamel fallback uses
    the same safe representer as ``safe_dump``.
    """
    if _pyyaml is not None:
        return _pyyaml.dump(
            data, stream, default_flow_style=default_flow_style, allow_unicode=allow_unicode,
            sort_keys=sort_keys, indent=indent, width=width,
        )
    return _ruamel_dump(data, stream, default_flow_style=default_flow_style,
                        allow_unicode=allow_unicode, sort_keys=sort_keys, width=width)
