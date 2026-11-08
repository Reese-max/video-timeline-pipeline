import ipaddress
import socket
import unittest
from unittest.mock import MagicMock, patch

import mcp_server


FB_URL = "https://www.facebook.com/share/v/1ExtCtwPgg/?mibextid=wwXIfr"


def dns_result(address="93.184.216.34"):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def dns_json(address="93.184.216.34", record_type=1):
    return {"Status": 0, "TC": False,
            "Question": [{"name": "example.com.", "type": record_type}],
            "Answer": [{"name": "example.com.", "type": record_type, "data": address}]}


class SSRFFailClosedTests(unittest.TestCase):
    def test_dns_lookup_failure_never_allows_download(self):
        with patch.object(mcp_server.socket, "getaddrinfo", side_effect=socket.gaierror()), \
             patch.object(mcp_server.requests, "get") as get:
            with self.assertRaisesRegex(ValueError, "無法確認"):
                mcp_server.validate_source_url(FB_URL)
        get.assert_not_called()

    def test_mixed_system_dns_private_address_is_rejected(self):
        with patch.object(mcp_server.socket, "getaddrinfo",
                          return_value=dns_result()+dns_result("10.0.0.1")):
            with self.assertRaisesRegex(ValueError, "公開 IP"):
                mcp_server.check_public_host("example.com")

    def test_private_and_mapped_ipv6_literals_are_rejected(self):
        for address in ["127.0.0.1", "169.254.169.254", "10.0.0.1",
                        "::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1"]:
            with self.subTest(address=address):
                self.assertFalse(mcp_server.address_is_public(ipaddress.ip_address(address)))
                with self.assertRaises(ValueError):
                    mcp_server.check_public_host(address)

    def test_doh_bad_contract_is_rejected(self):
        cases = []
        for field, value in [("Status", 2), ("Status", None), ("TC", True),
                             ("Question", []), ("Question", [{"name": "other.example", "type": 1}]),
                             ("Question", [{"name": "example.com.", "type": 28}]),
                             ("Question", [{"name": "example.com.", "type": True}]),
                             ("Question", [{"name": "example.com.", "type": 1.0}])]:
            data = dns_json()
            data[field] = value
            cases.append(data)
        for answer in [
            [{"name": "other.example.", "type": 1, "data": "93.184.216.34"}],
            [{"name": "example.com.", "type": 1, "data": "::1"}],
            [{"name": "example.com.", "type": 28, "data": "2606:4700:4700::1111"}],
            [{"name": "example.com.", "type": True, "data": "93.184.216.34"}],
            [{"name": "example.com.", "type": 1.0, "data": "93.184.216.34"}],
            [{"name": "example.com.", "type": 5.0, "data": "alias.example."}],
            [{"name": "example.com.", "type": 5, "data": "example.com."}],
            [{"name": "example.com.", "type": 5, "data": "alias.example."},
             {"name": "alias.example.", "type": 5, "data": "example.com."}],
            [{"name": "other.example.", "type": 5, "data": "alias.example."}],
        ]:
            data = dns_json()
            data["Answer"] = answer
            cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                mcp_server.dns_json_addresses(data, "example.com", 1)

    def test_doh_public_cname_chain_is_accepted(self):
        data = dns_json()
        data["Answer"] = [
            {"name": "example.com.", "type": 5, "data": "alias.example."},
            {"name": "alias.example.", "type": 1, "data": "93.184.216.34"}]
        self.assertEqual(mcp_server.dns_json_addresses(data, "example.com", 1),
                         {ipaddress.ip_address("93.184.216.34")})

    def test_private_doh_is_not_replaced_by_other_resolver(self):
        response = MagicMock(status_code=200)
        response.json.return_value = dns_json("10.0.0.1")
        with patch.object(mcp_server.requests, "get", return_value=response) as get:
            addresses = mcp_server.public_dns_addresses("example.com")
        self.assertEqual(addresses, {ipaddress.ip_address("10.0.0.1")})
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_all_doh_failures_block_fake_ip_even_for_facebook(self):
        for failure in [mcp_server.requests.Timeout(), ValueError("invalid JSON")]:
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(mcp_server.socket, "getaddrinfo", return_value=dns_result("198.18.0.1")), \
                 patch.object(mcp_server.requests, "get", side_effect=failure):
                with self.assertRaisesRegex(ValueError, "無法確認"):
                    mcp_server.validate_source_url(FB_URL)

    def test_redirect_errors_fail_closed(self):
        cases = [
            ({"Location": FB_URL}, "循環"),
            ({}, "Location"),
            ({"Location": "https://user:pass@www.facebook.com/reel/1"}, "帳號"),
            ({"Location": "ftp://www.facebook.com/reel/1"}, "HTTP"),
            ({"Location": "http://127.0.0.1/private"}, "公開 IP"),
            ({"Location": "http://169.254.169.254/private"}, "公開 IP"),
            ({"Location": "http://localhost/private"}, "本機"),
        ]
        for headers, error in cases:
            response = MagicMock(status_code=302, headers=headers)
            with self.subTest(headers=headers), \
                 patch.object(mcp_server.socket, "getaddrinfo", return_value=dns_result()), \
                 patch.object(mcp_server.requests, "get", return_value=response) as get:
                with self.assertRaisesRegex(ValueError, error):
                    mcp_server.validate_source_url(FB_URL)
            self.assertEqual(get.call_count, 1)
            response.close.assert_called_once()

    def test_redirect_request_failure_and_hop_limit_fail_closed(self):
        with patch.object(mcp_server.socket, "getaddrinfo", return_value=dns_result()), \
             patch.object(mcp_server.requests, "get", side_effect=mcp_server.requests.Timeout()):
            with self.assertRaisesRegex(ValueError, "無法驗證"):
                mcp_server.validate_source_url(FB_URL)
        hops = [MagicMock(status_code=302, headers={"Location": "/reel/1"}),
                MagicMock(status_code=302, headers={"Location": "/reel/2"})]
        with patch.object(mcp_server.socket, "getaddrinfo", return_value=dns_result()), \
             patch.object(mcp_server.requests, "get", side_effect=hops) as get:
            with self.assertRaisesRegex(ValueError, "驗證上限"):
                mcp_server.validate_share_redirects(FB_URL, max_hops=2)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(h.close.call_count == 1 for h in hops))

    def test_second_redirect_rechecks_dns_before_private_request(self):
        def lookup(host, port):
            return dns_result("10.0.0.1" if host == "rebound.example" else "93.184.216.34")
        hops = [MagicMock(status_code=302, headers={"Location": "/reel/1"}),
                MagicMock(status_code=302, headers={"Location": "https://rebound.example/private"})]
        with patch.object(mcp_server.socket, "getaddrinfo", side_effect=lookup), \
             patch.object(mcp_server.requests, "get", side_effect=hops) as get:
            with self.assertRaisesRegex(ValueError, "公開 IP"):
                mcp_server.validate_source_url(FB_URL)
        self.assertEqual(get.call_count, 2)
