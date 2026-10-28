"""Load a container's Bash environment without interpreting command arguments."""
from collections.abc import Sequence
from pathlib import Path

ROOT_BASHRC = "/root/.bashrc"

# Interactive Bash honors the common `case $- in *i*) ...` bashrc guard. Its
# terminal/job-control warnings are startup noise, so restore stderr only once
# that startup has completed. The requested program retains its original fds.
_OUTER = r'''AWB_BOOTSTRAP_SCRIPT=$1
shift
exec 3>&2 2>/dev/null
exec /bin/bash --noprofile --norc -ic "$AWB_BOOTSTRAP_SCRIPT" workbench-shell "$@"
'''

_INNER = r'''exec 2>&3 3>&-
declare -ar AWB_BOOTSTRAP_ARGV=("${@:3}")
declare -r AWB_BOOTSTRAP_PWD="$PWD"
declare -r AWB_BOOTSTRAP_HOME="${HOME-}"
declare -r AWB_BOOTSTRAP_HOME_SET="${HOME+x}"
declare -r AWB_BOOTSTRAP_ROOT="$1"
declare -A AWB_BOOTSTRAP_ENV=()
for AWB_BOOTSTRAP_KEY in OPENCODE_SERVER_USERNAME OPENCODE_SERVER_PASSWORD \
    PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD \
    NODE_EXTRA_CA_CERTS SSL_CERT_FILE GIT_SSL_CAINFO \
    OPENAI_API_KEY OPENROUTER_API_KEY ANTHROPIC_API_KEY \
    GIT_CONFIG_COUNT "${!GIT_CONFIG_KEY_@}" "${!GIT_CONFIG_VALUE_@}"; do
    if [[ -v "$AWB_BOOTSTRAP_KEY" ]]; then
        AWB_BOOTSTRAP_ENV["$AWB_BOOTSTRAP_KEY"]="${!AWB_BOOTSTRAP_KEY}"
    fi
done
declare -r AWB_BOOTSTRAP_ENV
if [[ "$AWB_BOOTSTRAP_ROOT" == root ]]; then
    AWB_BOOTSTRAP_RC=$2
    if [[ ! -r "$AWB_BOOTSTRAP_RC" ]]; then
        builtin printf 'Cannot load container startup file %s: missing or not readable.\n' "$AWB_BOOTSTRAP_RC" >&2
        builtin exit 126 2>/dev/null
    fi
    builtin export HOME=/root
else
    AWB_BOOTSTRAP_RC="${HOME-}/.bashrc"
    if [[ -z "${HOME-}" || ! -r "$AWB_BOOTSTRAP_RC" ]]; then
        AWB_BOOTSTRAP_RC=
    fi
fi
if [[ -n "$AWB_BOOTSTRAP_RC" ]]; then
    if source "$AWB_BOOTSTRAP_RC" </dev/null >/dev/null 2>&1; then
        :
    else
        AWB_BOOTSTRAP_STATUS=$?
        builtin printf 'Cannot load container startup file %s (exit %s).\n' "$AWB_BOOTSTRAP_RC" "$AWB_BOOTSTRAP_STATUS" >&2
        builtin exit "$AWB_BOOTSTRAP_STATUS" 2>/dev/null
    fi
fi
for AWB_BOOTSTRAP_KEY in "${!AWB_BOOTSTRAP_ENV[@]}"; do
    if ! builtin export "$AWB_BOOTSTRAP_KEY=${AWB_BOOTSTRAP_ENV[$AWB_BOOTSTRAP_KEY]}" 2>/dev/null; then
        builtin printf 'Cannot restore injected container variable %s.\n' "$AWB_BOOTSTRAP_KEY" >&2
        builtin exit 126 2>/dev/null
    fi
done
if [[ -n "$AWB_BOOTSTRAP_HOME_SET" ]]; then
    if ! builtin export HOME="$AWB_BOOTSTRAP_HOME" 2>/dev/null; then
        builtin printf 'Cannot restore container HOME after startup initialization.\n' >&2
        builtin exit 126 2>/dev/null
    fi
else
    if ! builtin unset HOME 2>/dev/null; then
        builtin printf 'Cannot restore container HOME after startup initialization.\n' >&2
        builtin exit 126 2>/dev/null
    fi
fi
builtin cd -- "$AWB_BOOTSTRAP_PWD" || builtin exit
if [[ "$AWB_BOOTSTRAP_ROOT" == root ]]; then
    builtin umask 0002
fi
builtin exec -- "${AWB_BOOTSTRAP_ARGV[@]}"
'''


def shell_argv(argv: Sequence[str], *, root: bool = False) -> list[str]:
    """Return a fixed Bash bootstrap followed by untouched executable argv.

    This helper does not choose or elevate the container user. Root execution
    requires /root/.bashrc; ordinary execution optionally reads $HOME/.bashrc.
    Initialization cannot consume task stdin or pollute its structured output.
    """
    if isinstance(argv, (str, bytes)) or not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise ValueError("容器命令必须是非空参数列表，且不能包含 NUL")
    if not argv[0]:
        raise ValueError("容器命令入口不能为空")
    return ["/bin/bash", "--noprofile", "--norc", "-c", _OUTER,
        "workbench-shell", _INNER, "root" if root else "user", ROOT_BASHRC if root else "", *argv]


def git_safe_environment(workspace: Path, context_mounts: Sequence[dict] = ()) -> dict[str, str]:
    """Trust only exported repositories whose host owner differs from root."""
    from .multi_repo import layout

    # The empty first entry resets any inherited wildcard trust from the image.
    directories = [""]
    for mount in [{"source": workspace / "repo", "target": "/workspace/project"}, *context_mounts]:
        target = mount["target"]
        directories.append(target)
        selected = layout(Path(mount["source"]))
        if selected:
            directories.extend(target + "/" + item["path"] for item in selected["repositories"])
    directories = list(dict.fromkeys(directories))
    environment = {"GIT_CONFIG_COUNT": str(len(directories))}
    for index, directory in enumerate(directories):
        environment[f"GIT_CONFIG_KEY_{index}"] = "safe.directory"
        environment[f"GIT_CONFIG_VALUE_{index}"] = directory
    return environment
