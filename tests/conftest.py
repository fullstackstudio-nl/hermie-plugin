"""Load the plugin the way Hermes does: as a package whose root is this repo.

Hermes imports a plugin directory under a module name of its own choosing, so
the repo root IS the package and every module inside it uses relative imports.
The tests build the same package rather than inventing an import path that
production never uses — which is also what keeps `from .. import contract` in
the modules honest.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "hermie_plugin"

if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    # In sys.modules BEFORE execution, so the package's own relative imports
    # resolve while it is still being built.
    sys.modules[NAME] = module
    spec.loader.exec_module(module)
