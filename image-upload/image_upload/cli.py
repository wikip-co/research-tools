from __future__ import annotations

import argparse
import json
import sys

from .uploader import upload_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload a local image or video file to Cloudinary."
    )
    parser.add_argument("file", nargs="?", help="Path to the local file to upload")
    parser.add_argument("--folder", help="Optional Cloudinary folder")
    parser.add_argument("--public-id", help="Optional Cloudinary public ID")
    parser.add_argument(
        "--search-prefix",
        help="List uploaded assets whose public IDs start with this prefix.",
    )
    parser.add_argument(
        "--search-tag",
        help="List uploaded assets with this Cloudinary tag.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Maximum number of assets to return for search.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download an existing Cloudinary asset by public ID.",
    )
    parser.add_argument(
        "--download-original",
        action="store_true",
        help="When downloading an image, fetch the original uploaded asset instead of the transformed site image.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write downloaded assets into.",
    )
    parser.add_argument(
        "--resource-type",
        default="image",
        help="Cloudinary resource type to query. Defaults to image.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check whether a public ID exists in Cloudinary without uploading a file.",
    )
    parser.add_argument(
        "--validate-url",
        action="store_true",
        help="Check that the transformed Cloudinary delivery URL is reachable after upload.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="Optional tag to attach to the asset. Repeat to add more tags.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing asset with the same public ID.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.search_prefix or args.search_tag:
            from .uploader import list_assets

            result = list_assets(
                prefix=args.search_prefix,
                tag=args.search_tag,
                resource_type=args.resource_type,
                max_results=args.max_results,
            )
            print(json.dumps({"ok": True, "result": result}, indent=2))
            return 0

        if args.download:
            if not args.public_id:
                parser.error("--download requires --public-id")
            from .uploader import download_asset

            result = download_asset(
                args.public_id,
                output_dir=args.output_dir,
                resource_type=args.resource_type,
                original=args.download_original,
            )
            print(json.dumps({"ok": True, "result": result}, indent=2))
            return 0

        if args.check_only:
            if not args.public_id:
                parser.error("--check-only requires --public-id")
            from .uploader import build_delivery_url, configure_cloudinary, public_id_exists

            configure_cloudinary()
            result = {
                "public_id": args.public_id,
                "exists": public_id_exists(
                    args.public_id,
                    resource_type=args.resource_type,
                ),
                "delivery_url": build_delivery_url(args.public_id)
                if args.resource_type == "image"
                else None,
            }
            print(json.dumps({"ok": True, "result": result}, indent=2))
            return 0

        if not args.file:
            parser.error("the following arguments are required: file")

        result = upload_file(
            args.file,
            folder=args.folder,
            public_id=args.public_id,
            tags=args.tags,
            overwrite=args.overwrite,
            validate_url=args.validate_url,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
