"""POSIX/Windows boundaries: locks, durable renames, private files, processes and UTF-8 I/O."""
import contextlib
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import shutil
import subprocess
import sys
import time

WINDOWS = os.name == 'nt'


# ---------------------------------------------------------------- advisory file locks
if WINDOWS:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(_Overlapped)]
    # One byte far past any content: Windows range locks are mandatory, so this keeps them advisory.
    _OFFSET_HIGH = 0x7FFFFFFF
else:
    import fcntl


def lock(handle, shared=False, blocking=True):
    """flock-equivalent per open file; BlockingIOError when non-blocking and already held."""
    if not WINDOWS:
        fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB))
        return
    flags = (0 if shared else 0x2) | (0 if blocking else 0x1)
    if not _kernel32.LockFileEx(msvcrt.get_osfhandle(handle.fileno()), flags, 0, 1, 0,
                                ctypes.byref(_Overlapped(OffsetHigh=_OFFSET_HIGH))):
        error = ctypes.get_last_error()
        if error == 33: raise BlockingIOError('lock is held by another owner')
        raise ctypes.WinError(error)


def unlock(handle):
    if not WINDOWS:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    if not _kernel32.UnlockFileEx(msvcrt.get_osfhandle(handle.fileno()), 0, 1, 0,
                                  ctypes.byref(_Overlapped(OffsetHigh=_OFFSET_HIGH))):
        error = ctypes.get_last_error()
        if error != 158: raise ctypes.WinError(error)


@contextlib.contextmanager
def locked(path, shared=False, mode='a'):
    with Path(path).open(mode) as handle:
        lock(handle, shared)
        try: yield handle
        finally: unlock(handle)


# ---------------------------------------------------------------- durable files
def fsync_directory(path):
    """POSIX: persist a rename in its directory. Windows cannot open directories for fsync;
    NTFS journals the rename (MoveFileEx) itself."""
    if WINDOWS: return
    directory = os.open(path, os.O_RDONLY)
    try: os.fsync(directory)
    finally: os.close(directory)


def replace(source, target):
    """os.replace; on Windows retry briefly while a reader holds the target open."""
    for attempt in range(50):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not WINDOWS or attempt == 49: raise
            time.sleep(.02)


def _current_user_sid():
    out = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True).stdout
    return out.strip().rsplit(',', 1)[-1].strip('"')


def make_private(path):
    """Owner-only access: mode 0600 on POSIX, a protected owner-only ACL on Windows."""
    if not WINDOWS:
        os.chmod(path, 0o600)
        return
    subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', '*' + _current_user_sid() + ':F'],
                   capture_output=True, check=True)


_ACL_PROBE = r'''
$acl = Get-Acl -LiteralPath $env:PERSONAGRAPH_ACL_PATH
$sids = @($acl.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
@{protected = $acl.AreAccessRulesProtected; sids = $sids} | ConvertTo-Json -Compress
'''


def is_private(path):
    path = Path(path)
    if path.is_symlink(): return False
    if not WINDOWS: return not path.stat().st_mode & 0o077
    # Passed through the environment: arguments after -Command are not bound to $args.
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', _ACL_PROBE],
                            env={**os.environ, 'PERSONAGRAPH_ACL_PATH': str(path)},
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    if result.returncode: return False
    acl = json.loads(result.stdout)
    sids = acl['sids'] if isinstance(acl['sids'], list) else [acl['sids']]
    # Owner, LocalSystem and Administrators only, with inheritance cut.
    return bool(acl['protected']) and set(sids) <= {_current_user_sid(), 'S-1-5-18', 'S-1-5-32-544'}


# ---------------------------------------------------------------- processes and interpreters
def detached():
    """Popen arguments for a background worker that outlives the hook that started it."""
    if WINDOWS: return {'creationflags': 0x00000200 | 0x08000000}  # new process group, no window
    return {'start_new_session': True}


def venv_python(venv):
    return Path(venv) / ('Scripts/python.exe' if WINDOWS else 'bin/python')


def executable(path):
    path = Path(path)
    return path.with_name(path.name + '.exe') if WINDOWS else path


def command_line(arguments):
    """A hook/status-line command string for the host's shell.

    POSIX hosts run hooks through sh. Claude Code and Codex on Windows may use Git Bash, cmd or
    PowerShell; unquoted forward-slash paths run unchanged in all three. A path with spaces
    (C:/Program Files/...) uses its 8.3 short name, or double quotes when none exists."""
    if not WINDOWS: return shlex.join(arguments)
    parts = []
    for argument in arguments:
        if PureWindowsPath(argument).is_absolute():
            if ' ' in argument:
                buffer = ctypes.create_unicode_buffer(32768)
                if _kernel32.GetShortPathNameW(str(argument), buffer, len(buffer)): argument = buffer.value
            argument = Path(argument).as_posix()
        parts.append('"' + argument + '"' if ' ' in argument else argument)
    return ' '.join(parts)


def git_bash():
    """Claude Code's Windows shell for hooks and status lines: CLAUDE_CODE_GIT_BASH_PATH or Git's bash."""
    configured = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    if configured and Path(configured).is_file(): return configured
    git = shutil.which('git')
    if git:
        for candidate in (Path(git).parents[1] / 'bin/bash.exe', Path(git).parents[1] / 'usr/bin/bash.exe'):
            if candidate.is_file(): return str(candidate)
    return None


def shell(command):
    """argv that runs a stored shell command the way the host would."""
    if not WINDOWS: return ['/bin/sh', '-c', command]
    bash = git_bash()
    return [bash, '-c', command] if bash else ['cmd', '/d', '/s', '/c', command]


def utf8_stdio():
    """Hosts exchange UTF-8 JSON; Windows pipes otherwise use the ANSI code page."""
    if not WINDOWS: return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')


# ---------------------------------------------------------------- restic snapshot paths
def snapshot_subtree(path):
    """restic's in-snapshot path for a backed-up root: /C/Users/... on Windows."""
    path = Path(path)
    if not WINDOWS: return path.as_posix()
    return '/' + path.drive.rstrip(':') + '/' + path.relative_to(path.anchor).as_posix()
