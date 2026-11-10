import os
import unittest
from unittest import mock

from seller_auto.proxy import (
    proxies_from_subscription,
    render_proxy_config,
    select_node,
    subscription_settings,
)

SAMPLE = """
proxies:
  - {name: 🇸🇬 Lil 新加坡02, server: api.example.com, port: 18566, type: ssr, cipher: chacha20-ietf, password: secret, protocol: auth_aes128_md5, obfs: plain, protocol-param: 196005:SIMBA798, obfs-param: example.com, udp: true}
  - name: 🇦🇷 Lil 阿根廷01
    type: ssr
    server: api.example.com
    port: 33568
    cipher: rc4-md5
    password: secret
    protocol: auth_aes128_md5
    obfs: plain
    protocol-param: 196005:SIMBA798
    obfs-param: example.com
    udp: true
proxy-groups:
  - name: PROXY
"""


class ProxyConfigTests(unittest.TestCase):
    def test_flow_and_block_nodes_keep_protocol_param(self) -> None:
        proxies = proxies_from_subscription(SAMPLE)
        self.assertEqual([item["name"] for item in proxies], ["🇸🇬 Lil 新加坡02", "🇦🇷 Lil 阿根廷01"])
        self.assertEqual(proxies[0]["protocol-param"], "196005:SIMBA798")
        self.assertEqual(proxies[1]["port"], 33568)
        self.assertEqual(proxies[1]["udp"], True)

    def test_select_node_uses_the_configured_name(self) -> None:
        node = select_node(proxies_from_subscription(SAMPLE), "Lil 阿根廷01")
        config = render_proxy_config(node)
        self.assertIn("port: 33568", config)
        self.assertIn("DOMAIN,api.example.com,DIRECT", config)
        self.assertIn("MATCH,PROXY", config)

    def test_missing_subscription_url_is_reported(self) -> None:
        with (
            mock.patch("seller_auto.proxy.load_local_env"),
            mock.patch.dict(os.environ, {"SELLER_AUTO_NODE_MATCH": "阿根廷01"}, clear=False),
        ):
            os.environ.pop("SELLER_AUTO_SUBSCRIPTION_URL", None)
            with self.assertRaises(RuntimeError) as raised:
                subscription_settings()
        self.assertIn("SELLER_AUTO_SUBSCRIPTION_URL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
