"""Built-in program interfaces. Importing this package registers them all.

These are hand-written (System/Token use bincode/packed layouts, not Borsh) and
register into the same global registry that IDL-generated modules target.
"""

from . import ata, system, token  # noqa: F401  (import side effect: registration)
