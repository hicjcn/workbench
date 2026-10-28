"""Launch and observe a configured service independently of the image's PID 1."""
import re

from .container_shell import shell_argv
from .workspace import WorkbenchError


# Fixed scripts only: the configured executable and arguments remain separate
# argv entries. The supervisor survives the CLI long enough to record its exit.
LAUNCH_SCRIPT = r'''
declare -r directory="$1"
shift
umask 077
/bin/mkdir -- "$directory" || exit 125
exec >"$directory/log" 2>&1
"$@" &
child=$!
printf '%s\n' "$child" >"$directory/pid"
wait "$child"
code=$?
printf '%s\n' "$code" >"$directory/exit.pending"
/bin/mv -- "$directory/exit.pending" "$directory/exit"
exit "$code"
'''

STATUS_SCRIPT = r'''
declare -r directory="$1"
if [[ -r "$directory/exit" ]]; then
    IFS= read -r code <"$directory/exit"
    printf 'exited:%s\n' "$code"
elif [[ -r "$directory/pid" ]]; then
    IFS= read -r child <"$directory/pid"
    if [[ "$child" =~ ^[1-9][0-9]*$ ]] && kill -0 "$child" 2>/dev/null; then
        printf 'running:\n'
    else
        printf 'unknown:\n'
    fi
else
    printf 'starting:\n'
fi
if [[ "$2" == logs && -r "$directory/log" ]]; then
    /usr/bin/tail -c 8000 -- "$directory/log"
fi
'''


def checked_directory(directory):
    if not isinstance(directory, str) or not re.fullmatch(r"/tmp/awb-agent-[a-f0-9]{16}", directory):
        raise WorkbenchError("Agent 服务运行目录不正确")
    return directory


def launch_argv(argv, *, directory, root=False):
    return ["/bin/bash", "--noprofile", "--norc", "-c", LAUNCH_SCRIPT,
            "workbench-agent", checked_directory(directory), *shell_argv(argv, root=root)]


def status_argv(directory, *, logs=False):
    return ["/bin/bash", "--noprofile", "--norc", "-c", STATUS_SCRIPT,
            "workbench-agent-status", checked_directory(directory), "logs" if logs else "status"]


def parse_status(output, *, logs=False):
    status, _, tail = output.partition("\n")
    match = re.fullmatch(r"(starting|running|exited|unknown):(\d{1,3})?", status)
    if (not match or (match[1] == "exited" and (match[2] is None or int(match[2]) > 255)) or
        (match[1] != "exited" and match[2] is not None)):
        raise WorkbenchError("无法读取 Agent 服务进程状态")
    result = {"agent_status": match[1], "agent_exit_code": int(match[2]) if match[2] else None}
    if logs: result["log_tail"] = tail[-8000:]
    return result
