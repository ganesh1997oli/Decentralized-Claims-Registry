import unittest

from ipfs import IPFSClient, IPFSError, PINATA_UPLOAD_URL


class FakeResponse:
    def __init__(self, *, content=b"", json_body=None, error=None):
        self.content = content
        self._json_body = json_body
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._json_body


class FakeSession:
    def __init__(self):
        self.post_response = None
        self.get_response = None
        self.post_call = None
        self.get_call = None

    def post(self, url, **kwargs):
        self.post_call = (url, kwargs)
        return self.post_response

    def get(self, url, **kwargs):
        self.get_call = (url, kwargs)
        return self.get_response


class IPFSClientTests(unittest.TestCase):
    def test_uploads_public_bytes_and_returns_cid(self):
        session = FakeSession()
        session.post_response = FakeResponse(
            json_body={"data": {"cid": "bafy-test-cid"}}
        )
        client = IPFSClient(pinata_jwt="secret", session=session)

        cid = client.upload_bytes(
            b'{"claim":1}', filename="claim-1.json", content_type="application/json"
        )

        self.assertEqual(cid, "bafy-test-cid")
        url, kwargs = session.post_call
        self.assertEqual(url, PINATA_UPLOAD_URL)
        self.assertEqual(kwargs["data"]["network"], "public")
        self.assertEqual(kwargs["files"]["file"][1], b'{"claim":1}')
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")

    def test_downloads_an_ipfs_pointer_through_gateway(self):
        session = FakeSession()
        session.get_response = FakeResponse(content=b"claim bytes")
        client = IPFSClient(
            gateway="https://example.test/ipfs/", session=session
        )

        payload = client.download_pointer("ipfs://bafy-test-cid", attempts=1)

        self.assertEqual(payload, b"claim bytes")
        self.assertEqual(
            session.get_call[0], "https://example.test/ipfs/bafy-test-cid"
        )

    def test_rejects_non_ipfs_data_pointer(self):
        with self.assertRaises(IPFSError):
            IPFSClient.target_from_pointer("https://example.test/claim.json")

    def test_upload_requires_pinata_jwt(self):
        with self.assertRaises(IPFSError):
            IPFSClient().upload_bytes(b"claim", filename="claim.json")


if __name__ == "__main__":
    unittest.main()
