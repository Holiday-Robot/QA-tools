"""SocketCAN helpers: raw CAN FD sockets with hardware timestamps, drop counters and frame packing. Linux only."""
from __future__ import annotations

import socket
import struct
import time

SOL_CAN_RAW = 101
CAN_RAW_ERR_FILTER = 2
CAN_RAW_RECV_OWN_MSGS = 4
CAN_RAW_FD_FRAMES = 5
SO_TIMESTAMPING = getattr(socket, "SO_TIMESTAMPING", 37)
SCM_TIMESTAMPING = SO_TIMESTAMPING
SO_RXQ_OVFL = getattr(socket, "SO_RXQ_OVFL", 40)
SOF_RX_HARDWARE = 1 << 2
SOF_RX_SOFTWARE = 1 << 3
SOF_SOFTWARE = 1 << 4
SOF_RAW_HARDWARE = 1 << 6
CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG = 0x80000000, 0x40000000, 0x20000000
CAN_ERR_MASK = 0x1FFFFFFF
CANFD_BRS, CANFD_ESI = 0x01, 0x02
FRAME = struct.Struct("=IBBBB64s")   # struct canfd_frame, 72 bytes
CLASSIC = struct.Struct("=IBBBB8s")  # struct can_frame, 16 bytes
MSG_DONTROUTE, MSG_CONFIRM = 0x04, 0x800   # recvmsg flags: sent from this host / sent by this socket
TS = struct.Struct("=qqqqqq")        # struct scm_timestamping: software, legacy, raw hardware
U32 = struct.Struct("=I")
ANC_SPACE = socket.CMSG_SPACE(TS.size) + socket.CMSG_SPACE(U32.size) if hasattr(socket, "CMSG_SPACE") else 128
COLUMNS = ["t_hw", "t_sw", "iface", "can_id", "ext", "fd", "brs", "esi", "err", "len", "data"]
LOG_COLUMNS = COLUMNS + ["dir"]       # dir is "tx" for frames this PC sent, empty for received ones


def open_socket(iface, blocking=True, own=False):
    """Raw socket on one interface: CAN FD frames, error frames, hardware timestamps, kernel drop counter.

    own=True also delivers the frames this socket sends, flagged MSG_CONFIRM, so they can be logged as Tx.
    """
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    s.setsockopt(SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1)
    s.setsockopt(SOL_CAN_RAW, CAN_RAW_ERR_FILTER, struct.pack("=I", CAN_ERR_MASK))
    if own:
        s.setsockopt(SOL_CAN_RAW, CAN_RAW_RECV_OWN_MSGS, 1)
    s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPING,
                 SOF_RX_HARDWARE | SOF_RAW_HARDWARE | SOF_RX_SOFTWARE | SOF_SOFTWARE)
    s.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    s.bind((iface,))
    s.setblocking(blocking)
    return s


def parse_frame(raw):
    """Return a dict for one canfd_frame / can_frame buffer."""
    fd = len(raw) == FRAME.size
    raw = raw.ljust(FRAME.size, b"\0")
    can_id, length, flags, _, _, data = FRAME.unpack(raw)
    return dict(can_id=can_id & (CAN_ERR_MASK if can_id & CAN_EFF_FLAG else 0x7FF),
                ext=int(bool(can_id & CAN_EFF_FLAG)), fd=int(fd),
                brs=int(bool(fd and flags & CANFD_BRS)), esi=int(bool(fd and flags & CANFD_ESI)),
                err=int(bool(can_id & CAN_ERR_FLAG)), len=length, data=data[:length].hex())


def parse_anc(ancdata):
    """Return (hardware time or None, software time, frames dropped by the kernel so far or None)."""
    hw, sw, dropped = None, None, None
    for level, kind, payload in ancdata:
        if level != socket.SOL_SOCKET:
            continue
        if kind == SCM_TIMESTAMPING and len(payload) >= TS.size:
            sw_s, sw_ns, _, _, hw_s, hw_ns = TS.unpack_from(payload)
            hw = hw_s + hw_ns * 1e-9 if (hw_s or hw_ns) else None
            sw = sw_s + sw_ns * 1e-9 if (sw_s or sw_ns) else None
        elif kind == SO_RXQ_OVFL and len(payload) >= U32.size:
            dropped = U32.unpack_from(payload)[0]
    return hw, sw if sw is not None else time.time(), dropped


def parse_ts(ancdata):
    """Return (hardware time or None, software time)."""
    hw, sw, _ = parse_anc(ancdata)
    return hw, sw


def pack_frame(can_id, ext, fd, brs, data):
    """Bytes of a canfd_frame (fd) or can_frame to write to a raw socket."""
    cid = (can_id & CAN_ERR_MASK) | CAN_EFF_FLAG if ext else can_id & 0x7FF
    if fd:
        return FRAME.pack(cid, len(data), CANFD_BRS if brs else 0, 0, 0, bytes(data).ljust(64, b"\0"))
    return CLASSIC.pack(cid, len(data), 0, 0, 0, bytes(data).ljust(8, b"\0"))
