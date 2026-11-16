"""Launch SSH without a controlling TTY so OpenSSH 7.4 uses private askpass IPC.

The VTE-owned parent forwards termination to its own SSH child and waits for it.
Only public SSH arguments and the broker socket address are inherited.
"""
import os
import signal
import sys


def main():
    if len(sys.argv)<3 or sys.argv[1]!='--':return 2
    child=os.fork()
    if child==0:
        try:
            os.setsid()
            os.execvp(sys.argv[2],sys.argv[2:])
        except Exception:os._exit(127)
    def forward(number,frame):
        try:os.kill(child,number)
        except ProcessLookupError:pass
    for number in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT):signal.signal(number,forward)
    while True:
        try:unused,status=os.waitpid(child,0);break
        except InterruptedError:continue
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128+os.WTERMSIG(status)


if __name__=='__main__':sys.exit(main())
