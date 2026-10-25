import socket
import unittest

from seller_auto.ipc import encode_message, read_message


class IpcTests(unittest.TestCase):
    def test_message_roundtrip(self) -> None:
        left, right = socket.socketpair()
        try:
            left.sendall(encode_message({"method": "ping", "params": {}}))
            self.assertEqual(read_message(right)["method"], "ping")
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
