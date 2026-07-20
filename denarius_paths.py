import os
from pathlib import Path


STATE_DIRECTORY_ENV = "DENARIUS_STATE_DIR"


def state_directory():
    configured = os.environ.get(STATE_DIRECTORY_ENV)
    directory = Path(configured).expanduser() if configured else Path.cwd() / "states"
    return directory.resolve()


def state_path(filename):
    return state_directory() / filename
