"""YouTube egress proxy for A1: an HTTP CONNECT proxy run on a home PC, reached over Tailscale.

YouTube answers cloud IPs with "Sign in to confirm you're not a bot", so A1's yt-dlp sends YouTube
traffic here (YTDLP_YOUTUBE_PROXY) and it leaves through the home connection instead. A1 serves a
public MCP, so assume A1 can be compromised: this proxy must never become a way into the home
network, the tailnet or the wider internet. Therefore:

- only the listed client IPs (A1's Tailscale address) get any reply; everyone else is dropped;
- only CONNECT to port 443 of strict ASCII host names under the YouTube allowlist (no IP literals,
  no other Google services);
- names are resolved here and every answer must be a public unicast address (not private, loopback,
  link-local, Tailscale 100.64.0.0/10, 6to4 or NAT64); the tunnel connects to that validated address;
- at most MAX_TUNNELS tunnels, closed after IDLE_TIMEOUT without traffic or MAX_LIFETIME in total.

Standard library only. Usage (see docs/gitops.md):
  python egress_proxy.py --listen 100.74.111.42:18443 --allow-client 100.82.40.33 --log C:\\path\\egress.log
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import logging.handlers
import re
import socket
import time

ALLOWED_SUFFIXES = ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com")
ALLOWED_HOSTS = {"youtubei.googleapis.com"}
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(r"(?=.{1,253}$)" + LABEL + r"(?:\." + LABEL + r")+")
BLOCKED_NETWORKS = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "::/96", "2002::/16", "64:ff9b::/96", "64:ff9b:1::/48")]
HEAD_LIMIT = 8192
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 120
MAX_LIFETIME = 3 * 3600
MAX_TUNNELS = 32
log = logging.getLogger("egress")


def host_allowed(host: str) -> bool:
    host = host.lower().rstrip(".")
    if not HOSTNAME_RE.fullmatch(host):
        return False  # rejects NUL/control characters, non-ASCII, '@', '%', '_' and over-long names
    try:
        ipaddress.ip_address(host)
        return False  # IP literals bypass the name allowlist
    except ValueError:
        pass
    return host in ALLOWED_HOSTS or any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES)


def address_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not any(ip in network for network in BLOCKED_NETWORKS if network.version == ip.version)


async def system_resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or not all(address_public(a) for a in addresses):
        raise PermissionError(f"{host} resolves to a non-public address: {addresses}")
    return addresses


async def system_open(address: str, port: int):
    return await asyncio.open_connection(address, port)


class RateLimitedLog:
    """At most `limit` lines per minute, so a flood from the client cannot rotate away the evidence."""

    def __init__(self, limit: int, level: int = logging.WARNING):
        self.limit, self.level, self.window, self.count, self.dropped = limit, level, 0, 0, 0

    def __call__(self, message: str, *args) -> None:
        minute = int(time.monotonic() // 60)
        if minute != self.window:
            if self.dropped:
                log.log(self.level, "suppressed %d similar lines", self.dropped)
            self.window, self.count, self.dropped = minute, 0, 0
        self.count += 1
        if self.count <= self.limit:
            log.log(self.level, message, *args)
        else:
            self.dropped += 1


async def reply(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def tunnel(client_reader, client_writer, up_reader, up_writer) -> tuple[int, int]:
    last = [time.monotonic()]
    started = last[0]

    async def pipe(reader, writer) -> int:
        total = 0
        try:
            while data := await reader.read(65536):
                last[0] = time.monotonic()
                total += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
        return total

    up = asyncio.ensure_future(pipe(client_reader, up_writer))
    down = asyncio.ensure_future(pipe(up_reader, client_writer))
    while not (up.done() and down.done()):
        await asyncio.wait({up, down}, timeout=min(10, IDLE_TIMEOUT))
        now = time.monotonic()
        if not (up.done() and down.done()) and (now - last[0] > IDLE_TIMEOUT or now - started > MAX_LIFETIME):
            for task in (up, down):
                task.cancel()
            up_writer.close()
            client_writer.close()
            break
    results = await asyncio.gather(up, down, return_exceptions=True)
    return tuple(r if isinstance(r, int) else 0 for r in results)


async def serve(listen_host: str, listen_port: int, allowed_clients: set[str], *,
                resolver=system_resolve, opener=system_open) -> asyncio.base_events.Server:
    slots = asyncio.Semaphore(MAX_TUNNELS)  # held from the first byte, so half-open requests count too
    refused = RateLimitedLog(20)
    tunnels = RateLimitedLog(120, logging.INFO)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = (writer.get_extra_info("peername") or ("?",))[0]
        if client not in allowed_clients:
            writer.close()  # no reply: the port should look closed to anyone else
            return
        if slots.locked():
            refused("refused %s: %d connections already open", client, MAX_TUNNELS)
            try:  # take what was sent so closing does not reset the connection before the reply arrives
                await asyncio.wait_for(reader.read(HEAD_LIMIT), 1)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
            await reply(writer, "503 Service Unavailable")
            return
        async with slots:
            await handle_request(client, reader, writer)

    async def handle_request(client: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError, OSError):
            writer.close()
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) != 3 or parts[0] != "CONNECT":
            await reply(writer, "405 Method Not Allowed" if len(parts) == 3 else "400 Bad Request")
            return
        host, _, port = parts[1].rpartition(":")
        host = host.lower().rstrip(".")  # one normalised name for the check and the lookup
        if not host or not (port.isascii() and port.isdecimal() and len(port) <= 5):
            await reply(writer, "400 Bad Request")
            return
        if int(port) != 443 or not host_allowed(host):
            refused("refused %s %r", client, parts[1][:120])
            await reply(writer, "403 Forbidden")
            return
        try:
            addresses = await asyncio.wait_for(resolver(host), CONNECT_TIMEOUT)
            if isinstance(addresses, str):
                addresses = [addresses]
            if not addresses or not all(address_public(a) for a in addresses):
                raise PermissionError(f"{host} resolves to a non-public address: {addresses}")
        except PermissionError as exc:
            refused("refused %s: %s", client, exc)
            await reply(writer, "403 Forbidden")
            return
        except (OSError, ValueError, UnicodeError, asyncio.TimeoutError) as exc:
            refused("resolve %s failed: %s", host, exc)
            await reply(writer, "502 Bad Gateway")
            return
        upstream = None
        for address in addresses:  # try each validated address (e.g. IPv6 first on an IPv4-only line)
            try:
                upstream = await asyncio.wait_for(opener(address, 443), CONNECT_TIMEOUT)
                break
            except (OSError, asyncio.TimeoutError) as exc:
                refused("connect %s (%s) failed: %s", host, address, exc)
        if upstream is None:
            await reply(writer, "502 Bad Gateway")
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        sent, received = await tunnel(reader, writer, *upstream)
        tunnels("tunnel %s -> %s up=%d down=%d %.1fs", client, host, sent, received, time.monotonic() - started)

    return await asyncio.start_server(handle, listen_host, listen_port, limit=HEAD_LIMIT)


async def main_async(args) -> None:
    host, _, port = args.listen.rpartition(":")
    while True:  # the Tailscale address may appear only after this starts at boot
        try:
            server = await serve(host, int(port), set(args.allow_client))
            break
        except OSError as exc:
            log.warning("cannot listen on %s yet (%s); retrying in 15 s", args.listen, exc)
            await asyncio.sleep(15)
    log.info("listening on %s for %s", args.listen, ", ".join(args.allow_client))
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", required=True, help="Tailscale address:port of this PC")
    parser.add_argument("--allow-client", action="append", required=True, help="client IP allowed to connect (repeatable)")
    parser.add_argument("--log", help="log file (rotated at 1 MB x 3); default stderr")
    args = parser.parse_args()
    handler = (logging.handlers.RotatingFileHandler(args.log, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
               if args.log else logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
