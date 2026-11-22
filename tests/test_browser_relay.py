import unittest

from test_durability import compose_config, needs_docker


class TestBrowserRelay(unittest.TestCase):
    @needs_docker
    def test_compose_browser_relay(self):
        relay = compose_config()["services"]["browser-relay"]
        self.assertEqual(relay["image"], "caddy:2-alpine")
        # Shares terminal's netns to reach the daemon's loopback-only relay;
        # port publishing is forbidden with this network mode (daemon rejects
        # it), so the host reaches the relay as terminal:19010 instead.
        self.assertEqual(relay["network_mode"], "service:terminal")
        self.assertNotIn("ports", relay)

    def test_relay_caddyfile(self):
        with open("config/browser-relay/Caddyfile") as f:
            caddyfile = f.read()
        # 19010, not 9010: the daemon already holds 127.0.0.1:9010 in the
        # shared namespace, so binding 9010 here would conflict.
        self.assertIn(":19010", caddyfile)
        self.assertIn("reverse_proxy 127.0.0.1:9010", caddyfile)
        self.assertIn("header_up Host 127.0.0.1:9010", caddyfile)


if __name__ == "__main__":
    unittest.main()
