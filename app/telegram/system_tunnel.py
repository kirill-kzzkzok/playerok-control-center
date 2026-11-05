"""Windows Happ admission and interface-bound Telegram sockets; no route fallback."""

import asyncio
import ctypes
import os
import socket
import sys
from pathlib import Path

from telethon.network.connection import ConnectionTcpFull


def happ_required(mode):
    # Keep portable direct transport unchanged on hosts without this installation.
    return (
        mode == "direct"
        and sys.platform == "win32"
        and (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "FlyFrogLLC/Happ/Happ.exe").is_file()
    )


def interface():
    from ctypes import wintypes as w

    class Row(ctypes.Structure):
        _fields_ = (
            [("name", w.WCHAR * 256)]
            + [(name, w.DWORD) for name in ("index", "type", "mtu", "speed", "address_length")]
            + [("address", w.BYTE * 8)]
            + [
                (name, w.DWORD)
                for name in (
                    "admin",
                    "oper",
                    "last_change",
                    "in_octets",
                    "in_unicast",
                    "in_other",
                    "in_discards",
                    "in_errors",
                    "in_unknown",
                    "out_octets",
                    "out_unicast",
                    "out_other",
                    "out_discards",
                    "out_errors",
                    "out_queue",
                    "description_length",
                )
            ]
            + [("description", w.BYTE * 256)]
        )

    api = ctypes.WinDLL("iphlpapi")
    luid, index = ctypes.c_ulonglong(), w.DWORD()
    if api.ConvertInterfaceAliasToLuid(ctypes.c_wchar_p("happ-xray"), ctypes.byref(luid)):
        raise OSError("Happ tunnel unavailable")
    if api.ConvertInterfaceLuidToIndex(ctypes.byref(luid), ctypes.byref(index)):
        raise OSError("Happ tunnel unavailable")
    row = Row(index=index.value)
    if api.GetIfEntry(ctypes.byref(row)) or row.admin != 1 or row.oper != 5:
        raise OSError("Happ tunnel unavailable")
    return index.value, luid.value


class HappRoute:
    def check(self):
        return interface()[1]

    def socket(self, address_info):
        index, _ = interface()
        family, kind, protocol = address_info[:3]
        sock = socket.socket(family, kind, protocol)
        try:
            if family == socket.AF_INET:
                # Winsock requires IP_UNICAST_IF in NETWORK byte order.
                sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(index))
            elif family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, 31, index)
            else:
                raise OSError("Telegram address family unavailable")
            return sock
        except BaseException:
            sock.close()
            raise


class HappConnectionTcpFull(ConnectionTcpFull):
    async def _connect(self, timeout=None, ssl=None):
        # Telethon's direct open_connection has no socket factory. Keep its codec
        # lifecycle, but give it a socket whose interface Windows cannot fail over.
        if self._proxy or ssl or self._local_addr:
            raise OSError("Unexpected Happ connection transport")
        family = socket.AF_INET6 if ":" in self._ip else socket.AF_INET
        sock = HappRoute().socket((family, socket.SOCK_STREAM, socket.IPPROTO_TCP))
        sock.setblocking(False)
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(sock, (self._ip, self._port)), timeout=timeout
            )
            self._reader, self._writer = await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()
