"""Load secrets and machine-specific values from env files, for every way the pipeline is started.

The files are `$JOB_SEARCH_ENV_FILE` (optional, any path) and then `.env` in the repository root, so a value in
`.env` wins. Values in the files win over the process environment too, as when a shell `source`s them.
Kept free of pipeline imports: it runs before config.py reads JOB_PIPELINE_CONFIG.
"""
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_files(environ=None):
    """The env files to read, in order (later ones win)."""
    extra = (os.environ if environ is None else environ).get("JOB_SEARCH_ENV_FILE")
    return ([os.path.expanduser(extra)] if extra else []) + [os.path.join(ROOT_DIR, ".env")]


def read_env_file(path):
    """KEY=VALUE pairs from a shell-style env file (comments, `export` and quotes allowed)."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.removeprefix("export ").partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except OSError:
        pass
    return values


def effective_env(environ=None, files=None):
    """The process environment with the env files laid over it."""
    env = dict(os.environ if environ is None else environ)
    for path in env_files(env) if files is None else files:
        env.update(read_env_file(path))
    return env


def load_env_files():
    """Apply the env files to os.environ."""
    os.environ.update(effective_env())
