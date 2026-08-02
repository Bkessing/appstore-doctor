# Shim so `pip install -e .` works on older pip (macOS/Xcode ships pip 21.x,
# which cannot do editable installs from pyproject.toml alone). All real
# configuration lives in pyproject.toml.
from setuptools import setup

setup()
