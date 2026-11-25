"""Local authenticated request/reply transport for notification producers (Linux)."""
import array
import contextlib
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading

_PACKET = 64 * 1024


def peer_uid(sock):
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def _pack(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(raw) > _PACKET:
        raise ValueError('Message too large')
    return raw


def _receive(sock):
    raw, control, flags, _ = sock.recvmsg(_PACKET, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
    fds = array.array('i')
    for level, kind, data in control:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data)
    try:
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(fds) > 1:
            raise ValueError('Invalid packet')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid object')
        return result, fds.pop() if fds else None
    finally:
        for fd in fds:
            os.close(fd)


def exchange(path, uid, payload, stream=None, timeout=15):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        if peer_uid(sock) != uid:
            raise PermissionError('Unexpected socket owner')
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [stream.fileno()]))] if stream else []
        sock.sendmsg([_pack(payload)], ancillary)
        reply, fd = _receive(sock)
        if fd is not None:
            os.close(fd)
            raise ValueError('Unexpected attachment')
        return reply


def _serve_one(sock, permitted, handle, permits):
    with sock, contextlib.ExitStack() as stack:
        stack.callback(permits.release)
        try:
            sock.settimeout(10)
            uid = peer_uid(sock)
            if uid not in permitted:
                return
            payload, fd = _receive(sock)
            if fd is not None:
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                    raise ValueError('Invalid attachment')
            result = handle(uid, payload, fd)
        except Exception:
            result = {'ok': False, 'error': 'unavailable'}
        with contextlib.suppress(OSError):
            sock.sendall(_pack(result))


def serve(path, permitted, handle):
    """The socket parent must be owned by the daemon and not group/world writable."""
    path = Path(path)
    parent = path.parent.stat()
    if not path.is_absolute() or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise ValueError('Socket requires a protected absolute runtime directory')
    with open(str(path) + '.lock', 'a') as lock, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ValueError('Unexpected runtime file')
            path.unlink()
        server.bind(str(path))
        try:
            path.chmod(0o660)
            server.listen(16)
            permits = threading.BoundedSemaphore(16)
            while True:
                permits.acquire()
                client, _ = server.accept()
                threading.Thread(target=_serve_one, args=(client, permitted, handle, permits), daemon=True).start()
        finally:
            path.unlink(missing_ok=True)
