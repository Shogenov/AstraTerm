"""Give only the owned FreeRDP child a private controlling terminal for prompts."""
import fcntl
import os
import sys
import termios

if __name__ == '__main__':
    if len(sys.argv) < 3 or sys.argv[1] != '--':
        sys.exit(2)
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(sys.argv[2], sys.argv[2:])
