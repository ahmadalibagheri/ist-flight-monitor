"""Bundled data files.

Contains the seed observation history that :mod:`app.services.seed` imports on a
fresh database. This package exists so the file is addressable through
``importlib.resources`` and therefore ships inside the installed package and the
Docker image, rather than depending on a path relative to the source tree.
"""
