"""Self-check for the S3 staging helpers.

The durable ingest path is only correct if a PDF staged during the upload
request comes back byte-identical in a later, separate invocation - that is
the whole reason this module exists. Run: python test_uploads.py
"""

import os

os.environ["UPLOAD_S3_BUCKET"] = "test-bucket"

import uploads  # noqa: E402  (must follow the env var it reads at import)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def download_file(self, Bucket, Key, dest):
        with open(dest, "wb") as fh:
            fh.write(self.objects[(Bucket, Key)])

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def demo():
    fake = FakeS3()
    uploads._client = lambda: fake

    assert uploads.enabled(), "bucket set, so the durable path should be on"

    body = b"%PDF-1.4 pretend"
    key = uploads.put(body, "report.pdf")
    assert key.startswith("uploads/"), key
    assert key.endswith("/report.pdf"), key

    # Same filename twice must not collide: the first ingest may still be running.
    assert uploads.put(body, "report.pdf") != key

    # The round trip a retry in a fresh container depends on.
    with open(uploads.fetch_to_tmp(key), "rb") as fh:
        assert fh.read() == body

    uploads.discard(key)
    uploads.discard(key)  # Inngest replays steps, so this must not raise
    assert (("test-bucket", key)) not in fake.objects

    # A path-traversal filename must not escape the prefix.
    assert uploads.put(body, "../../etc/passwd").startswith("uploads/")

    print("uploads: OK")


if __name__ == "__main__":
    demo()
