from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import cloudinary
import cloudinary.api
from cloudinary.uploader import upload

REQUIRED_ENV_VARS = (
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)


def load_dotenv() -> None:
    candidate_paths = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]

    for env_path in candidate_paths:
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
        return


def configure_cloudinary() -> None:
    load_dotenv()

    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        missing_list = ", ".join(missing)
        raise ValueError(f"Missing Cloudinary environment variables: {missing_list}")

    cloudinary.config(
        cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
        api_key=os.environ["CLOUDINARY_API_KEY"],
        api_secret=os.environ["CLOUDINARY_API_SECRET"],
        secure=True,
    )


def detect_resource_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type:
        return "auto"

    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return "auto"


def build_delivery_url(public_id: str | None, *, transform: str = "w_200,f_auto") -> str | None:
    if not public_id:
        return None
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME")
    if not cloud_name:
        return None
    return f"https://res.cloudinary.com/{cloud_name}/image/upload/{transform}/{public_id}"


def validate_delivery_url(url: str | None) -> bool:
    if not url:
        return False

    request = Request(url, method="HEAD")
    with urlopen(request, timeout=10) as response:
        return 200 <= response.status < 400


def public_id_exists(public_id: str | None, *, resource_type: str = "image") -> bool | None:
    if not public_id:
        return None

    try:
        cloudinary.api.resource(public_id, resource_type=resource_type)
        return True
    except Exception as exc:
        if exc.__class__.__name__ == "NotFound":
            return False
        raise


def list_assets(
    *,
    prefix: str | None = None,
    tag: str | None = None,
    resource_type: str = "image",
    max_results: int = 10,
) -> dict[str, Any]:
    configure_cloudinary()

    if prefix and tag:
        raise ValueError("Use either prefix or tag, not both")

    if tag:
        response = cloudinary.api.resources_by_tag(
            tag,
            resource_type=resource_type,
            max_results=max_results,
        )
    else:
        response = cloudinary.api.resources(
            type="upload",
            prefix=prefix,
            resource_type=resource_type,
            max_results=max_results,
        )

    resources = []
    for item in response.get("resources", []):
        item_public_id = item.get("public_id")
        resources.append(
            {
                "asset_id": item.get("asset_id"),
                "public_id": item_public_id,
                "resource_type": item.get("resource_type"),
                "format": item.get("format"),
                "bytes": item.get("bytes"),
                "width": item.get("width"),
                "height": item.get("height"),
                "created_at": item.get("created_at"),
                "secure_url": item.get("secure_url"),
                "url": item.get("url"),
                "delivery_url": build_delivery_url(item_public_id)
                if item.get("resource_type") == "image"
                else None,
            }
        )

    return {
        "count": len(resources),
        "next_cursor": response.get("next_cursor"),
        "resources": resources,
    }


def download_asset(
    public_id: str,
    *,
    output_dir: str | Path = ".",
    resource_type: str = "image",
    transform: str = "w_200,f_auto",
    original: bool = False,
) -> dict[str, Any]:
    configure_cloudinary()

    try:
        resource = cloudinary.api.resource(public_id, resource_type=resource_type)
    except Exception as exc:
        if exc.__class__.__name__ == "NotFound":
            raise FileNotFoundError(f"Cloudinary asset not found: {public_id}") from exc
        raise

    source_url = (
        resource.get("secure_url") or resource.get("url")
        if original or resource_type != "image"
        else build_delivery_url(public_id, transform=transform)
    )
    if not source_url:
        raise ValueError(f"Could not determine a download URL for public ID: {public_id}")

    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(source_url)
    suffix = Path(parsed.path).suffix
    if not suffix:
        resource_format = resource.get("format")
        suffix = f".{resource_format}" if resource_format else ""

    filename = public_id.rsplit("/", 1)[-1] + suffix
    destination = output_path / filename

    request = Request(source_url)
    with urlopen(request, timeout=30) as response:
        destination.write_bytes(response.read())

    return {
        "public_id": public_id,
        "resource_type": resource_type,
        "download_type": "original" if original else "transformed",
        "source_url": source_url,
        "local_path": str(destination),
        "bytes": destination.stat().st_size,
    }


def upload_file(
    file_path: str | Path,
    *,
    folder: str | None = None,
    public_id: str | None = None,
    tags: list[str] | None = None,
    overwrite: bool = False,
    validate_url: bool = False,
) -> dict[str, Any]:
    configure_cloudinary()

    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    options: dict[str, Any] = {
        "resource_type": detect_resource_type(path),
        "use_filename": public_id is None,
        "unique_filename": public_id is None,
        "overwrite": overwrite,
    }
    existing_before_upload = public_id_exists(
        public_id,
        resource_type=options["resource_type"],
    )
    if folder:
        options["folder"] = folder
    if public_id:
        options["public_id"] = public_id
    if tags:
        options["tags"] = tags

    response = upload(str(path), **options)
    resolved_public_id = response.get("public_id")
    delivery_url = build_delivery_url(resolved_public_id)
    return {
        "local_path": str(path),
        "asset_id": response.get("asset_id"),
        "exists_before_upload": existing_before_upload,
        "public_id": resolved_public_id,
        "version": response.get("version"),
        "resource_type": response.get("resource_type"),
        "format": response.get("format"),
        "width": response.get("width"),
        "height": response.get("height"),
        "bytes": response.get("bytes"),
        "created_at": response.get("created_at"),
        "original_filename": response.get("original_filename"),
        "secure_url": response.get("secure_url"),
        "url": response.get("url"),
        "delivery_url": delivery_url,
        "delivery_url_valid": validate_delivery_url(delivery_url) if validate_url else None,
    }
