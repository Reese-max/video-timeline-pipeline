import asyncio
import importlib.util
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline

PROXY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "youtube-egress" / "egress_proxy.py"
GOOGLE_IP = "142.250.196.110"


class YouTubeProxyOptionTests(unittest.TestCase):
    proxy = "http://100.74.111.42:18443"

    def test_is_youtube_url_hosts(self):
        for url in ("https://www.youtube.com/watch?v=abc", "https://youtube.com/shorts/abc", "https://m.youtube.com/watch?v=abc",
                    "https://music.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://www.youtube-nocookie.com/embed/abc"):
            self.assertTrue(pipeline.is_youtube_url(url), url)
        for url in ("https://www.youtube.com.example.com/watch?v=abc", "https://notyoutube.com/x", "https://www.facebook.com/reel/1",
                    "not a url", "https://youtu.be.evil.test/abc"):
            self.assertFalse(pipeline.is_youtube_url(url), url)

    def download_command(self, url, proxy):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": proxy}), \
                patch.object(pipeline, "run_external") as run:
            root = Path(folder)
            (root / "source.mp4").write_bytes(b"video")
            pipeline.download_video(url, root)
            return run.call_args.args[0]

    def test_only_youtube_downloads_use_the_proxy(self):
        command = self.download_command("https://www.youtube.com/watch?v=jNQXAC9IVRw", self.proxy)
        self.assertEqual(command[command.index("--proxy") + 1], self.proxy)
        self.assertEqual(command[-1], "https://www.youtube.com/watch?v=jNQXAC9IVRw")  # options stay before the URL
        self.assertNotIn("--proxy", self.download_command("https://vimeo.com/123", self.proxy))
        self.assertNotIn("--proxy", self.download_command("https://youtu.be/jNQXAC9IVRw", ""))

    def test_youtube_captions_use_the_proxy(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": self.proxy}), \
                patch.object(pipeline, "run_external") as run:
            pipeline.fetch_youtube_transcript("https://youtu.be/jNQXAC9IVRw", Path(folder), "auto")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--proxy") + 1], self.proxy)


def load_proxy():
    spec = importlib.util.spec_from_file_location("egress_proxy_under_test", PROXY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EgressProxyTests(unittest.TestCase):
    """Run the proxy on 127.0.0.1; allowed names resolve to a public IP that the opener sends to a local echo."""

    def setUp(self):
        self.proxy = load_proxy()

    async def start(self, *, allowed_clients=("127.0.0.1",), resolve=None, real_resolver=False):
        async def echo(reader, writer):
            try:
                while data := await reader.read(100):
                    writer.write(b"echo:" + data)
                    await writer.drain()
            finally:
                writer.close()
        self.upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        upstream_port = self.upstream.sockets[0].getsockname()[1]
        self.resolved, self.opened = [], []

        async def resolver(host):
            self.resolved.append(host)
            return resolve(host) if resolve else [GOOGLE_IP]

        async def opener(ip, port):
            self.opened.append((ip, port))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        kwargs = {"opener": opener} if real_resolver else {"resolver": resolver, "opener": opener}
        self.server = await self.proxy.serve("127.0.0.1", 0, set(allowed_clients), **kwargs)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        self.upstream.close()
        await self.server.wait_closed()
        await self.upstream.wait_closed()

    async def request(self, port, raw):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(raw)
        await writer.drain()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            head = b""  # closed without a reply
        return reader, writer, head

    def exchange(self, raw, **options):
        async def scenario():
            port = await self.start(**options)
            reader, writer, head = await self.request(port, raw)
            body = b""
            if head.startswith(b"HTTP/1.1 200"):
                writer.write(b"ping")
                await writer.drain()
                body = await asyncio.wait_for(reader.read(100), 5)
            writer.close()
            await self.stop()
            return head, body
        return asyncio.run(scenario())

    def test_allowed_host_is_tunnelled_to_the_validated_address(self):
        head, body = self.exchange(b"CONNECT rr3---sn-a5mekn6s.googlevideo.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        self.assertEqual(body, b"echo:ping")
        self.assertEqual((self.resolved, self.opened), (["rr3---sn-a5mekn6s.googlevideo.com"], [(GOOGLE_IP, 443)]))

    def test_refusals_happen_before_any_lookup(self):
        cases = {
            b"GET http://www.youtube.com/ HTTP/1.1\r\n\r\n": b"405",
            b"CONNECT www.youtube.com:80 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT www.youtube.com:\xb2 HTTP/1.1\r\n\r\n": b"400",
            b"CONNECT www.youtube.com:" + b"4" * 5000 + b" HTTP/1.1\r\n\r\n": b"400",
            b"CONNECT example.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT example.com\x00.youtube.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT localhost\x00.youtube.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT www.y\xf6utube.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT youtube.com.evil.test:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT www.google.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT storage.googleapis.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT 67.45.123.34.bc.googleusercontent.com:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT 142.250.1.1:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT [2404:6800::1]:443 HTTP/1.1\r\n\r\n": b"403",
            b"CONNECT nonsense HTTP/1.1\r\n\r\n": b"400",
        }
        for raw, status in cases.items():
            with self.subTest(raw=raw):
                head, body = self.exchange(raw)
                self.assertTrue(head.startswith(b"HTTP/1.1 " + status), head)
                self.assertEqual((body, self.resolved, self.opened), (b"", [], []))

    def test_oversized_request_head_is_dropped(self):
        head, body = self.exchange(b"CONNECT www.youtube.com:443 HTTP/1.1\r\nX: " + b"a" * 9000 + b"\r\n\r\n")
        self.assertEqual((head, body, self.opened), (b"", b"", []))

    def test_names_resolving_to_non_public_addresses_are_refused(self):
        for answer in (["10.0.0.5"], ["192.168.1.1"], ["127.0.0.1"], ["100.82.40.33"], ["169.254.169.254"], ["::1"],
                       ["fd7a:115c::1"], ["::ffff:10.0.0.1"], ["::10.0.0.1"], ["2002:c0a8:101::1"], ["64:ff9b::c0a8:101"],
                       [GOOGLE_IP, "10.0.0.1"]):
            with self.subTest(answer=answer):
                head, _ = self.exchange(b"CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n", resolve=lambda host: answer)
                self.assertTrue(head.startswith(b"HTTP/1.1 403"), head)
                self.assertEqual(self.opened, [])

    def test_system_resolver_checks_every_answer(self):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            answers = {"mixed.youtube.com": [GOOGLE_IP, "10.1.2.3"], "ok.youtube.com": [GOOGLE_IP, "142.250.196.111"]}[host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers]
        with patch("socket.getaddrinfo", fake_getaddrinfo):
            self.assertEqual(asyncio.run(self.proxy.system_resolve("ok.youtube.com")), [GOOGLE_IP, "142.250.196.111"])
            with self.assertRaises(PermissionError):
                asyncio.run(self.proxy.system_resolve("mixed.youtube.com"))
            head, _ = self.exchange(b"CONNECT mixed.youtube.com:443 HTTP/1.1\r\n\r\n", real_resolver=True)
        self.assertTrue(head.startswith(b"HTTP/1.1 403"), head)
        self.assertEqual(self.opened, [])

    def test_unknown_client_is_dropped_without_a_reply(self):
        head, body = self.exchange(b"CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n", allowed_clients=("100.82.40.33",))
        self.assertEqual((head, body, self.resolved), (b"", b"", []))

    def test_tunnel_limit_and_idle_timeout(self):
        async def scenario():
            port = await self.start()
            first_reader, first_writer, first = await self.request(port, b"CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n")
            _, second_writer, second = await self.request(port, b"CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n")
            second_writer.close()
            closed = await asyncio.wait_for(first_reader.read(100), 5)  # idle tunnel is closed by the proxy
            first_writer.close()
            await self.stop()
            return first, second, closed
        with patch.object(self.proxy, "MAX_TUNNELS", 1), patch.object(self.proxy, "IDLE_TIMEOUT", 0.3):
            first, second, closed = asyncio.run(scenario())
        self.assertTrue(first.startswith(b"HTTP/1.1 200"), first)
        self.assertTrue(second.startswith(b"HTTP/1.1 503"), second)
        self.assertEqual(closed, b"")

    def test_host_allowlist(self):
        allowed = self.proxy.host_allowed
        for host in ("www.youtube.com", "youtube.com", "rr1---sn-x.googlevideo.com", "youtubei.googleapis.com",
                     "youtu.be", "WWW.YouTube.com.", "www.youtube-nocookie.com"):
            self.assertTrue(allowed(host), host)
        for host in ("example.com", "youtube.com.evil.test", "evilyoutube.com", "10.0.0.1", "localhost", "", "a" * 300,
                     "example.com\x00.youtube.com", "under_score.youtube.com", "-x.youtube.com", "www.google.com",
                     "i.ytimg.com", "storage.googleapis.com", "x.bc.googleusercontent.com", "wwŵ.youtube.com"):
            self.assertFalse(allowed(host), repr(host))


if __name__ == "__main__":
    unittest.main()
