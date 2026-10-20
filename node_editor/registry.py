"""Registry of node types that appear in the Tab menu."""

NODE_TYPES = {}
# old type names in saved graphs -> today's (not shown in the Tab menu)
ALIASES = {"edit": "edit_media"}


def node_class(type_name):
    """The class for a type name from a file or the clipboard, old names included; None if unknown."""
    return NODE_TYPES.get(ALIASES.get(type_name, type_name))


def register_node(cls):
    """Class decorator: makes a node type available in the Tab menu."""
    NODE_TYPES[cls.type_name] = cls
    return cls
