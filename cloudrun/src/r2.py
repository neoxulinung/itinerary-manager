import asyncio

import boto3


class R2:
    # boto3 is a sync client - unlike D1 (which this project already talks to over a plain
    # REST API), R2's S3-compatible API has no first-party async client, so every call below
    # runs through asyncio.to_thread so it doesn't block the event loop other requests share.
    def __init__(self, account_id: str, access_key_id: str, secret_access_key: str, bucket: str, public_base_url: str):
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url}/{key}"
