"""Exercise the local RustFS S3 contract using only UUID-named probe objects."""

import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from botocore.exceptions import ClientError

from local_services import (
    ROOT, credentials, require, require_empty_bucket, s3_client, verified_hosts,
)


def read_object(client, bucket, key, **options):
    response = client.get_object(Bucket=bucket, Key=key, **options)
    try:
        return response["Body"].read(), response
    finally:
        response["Body"].close()


def main():
    require(
        Path.cwd() == ROOT
        and sys.executable.startswith("/root/anaconda3/envs/wagtailblog-test/"),
        "Use the authorized WSL cwd and Conda interpreter",
    )
    values, hosts = credentials(), verified_hosts()
    s3 = s3_client(values, hosts)
    anonymous = s3_client(values, hosts, anonymous=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    results = []
    try:
        for bucket in ("myblog-dev", "myblog-test"):
            require_empty_bucket(s3, bucket)
            # Only this unique prefix is ever removed, even after a lost reply.
            prefix = "_myblog_s3_probe/" + uuid4().hex + "/"
            key = prefix + "keep.txt"
            bulk = [prefix + f"bulk-{index}.txt" for index in range(3)]
            multipart_key = prefix + "multipart.bin"
            payload = "RustFS 私有对象 S3 验证".encode()
            try:
                s3.put_object(
                    Bucket=bucket, Key=key, Body=payload,
                    ContentType="text/plain; charset=utf-8",
                    Metadata={"project": "anc1996-myblog"},
                )
                body, response = read_object(s3, bucket, key)
                require(body == payload, "Full readback failed")
                require(response["Metadata"] == {"project": "anc1996-myblog"},
                        "User metadata readback failed")
                body, response = read_object(s3, bucket, key, Range="bytes=3-9")
                require(
                    body == payload[3:10]
                    and response["ResponseMetadata"]["HTTPStatusCode"] == 206
                    and response["ContentRange"] == f"bytes 3-9/{len(payload)}",
                    "Range response mismatch",
                )
                for operation, options in (
                    (anonymous.get_object, {"Key": key}),
                    (anonymous.list_objects_v2, {}),
                ):
                    try:
                        response = operation(Bucket=bucket, **options)
                    except ClientError as exc:
                        require(exc.response["Error"]["Code"] == "AccessDenied",
                                "Unexpected anonymous response")
                    else:
                        if "Body" in response:
                            response["Body"].close()
                        raise ValueError("Anonymous access unexpectedly succeeded")
                signed = s3.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60,
                )
                with opener.open(signed, timeout=10) as response:
                    require(response.read() == payload, "Presigned GET readback failed")
                parsed = urllib.parse.urlsplit(signed)
                query = urllib.parse.parse_qsl(parsed.query)
                query = [(name, "0" * 64 if name == "X-Amz-Signature" else value)
                         for name, value in query]
                tampered = urllib.parse.urlunsplit(
                    parsed._replace(query=urllib.parse.urlencode(query)),
                )
                try:
                    opener.open(tampered, timeout=10).close()
                except urllib.error.HTTPError as exc:
                    try:
                        require(exc.code == 403, "Unexpected invalid-signature status")
                    finally:
                        exc.close()
                else:
                    raise ValueError("Tampered signature unexpectedly accepted")

                upload = s3.create_multipart_upload(
                    Bucket=bucket, Key=multipart_key, ContentType="application/octet-stream",
                )["UploadId"]
                parts, chunks = [], [b"R" * (5 * 1024 * 1024), b"rustfs-last-part"]
                for number, chunk in enumerate(chunks, 1):
                    part = s3.upload_part(
                        Bucket=bucket, Key=multipart_key, UploadId=upload,
                        PartNumber=number, Body=chunk,
                    )
                    parts.append({"PartNumber": number, "ETag": part["ETag"]})
                s3.complete_multipart_upload(
                    Bucket=bucket, Key=multipart_key, UploadId=upload,
                    MultipartUpload={"Parts": parts},
                )
                body, _ = read_object(s3, bucket, multipart_key)
                require(body == b"".join(chunks), "Multipart readback failed")
                for name in bulk:
                    s3.put_object(Bucket=bucket, Key=name, Body=payload)
                result = s3.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": name} for name in bulk]},
                )
                require(not result.get("Errors"), "Batch delete returned per-object errors")
                for name in bulk:
                    try:
                        s3.head_object(Bucket=bucket, Key=name)
                    except ClientError as exc:
                        require(exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"),
                                "Unexpected deleted-object response")
                    else:
                        raise ValueError("Batch-deleted object still exists")
                body, _ = read_object(s3, bucket, key)
                require(body == payload, "Batch delete removed an unrelated probe")
                print(bucket, "private/metadata/Range206/presign/signature rejection/"
                      "multipart/batch delete OK", flush=True)
                results.append({"bucket": bucket, "passed": True})
            finally:
                # Abort only unfinished uploads under this run's owned prefix.
                for page in s3.get_paginator("list_multipart_uploads").paginate(
                    Bucket=bucket, Prefix=prefix,
                ):
                    for upload in page.get("Uploads", []):
                        s3.abort_multipart_upload(
                            Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"],
                        )
                s3.delete_objects(Bucket=bucket, Delete={
                    "Objects": [{"Key": name} for name in [key, multipart_key, *bulk]],
                })
            require_empty_bucket(s3, bucket)
        # No credentials, signed URLs or object contents enter the evidence file.
        evidence = Path("/opt/myblog-services/rustfs-s3-verification.json")
        evidence.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print("S3 probes cleaned; both buckets empty. No application migrations.")
    finally:
        anonymous.close()
        s3.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Client/HTTP exceptions may contain signed URLs or connection details.
        print(f"Local S3 verification failed ({type(exc).__name__}); details withheld",
              file=sys.stderr)
        sys.exit(1)
