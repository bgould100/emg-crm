
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def _bucket_name():
    return os.getenv("EMG_S3_BUCKET", "").strip()


def _s3_client():
    region = (
        os.getenv("EMG_S3_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or None
    )
    return boto3.client("s3", region_name=region)


def _upload_directory():
    return (
        Path(__file__).resolve().parents[2]
        / "data"
        / "uploads"
    )


def _valid_filename(filename):
    return (
        isinstance(filename, str)
        and bool(filename)
        and filename not in {".", ".."}
        and Path(filename).name == filename
        and "/" not in filename
        and "\\" not in filename
    )


def _valid_storage_key(storage_key):
    return (
        isinstance(storage_key, str)
        and bool(storage_key)
    )


def save_photo(filename, content, content_type):
    if not _valid_filename(filename):
        raise ValueError("Invalid photo filename.")

    bucket = _bucket_name()

    if bucket:
        object_key = f"photos/{filename}"

        _s3_client().put_object(
            Bucket=bucket,
            Key=object_key,
            Body=content,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )

        return f"s3/{object_key}"

    upload_dir = _upload_directory()
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / filename).write_bytes(content)

    return f"local/{filename}"


def read_photo(storage_key):
    if not _valid_storage_key(storage_key):
        raise FileNotFoundError("Photo not found.")

    if storage_key.startswith("s3/"):
        bucket = _bucket_name()
        object_key = storage_key.removeprefix("s3/")

        if not bucket or not object_key.startswith("photos/"):
            raise FileNotFoundError("Photo not found.")

        try:
            response = _s3_client().get_object(
                Bucket=bucket,
                Key=object_key,
            )
        except ClientError as error:
            error_code = error.response.get("Error", {}).get("Code")
            status_code = (
                error.response.get("ResponseMetadata", {}).get(
                    "HTTPStatusCode"
                )
            )

            if error_code in {"NoSuchKey", "404", "NotFound"} or status_code == 404:
                raise FileNotFoundError("Photo not found.") from error

            raise

        return response["Body"].read()

    if storage_key.startswith("local/"):
        filename = storage_key.removeprefix("local/")

        if not _valid_filename(filename):
            raise FileNotFoundError("Photo not found.")

        file_path = _upload_directory() / filename

        if not file_path.is_file():
            raise FileNotFoundError("Photo not found.")

        return file_path.read_bytes()

    raise FileNotFoundError("Photo not found.")


def delete_photo(storage_key):
    if not _valid_storage_key(storage_key):
        raise FileNotFoundError("Photo not found.")

    if storage_key.startswith("s3/"):
        bucket = _bucket_name()
        object_key = storage_key.removeprefix("s3/")

        if not bucket or not object_key.startswith("photos/"):
            raise FileNotFoundError("Photo not found.")

        _s3_client().delete_object(
            Bucket=bucket,
            Key=object_key,
        )
        return

    if storage_key.startswith("local/"):
        filename = storage_key.removeprefix("local/")

        if not _valid_filename(filename):
            raise FileNotFoundError("Photo not found.")

        (_upload_directory() / filename).unlink(missing_ok=True)
        return

    raise FileNotFoundError("Photo not found.")
